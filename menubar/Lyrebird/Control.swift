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
    static func shell(_ launchPath: String, _ arguments: [String],
                      environment: [String: String] = [:]) async -> Result {
        guard !launchPath.isEmpty, FileManager.default.isExecutableFile(atPath: launchPath) else {
            return Result(output: launchPath.isEmpty
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

        return await withTaskCancellationHandler {
            async let output = drain(pipe.fileHandleForReading)

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

            let text = await output
            return Result(output: text.isEmpty && status == -1
                          ? "failed to run \(launchPath)" : text, status: status)
        } onCancel: {
            process.terminate()
        }
    }

    private static func drain(_ handle: FileHandle) async -> String {
        var data = Data()
        do {
            for try await byte in handle.bytes { data.append(byte) }
        } catch {
            // A read error still leaves whatever arrived worth reporting.
        }
        return String(data: data, encoding: .utf8) ?? ""
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
    private static func lyrebird(_ arguments: [String]) async -> Result {
        var argv: [String] = []
        let profile = Config.profilePath
        if !profile.isEmpty {
            argv += ["--profile", profile]
        }
        argv += arguments
        return await shell(Config.lyrebirdPath, argv,
                           environment: controlEnvironment(for: Config.controlURL))
    }

    static func up() async -> Result { await lyrebird(["up"]) }

    static func down() async -> Result { await lyrebird(["down"]) }

    static func relaunch(bundleId: String) async -> Result {
        _ = await shell("/usr/bin/xcrun", ["simctl", "terminate", "booted", bundleId])
        return await shell("/usr/bin/xcrun", ["simctl", "launch", "booted", bundleId])
    }
}
