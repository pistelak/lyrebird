import Foundation

enum Config {
    static let controlURLKey = "controlURL"
    static let lyrebirdPathKey = "lyrebirdPath"
    static let profilePathKey = "profilePath"

    static let defaultControlURL = "http://127.0.0.1:8088"

    /// The store every setting is read from, and the one `SettingsView` writes to.
    ///
    /// A `var` only so the tests can point it somewhere else: they run hosted inside Lyrebird.app,
    /// so `UserDefaults.standard` in a test is the user's real `com.lyrebird.Lyrebird` domain — a
    /// suite that set a path and tidied up after itself deleted the launcher and profile paths the
    /// user had typed into Settings, and the app came up "profile unknown" after every `make check`.
    /// See testARunOfTheSuiteLeavesTheUsersOwnSettingsAlone.
    static var defaults: UserDefaults = .standard

    private static func string(_ key: String, default fallback: String) -> String {
        let value = defaults.string(forKey: key)
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
        return ""  // empty → Control.shell reports a clear "not found" instead of failing opaquely
    }

    /// The profile directory. Empty means "let the engine use its own default".
    static var profilePath: String {
        string(profilePathKey, default: "")
    }

    /// The menu re-reads health, scenarios and recent traffic at this interval.
    static let pollSeconds = 2.0
}
