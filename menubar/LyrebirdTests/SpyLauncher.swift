import Foundation

@testable import Lyrebird

/// A launcher that records what it was asked to run, and the control port it was handed, then
/// exits as told. Installed as `Config.lyrebirdPath`, so a test drives the real `Control` call —
/// the process, the argv, the environment — and reads back what actually reached the child.
///
/// Each line of `calls` is `<argv joined by spaces> port=<LYREBIRD_CONTROL_PORT>`.
@MainActor
func spyLauncher(exiting code: Int) throws -> (path: String, calls: URL) {
    let dir = FileManager.default.temporaryDirectory
        .appendingPathComponent("lyrebird-launcher-\(ProcessInfo.processInfo.globallyUniqueString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let calls = dir.appendingPathComponent("calls")
    let script = dir.appendingPathComponent("lyrebird")
    let body =
        "#!/bin/sh\nprintf '%s port=%s\\n' \"$*\" \"$LYREBIRD_CONTROL_PORT\" >> \"\(calls.path)\"\nexit \(code)\n"
    try body.write(to: script, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
    Config.defaults.set(script.path, forKey: Config.lyrebirdPathKey)
    return (script.path, calls)
}

func recordedCalls(_ calls: URL) -> String {
    (try? String(contentsOf: calls, encoding: .utf8)) ?? ""
}
