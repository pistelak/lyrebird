import Foundation
import Testing

@testable import Lyrebird

/// Redirect hosted tests away from the user's settings and verify cleanup preserves them.
/// A fixed scratch domain avoids leaving one preferences file behind per test run.
enum TestDefaults {
    static let suiteName = "com.lyrebird.LyrebirdTests"
    static let ownedKeys = [Config.controlURLKey, Config.lyrebirdPathKey, Config.profilePathKey]
    private static var userDockPreference: Bool?
    private static var userValues: [String: String?] = [:]

    static func userValue(forKey key: String) -> String? { userValues[key] ?? nil }

    static func install() throws {
        let suite = try #require(UserDefaults(suiteName: suiteName), "Could not open the test preferences domain")
        userValues = Dictionary(
            uniqueKeysWithValues: ownedKeys.map { ($0, UserDefaults.standard.string(forKey: $0)) })
        userDockPreference = UserDefaults.standard.object(forKey: Config.dockOnlyWhileWindowOpenKey) as? Bool
        suite.removePersistentDomain(forName: suiteName)
        Config.defaults = suite
    }

    static func restore() {
        UserDefaults(suiteName: suiteName)?.removePersistentDomain(forName: suiteName)
        Config.defaults = .standard
        for key in ownedKeys {
            #expect(
                UserDefaults.standard.string(forKey: key) == userValues[key] ?? nil,
                "Test changed the user's \(key) setting")
        }
        #expect(
            UserDefaults.standard.object(forKey: Config.dockOnlyWhileWindowOpenKey) as? Bool == userDockPreference,
            "Test changed the user's Dock preference")
        userValues = [:]
        userDockPreference = nil
    }
}

extension AppTests {
    @MainActor
    struct TestDefaultsTests {
        @Test func testPreferencesLeaveUserSettingsUnchanged()
            async throws
        {
            try await withAppTestEnvironment {
                let key = Config.lyrebirdPathKey
                let sentinel = "/tmp/test-launcher"
                let userPath = TestDefaults.userValue(forKey: key)
                try #require(userPath != sentinel)
                Config.defaults.set(sentinel, forKey: key)
                #expect(Config.lyrebirdPath == sentinel)
                #expect(UserDefaults.standard.string(forKey: key) == userPath)
                Config.defaults.removeObject(forKey: key)
                #expect(UserDefaults.standard.string(forKey: key) == userPath)
            }
        }
    }
}
