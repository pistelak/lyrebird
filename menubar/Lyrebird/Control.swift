import Foundation

/// Shell-outs to the `lyrebird` CLI and `xcrun simctl` — the app drives the engine, never duplicates it.
enum Control {
    struct Result {
        var output: String
        var status: Int32
        var succeeded: Bool { status == 0 }

        /// Why the command failed, never an empty string — a launcher that exits nonzero without
        /// saying why, or whose output is not UTF-8, used to leave `lastError` empty, and both error
        /// displays drop an empty message: the command failed and the app said nothing.
        /// See aCommandThatFailedSilentlyStillSaysSomethingTheDisplayKeeps.
        var failure: String? {
            guard !succeeded else { return nil }
            let message = output.trimmingCharacters(in: .whitespacesAndNewlines)
            return message.isEmpty ? "the command failed with exit status \(status) and said nothing" : message
        }
    }

    /// Runs a subprocess without blocking a thread.
    ///
    /// `Process` is event-driven, so there is nothing here to block on: output is drained as an
    /// async sequence and exit is delivered by `terminationHandler`. Draining runs concurrently
    /// with the process, which is also what stops a child that fills the OS pipe buffer from
    /// deadlocking against a parent waiting for it to exit.
    ///
    /// `readerStarted` is a test seam, not part of the interface: it hands over the buffer the
    /// child's output is read into, whose `isFinished` says whether the reading task ran to
    /// completion. A test needs that to prove the reader was *released* when this call gave up on
    /// it, rather than only that the call itself came back quickly.
    static func shell(
        _ launchPath: String, _ arguments: [String],
        environment: [String: String] = [:],
        readerStarted: (@Sendable (OutputBuffer) -> Void)? = nil
    ) async -> Result {
        guard !launchPath.isEmpty, FileManager.default.isExecutableFile(atPath: launchPath) else {
            return Result(
                output: launchPath.isEmpty
                    ? "lyrebird not found on PATH — set its location in Settings"
                    : "not found or not executable: \(launchPath)", status: -1)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: launchPath)
        process.arguments = arguments
        // Merged over the inherited environment rather than replacing it: the CLI resolves its own
        // interpreter and tools through PATH and HOME, so a child handed only our additions would
        // fail to start for reasons that have nothing to do with what we set.
        process.environment = ProcessInfo.processInfo.environment
            .merging(environment) { _, ours in ours }
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        let buffer = OutputBuffer()
        return await withTaskCancellationHandler {
            // Unstructured on purpose: it must be allowed to outlive this call. A killed child can
            // leave a grandchild holding the write end of the pipe, and waiting for *that* to close
            // is not something a cancelled call may do — so the reading goes into a buffer this
            // function can take whatever has arrived from.
            let output = Task { await drain(pipe.fileHandleForReading, into: buffer) }
            readerStarted?(buffer)

            let status: Int32 = await withCheckedContinuation { continuation in
                // Installed before run(): a handler set after the process has already exited may
                // never fire, and the continuation would leak.
                process.terminationHandler = { continuation.resume(returning: $0.terminationStatus) }
                do {
                    try process.run()
                } catch {
                    process.terminationHandler = nil
                    try? pipe.fileHandleForWriting.close()  // unblock the drain: no child will close it
                    continuation.resume(returning: -1)
                }
            }

            let text = await collect(
                output, from: buffer,
                reading: pipe.fileHandleForReading, within: drainGrace)
            return Result(
                output: text.isEmpty && status == -1
                    ? "failed to run \(launchPath)" : text, status: status)
        } onCancel: {
            process.terminate()
            // SIGTERM is a request, and a child may decline it — this call goes on awaiting the
            // exit, so a timeout built on cancellation alone is no bound at all. Give it a couple
            // of seconds to leave on its own terms, then insist.
            DispatchQueue.global().asyncAfter(deadline: .now() + terminateGrace) {
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            }
        }
    }

    /// How long a cancelled child gets between SIGTERM and SIGKILL.
    static let terminateGrace = 2.0
    /// How long to keep reading output after the child has exited.
    static let drainGrace = 2.0

    /// Everything the child wrote, waiting no longer than `seconds` past its exit.
    ///
    /// Normally the pipe reaches EOF the moment the child exits and this returns at once. It is
    /// bounded because that is not guaranteed: anything the child spawned inherited the write end,
    /// so `sh -c 'sleep 30'` killed at the shell leaves the `sleep` holding the pipe open — and
    /// waiting on that is how a call cancelled after two seconds still took thirty. A cancelled
    /// task does not wait at all: by then nobody is reading this output.
    ///
    /// Giving up is not the same as walking away. A reader left behind holds the pipe open, keeps
    /// its waiter suspended, and — if the descendant goes on writing — grows the buffer with no
    /// one to empty it, for as long as that descendant lives. So whenever the reading has not
    /// finished on its own, it is cancelled and the handle closed: the closed handle ends the byte
    /// iteration with an error `drain` already tolerates, and everything holding onto it goes.
    private static func collect(
        _ output: Task<Void, Never>, from buffer: OutputBuffer,
        reading handle: FileHandle, within seconds: Double
    ) async -> String {
        if Task.isCancelled {
            stopReading(output, handle)
            return buffer.text
        }
        let gate = ResumeOnce()
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            // Unstructured, so nothing here is waited on once the other side has won the race.
            Task {
                await output.value
                gate.resume(continuation, as: .reader)
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + seconds) {
                gate.resume(continuation, as: .deadline)
            }
        }
        if gate.winner != .reader { stopReading(output, handle) }
        return buffer.text
    }

    private static func stopReading(_ output: Task<Void, Never>, _ handle: FileHandle) {
        output.cancel()
        // The iteration does not check cancellation; closing what it is reading does end it.
        try? handle.close()
    }

    private static func drain(_ handle: FileHandle, into buffer: OutputBuffer) async {
        defer { buffer.finish() }
        do {
            for try await byte in handle.bytes { buffer.append(byte) }
        } catch {
            // A read error still leaves whatever arrived worth reporting.
        }
    }

    /// What the child has written so far, and whether the reading of it ran to completion. Locked
    /// because the reading task and the call that gives up on it are different threads.
    final class OutputBuffer: @unchecked Sendable {
        private let lock = NSLock()
        private var data = Data()
        private var finished = false

        func append(_ byte: UInt8) {
            lock.lock()
            data.append(byte)
            lock.unlock()
        }

        func finish() {
            lock.lock()
            finished = true
            lock.unlock()
        }

        /// True once the reading task has returned — the evidence that an abandoned reader was
        /// actually released rather than merely stopped being waited for.
        var isFinished: Bool {
            lock.lock()
            defer { lock.unlock() }
            return finished
        }

        var text: String {
            lock.lock()
            defer { lock.unlock() }
            return String(data: data, encoding: .utf8) ?? ""
        }
    }

    /// Lets the reader and the deadline race for one continuation — resuming twice would trap —
    /// and records which of them won, because that is what says whether cleanup is still owed.
    private final class ResumeOnce: @unchecked Sendable {
        enum Winner { case none, reader, deadline }

        private let lock = NSLock()
        private var won = Winner.none

        var winner: Winner {
            lock.lock()
            defer { lock.unlock() }
            return won
        }

        func resume(_ continuation: CheckedContinuation<Void, Never>, as who: Winner) {
            lock.lock()
            let first = won == .none
            if first { won = who }
            lock.unlock()
            if first { continuation.resume() }
        }
    }

    // Reject endpoints the CLI cannot address; see controlEnvironmentRejectsEndpointsTheCLICannotControl.
    static func controlEnvironment(for url: URL) throws -> [String: String] {
        let validated = try Config.validateControlURL(url.absoluteString)
        return ["LYREBIRD_CONTROL_PORT": String(validated.port!)]
    }

    /// When a profile is configured it is passed explicitly: a Finder-launched app inherits no
    /// shell environment, so relying on `LYREBIRD_PROFILE` would silently pick the wrong profile.
    /// Unset means "let the engine use its default".
    static func arguments(_ command: [String], profile: String) -> [String] {
        (profile.isEmpty ? [] : ["--profile", profile]) + command
    }

    private static func lyrebird(_ command: [String]) async -> Result {
        do {
            let environment = try controlEnvironment(for: Config.controlURL.get())
            return await shell(
                Config.lyrebirdPath, arguments(command, profile: Config.profilePath), environment: environment)
        } catch {
            return Result(output: error.localizedDescription, status: -1)
        }
    }

    /// Why the app could not find out which profile it is configured for. Carries the CLI's own
    /// output, because that is where the reason lives — a wrong launcher path, a profile directory
    /// that is not one, a Python that will not start.
    struct ProfileUnknown: LocalizedError {
        var reason: String
        var errorDescription: String? { reason }
    }

    /// How long to wait for `status --json`. It shells out to `networksetup`, which can sit there
    /// for a while on a machine with an unhealthy network service, and a discovery that never
    /// returns would leave the menu in "profile unknown" with nothing to explain it.
    static let fingerprintTimeout = 15.0

    /// What `status --json` said: which profile the app is configured for, and whether the Mac's
    /// proxy settings point at a proxy that is no longer there.
    ///
    /// The fingerprint is `sha256(resolved profile dir)[:12]` in `config`, and it is the engine's
    /// to compute: the app cannot even see which directory it is when the profile is unset and
    /// the engine falls back to its own default. `status --json` prints it whatever the proxy is
    /// doing — it is config-derived, and the command exits 1 when not intercepting while still
    /// printing the JSON — so a down or foreign proxy answers this question just as well.
    ///
    /// The PAC fields are optional on purpose: the engine prints `pac: null` when `networksetup`
    /// could not be asked, and a reading that required them would turn a valid fingerprint into
    /// "profile unknown". A missing field is nil, never false — see `StaleSessionTests`.
    struct StatusReading: Sendable, Equatable {
        var fingerprint: String
        var proxyUp: Bool?
        var pacOurs: Bool?
        var pacEnabled: Bool?

        init(fingerprint: String, proxyUp: Bool? = nil, pacOurs: Bool? = nil, pacEnabled: Bool? = nil) {
            self.fingerprint = fingerprint
            self.proxyUp = proxyUp
            self.pacOurs = pacOurs
            self.pacEnabled = pacEnabled
        }

        /// Nothing answers the control port, and the PAC is enabled and points at it: the Mac is
        /// routed at a dead proxy until `down` runs. Only a reading that saw all three says so.
        var routesToADeadProxy: Bool {
            proxyUp == false && pacOurs == true && pacEnabled == true
        }
    }

    /// Runs `status --json` and reads it. Throws with the CLI's own words when it did not answer,
    /// or answered without naming a profile.
    static func statusReading() async throws -> StatusReading {
        guard let result = await lyrebird(["status", "--json"], within: fingerprintTimeout) else {
            throw ProfileUnknown(
                reason: "`lyrebird status --json` did not answer within "
                    + "\(Int(fingerprintTimeout))s")
        }
        return try statusReading(from: result)
    }

    /// Run a CLI command that only reads, giving up after `timeout`: nil when the timeout won.
    /// Shared by the two reads the app makes on its own schedule — the profile lookup and the file
    /// preview — so a launcher that never returns cannot hold either.
    private static func lyrebird(_ command: [String], within timeout: Double) async -> Result? {
        await withTaskGroup(of: Result?.self) { group -> Result? in
            group.addTask { await lyrebird(command) }
            group.addTask {
                try? await Task.sleep(for: .seconds(timeout))
                return nil  // the timeout won the race
            }
            defer { group.cancelAll() }  // cancelling the shell task terminates the child
            return await group.next() ?? nil
        }
    }

    /// The profile's scenarios read from files, as `lyrebird scenario show` prints them. Nil when
    /// the launcher did not answer in time; the caller decodes the rest, exit status included, since
    /// the command prints its JSON — and the reason for a nonzero exit — either way.
    static func preview() async -> Result? {
        await lyrebird(["scenario", "show"], within: fingerprintTimeout)
    }

    /// Everything from the first `{` to the last `}` of what a launcher printed, or nil when there
    /// is no such span. The launcher merges stderr into stdout, so a warning before or after the
    /// JSON is normal and is not a reason to report the payload as missing.
    static func jsonSpan(in output: String) -> Data? {
        guard let start = output.firstIndex(of: "{"), let end = output.lastIndex(of: "}"), start < end else {
            return nil
        }
        return Data(output[start...end].utf8)
    }

    /// The half of the lookup that decides what a finished `status --json` run means, kept apart
    /// from the half that runs it so a test can put a real payload through the shipped path.
    ///
    /// The exit status is deliberately not consulted: `status` exits 1 whenever this profile is
    /// not intercepting — proxy down, PAC off, another profile holding the port — and prints the
    /// same config-derived `profileFingerprint` either way. Requiring 0 here would leave the menu
    /// unable to name its own profile in exactly the situations it exists to explain.
    static func statusReading(from result: Result) throws -> StatusReading {
        guard let reading = statusReading(fromStatusJSON: Data(result.output.utf8)) else {
            let output = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            throw ProfileUnknown(
                reason: output.isEmpty
                    ? "`lyrebird status --json` printed nothing"
                    : output)
        }
        return reading
    }

    /// Pulls `profileFingerprint` — and, when printed, `proxyUp` and the `pac` object — out of
    /// what `status --json` printed.
    ///
    /// Lenient about what surrounds the object: the launcher merges stderr into stdout, so a
    /// warning printed before or after the JSON is normal and is not a reason to report the
    /// profile as unknown. Everything from the first `{` to the last `}` is offered to the
    /// decoder; nil means the fingerprint was not there, never a guess.
    static func statusReading(fromStatusJSON output: Data) -> StatusReading? {
        guard let text = String(data: output, encoding: .utf8), let span = jsonSpan(in: text) else { return nil }
        let object = (try? JSONSerialization.jsonObject(with: span)) as? [String: Any]
        guard let fingerprint = object?["profileFingerprint"] as? String, !fingerprint.isEmpty else { return nil }
        let pac = object?["pac"] as? [String: Any]
        return StatusReading(
            fingerprint: fingerprint, proxyUp: object?["proxyUp"] as? Bool,
            pacOurs: pac?["ours"] as? Bool, pacEnabled: pac?["enabled"] as? Bool)
    }

    static func up() async -> Result {
        await lyrebird(["up"])
    }

    static func down() async -> Result {
        await lyrebird(["down"])
    }

    /// The argv Relaunch runs. Named separately so a test can read it without starting anything.
    ///
    /// It goes through the CLI rather than `xcrun simctl` directly, and that is the whole point:
    /// `simctl launch booted` lets simctl pick when two simulators are booted, so the button could
    /// relaunch the app on a device that never got Lyrebird's CA — while the menu went on saying
    /// INTERCEPT ACTIVE. `lyrebird relaunch` uses the device `up` recorded, and refuses with its
    /// own message when there is no single device it can mean. The app never names `booted`.
    static func relaunchCommand(bundleId: String) -> [String] {
        ["relaunch", bundleId]
    }

    static func relaunch(bundleId: String) async -> Result {
        await lyrebird(relaunchCommand(bundleId: bundleId))
    }
}
