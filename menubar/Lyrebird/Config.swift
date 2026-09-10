import Foundation

enum Config {
    static let controlURLKey = "controlURL"

    static let lyrebirdPathKey = "lyrebirdPath"

    static let profilePathKey = "profilePath"

    static let dockOnlyWhileWindowOpenKey = "dockOnlyWhileWindowOpen"

    static let defaultControlURL = "http://127.0.0.1:8088"

    /// The store every setting is read from, and the one `SettingsWindowController` writes to.
    ///
    /// A `var` only so the tests can point it somewhere else: they run hosted inside Lyrebird.app,
    /// so `UserDefaults.standard` in a test is the user's real `com.lyrebird.Lyrebird` domain — a
    /// suite that set a path and tidied up after itself deleted the launcher and profile paths the
    /// user had typed into Settings, and the app came up "profile unknown" after every `make check`.
    /// See `TestDefaults`.
    static var defaults: UserDefaults = .standard

    private static func string(_ key: String, default fallback: String) -> String {
        let value = defaults.string(forKey: key)
        return (value?.isEmpty == false) ? value! : fallback
    }

    struct ControlURLProblem {}

    static func validateControlURL(_ text: String) throws -> URL {
        guard let url = URL(string: text, encodingInvalidCharacters: false),
            let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
            parts.scheme?.lowercased() == "http",
            ["127.0.0.1", "localhost"].contains(parts.host?.lowercased() ?? ""),
            let port = parts.port, (1...65535).contains(port),
            parts.user == nil, parts.password == nil,
            parts.path.isEmpty || parts.path == "/", parts.query == nil, parts.fragment == nil
        else {
            throw ControlURLProblem()
        }
        // Resolved to the address the CLI uses, not left as written: `localhost` may resolve to ::1,
        // and the CLI is handed only a port and always addresses 127.0.0.1 — so reads could describe
        // a listener that Stop would not stop.
        // See aLocalhostControlURLReadsTheSameEndpointTheCLIStops.
        guard var resolved = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw ControlURLProblem()
        }
        resolved.host = "127.0.0.1"
        guard let address = resolved.url else { throw ControlURLProblem() }
        return address
    }

    // Invalid preferences must not select another endpoint; see invalidPersistedControlURLIsReportedWithoutIO.
    static var controlURL: Result<URL, ControlURLProblem> {
        guard defaults.object(forKey: controlURLKey) != nil else {
            return .success(URL(string: defaultControlURL)!)
        }
        guard let text = defaults.string(forKey: controlURLKey) else {
            return .failure(ControlURLProblem())
        }
        do {
            return .success(try validateControlURL(text))
        } catch {
            return .failure(ControlURLProblem())
        }
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

    /// Whether Lyrebird leaves the Dock when its last window closes. Off by default: a menu bar
    /// extra is easy to miss, and the HIG asks for the app's functionality to be reachable some
    /// other way too. See `DockPresence`.
    static var dockOnlyWhileWindowOpen: Bool {
        defaults.bool(forKey: dockOnlyWhileWindowOpenKey)
    }

    /// The menu re-reads health, scenarios and recent traffic at this interval.
    static let pollSeconds = 2.0
}

extension Config.ControlURLProblem: LocalizedError {
    var errorDescription: String? {
        "Invalid control URL: use http://127.0.0.1:PORT or http://localhost:PORT (1–65535) in Settings."
    }
}

extension Config.ControlURLProblem: Equatable {}
