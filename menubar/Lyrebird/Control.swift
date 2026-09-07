import Foundation

/// Shell-outs to the `lyrebird` CLI and `xcrun simctl` — the app drives the engine, never duplicates it.
enum Control {
    struct Result {
        var output: String
        var status: Int32
        var succeeded: Bool { status == 0 }
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
        readerStarted: ((OutputBuffer) -> Void)? = nil
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

    /// The control port the CLI must use, taken from the URL the app itself reads.
    ///
    /// The engine takes that port from the environment, not from a flag, so Start and Stop would
    /// otherwise drive the default 8088 while the menu's own reads went to whatever Settings holds
    /// — one menu reporting on one proxy and starting another. A URL with no port means the scheme
    /// default; that is a real port, not a stand-in for 8088, and guessing 8088 instead would put
    /// the two halves back out of step for anyone who typed a bare host.
    static func controlEnvironment(for url: URL) -> [String: String] {
        let port = url.port ?? (url.scheme == "https" ? 443 : 80)
        return ["LYREBIRD_CONTROL_PORT": String(port)]
    }

    /// When a profile is configured it is passed explicitly: a Finder-launched app inherits no
    /// shell environment, so relying on `LYREBIRD_PROFILE` would silently pick the wrong profile.
    /// Unset means "let the engine use its default".
    static func arguments(_ command: [String], profile: String) -> [String] {
        (profile.isEmpty ? [] : ["--profile", profile]) + command
    }

    private static func lyrebird(_ command: [String]) async -> Result {
        return await shell(
            Config.lyrebirdPath, arguments(command, profile: Config.profilePath),
            environment: controlEnvironment(for: Config.controlURL))
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

    /// The fingerprint of the profile the app is configured for, straight from the engine.
    ///
    /// It is `sha256(resolved profile dir)[:12]` in `config`, and it is the engine's to compute:
    /// the app cannot even see which directory it is when the profile is unset and the engine
    /// falls back to its own default. `status --json` prints the field whatever the proxy is
    /// doing — it is config-derived, and the command exits 1 when not intercepting while still
    /// printing the JSON — so a down or foreign proxy answers this question just as well.
    static func fingerprint() async throws -> String {
        let result = try await withThrowingTaskGroup(of: Result?.self) { group -> Result in
            group.addTask { await lyrebird(["status", "--json"]) }
            group.addTask {
                try await Task.sleep(for: .seconds(fingerprintTimeout))
                return nil  // the timeout won the race
            }
            defer { group.cancelAll() }  // cancelling the shell task terminates the child
            guard let first = try await group.next() else {
                throw ProfileUnknown(reason: "the profile lookup produced no result at all")
            }
            guard let result = first else {
                throw ProfileUnknown(
                    reason: "`lyrebird status --json` did not answer within "
                        + "\(Int(fingerprintTimeout))s")
            }
            return result
        }

        return try fingerprint(from: result)
    }

    /// The half of the lookup that decides what a finished `status --json` run means, kept apart
    /// from the half that runs it so a test can put a real payload through the shipped path.
    ///
    /// The exit status is deliberately not consulted: `status` exits 1 whenever this profile is
    /// not intercepting — proxy down, PAC off, another profile holding the port — and prints the
    /// same config-derived `profileFingerprint` either way. Requiring 0 here would leave the menu
    /// unable to name its own profile in exactly the situations it exists to explain.
    static func fingerprint(from result: Result) throws -> String {
        guard let fingerprint = fingerprint(fromStatusJSON: Data(result.output.utf8)) else {
            let output = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            throw ProfileUnknown(
                reason: output.isEmpty
                    ? "`lyrebird status --json` printed nothing"
                    : output)
        }
        return fingerprint
    }

    /// Pulls `profileFingerprint` out of what `status --json` printed.
    ///
    /// Lenient about what surrounds the object: the launcher merges stderr into stdout, so a
    /// warning printed before or after the JSON is normal and is not a reason to report the
    /// profile as unknown. Everything from the first `{` to the last `}` is offered to the
    /// decoder; nil means the field was not there, never a guess.
    static func fingerprint(fromStatusJSON output: Data) -> String? {
        guard let text = String(data: output, encoding: .utf8),
            let start = text.firstIndex(of: "{"),
            let end = text.lastIndex(of: "}"), start < end
        else { return nil }
        let object = try? JSONSerialization.jsonObject(with: Data(text[start...end].utf8))
        guard let fingerprint = (object as? [String: Any])?["profileFingerprint"] as? String,
            !fingerprint.isEmpty
        else { return nil }
        return fingerprint
    }

    static func up() async -> Result { await lyrebird(["up"]) }

    static func down() async -> Result { await lyrebird(["down"]) }

    /// The argv Relaunch runs. Named separately so a test can read it without starting anything.
    ///
    /// It goes through the CLI rather than `xcrun simctl` directly, and that is the whole point:
    /// `simctl launch booted` lets simctl pick when two simulators are booted, so the button could
    /// relaunch the app on a device that never got Lyrebird's CA — while the menu went on saying
    /// INTERCEPT ACTIVE. `lyrebird relaunch` uses the device `up` recorded, and refuses with its
    /// own message when there is no single device it can mean. The app never names `booted`.
    static func relaunchCommand(bundleId: String) -> [String] { ["relaunch", bundleId] }

    static func relaunch(bundleId: String) async -> Result {
        await lyrebird(relaunchCommand(bundleId: bundleId))
    }
}
