import Foundation

enum Config {
    static let controlURLKey = "controlURL"
    static let lyrebirdPathKey = "lyrebirdPath"
    static let profilePathKey = "profilePath"
    static let pollKey = "pollSeconds"

    static let defaultControlURL = "http://127.0.0.1:8088"

    private static func string(_ key: String, default fallback: String) -> String {
        let value = UserDefaults.standard.string(forKey: key)
        return (value?.isEmpty == false) ? value! : fallback
    }

    static var controlURL: URL {
        URL(string: string(controlURLKey, default: defaultControlURL))
            ?? URL(string: defaultControlURL)!
    }

    /// Resolved by searching PATH when unset, so a clone anywhere still works. There is no default
    /// checkout location — assuming one would only fail confusingly on someone else's Mac.
    ///
    /// A GUI app launched from Finder gets a minimal PATH, so the usual install directories are
    /// searched too. `Process` does not search PATH itself, so this must resolve to a full path.
    static var lyrebirdPath: String {
        let configured = string(lyrebirdPathKey, default: "")
        if !configured.isEmpty { return configured }

        let fromPath = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        let fallbacks = [
            "/usr/local/bin",
            "/opt/homebrew/bin",
            "\(NSHomeDirectory())/.local/bin",
            "\(NSHomeDirectory())/bin",
        ]
        for directory in fromPath + fallbacks {
            let candidate = (directory as NSString).appendingPathComponent("lyrebird")
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return ""   // empty → Control.shell reports a clear "not found" instead of failing opaquely
    }

    /// The profile directory. Empty means "let the engine use its own default".
    static var profilePath: String {
        string(profilePathKey, default: "")
    }

    static var pollSeconds: Double {
        let value = UserDefaults.standard.double(forKey: pollKey)
        return value > 0 ? value : 2.0
    }
}
