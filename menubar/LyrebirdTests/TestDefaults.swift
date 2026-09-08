import XCTest

@testable import Lyrebird

/// A preferences domain of the suite's own, because the alternative is the user's.
///
/// These tests are hosted *inside* Lyrebird.app, so `UserDefaults.standard` in a test is the real
/// `com.lyrebird.Lyrebird` domain — the same one the menu bar app reads. Tests that set a launcher
/// or profile path and removed it again in `tearDown` were therefore deleting whatever the user had
/// typed into Settings, and the app came up "profile unknown: lyrebird not found" after every
/// `make check`.
///
/// `install()` points `Config.defaults` at a scratch suite and remembers what the user's own domain
/// held; `restore()` wipes the scratch suite, puts `Config.defaults` back, and *checks* that the
/// three keys in the user's domain are exactly as they were. The check lives here rather than in one
/// test so that every class using the helper enforces it, including ones written later.
///
/// The suite name is fixed rather than qualified per process: `cfprefsd` writes a domain's file back
/// out as the process exits, after anything this code can do, so a per-run name left one empty plist
/// in the user's Preferences folder for every `make check`, while one fixed name is simply wiped by
/// the next run. Two hosted test runs at once would share it, but they already share
/// `menubar/.build` and the `Lyrebird` process name, so the suite is not the weakest link there.
enum TestDefaults {
    /// Not `com.lyrebird.Lyrebird`: a suite named after the app is the app's own domain, which is
    /// the thing being protected.
    static let suiteName = "com.lyrebird.LyrebirdTests"

    /// The settings that belong to the person running the tests, and that no test may disturb.
    static let ownedKeys = [Config.controlURLKey, Config.lyrebirdPathKey, Config.profilePathKey]

    private static var userValues: [String: String?] = [:]

    /// What the user's own domain held for `key` when `install()` ran. Read-only: a test asserts
    /// against it, never writes it back.
    static func userValue(forKey key: String) -> String? { userValues[key] ?? nil }

    /// Throws rather than merely failing: an `XCTFail` lets the test body run on, and the store it
    /// would run against is `.standard` — the user's own Settings, which is the damage this exists
    /// to prevent. `setUpWithError` stops the test instead.
    static func install(file: StaticString = #filePath, line: UInt = #line) throws {
        let suite = try XCTUnwrap(
            UserDefaults(suiteName: suiteName),
            "could not open the '\(suiteName)' defaults suite", file: file, line: line)
        // Recorded before anything is redirected, so `restore` compares against the domain as the
        // user left it rather than against whatever a previous test class saw.
        userValues = Dictionary(
            uniqueKeysWithValues: ownedKeys.map { ($0, UserDefaults.standard.string(forKey: $0)) })
        // Wiped going in as well as coming out: a suite left dirty by a run that was killed before
        // `restore` would otherwise hand the next one a profile path it never set.
        suite.removePersistentDomain(forName: suiteName)
        Config.defaults = suite
    }

    static func restore(file: StaticString = #filePath, line: UInt = #line) {
        UserDefaults(suiteName: suiteName)?.removePersistentDomain(forName: suiteName)
        Config.defaults = .standard
        for key in ownedKeys {
            XCTAssertEqual(
                UserDefaults.standard.string(forKey: key), userValues[key] ?? nil,
                "the suite changed the user's own '\(key)' — that is their Settings, not a fixture",
                file: file, line: line)
        }
        userValues = [:]
    }
}

/// The helper checking itself: the rest of the suite only proves it does not disturb keys it happens
/// not to touch, and the failure this exists to prevent was a test that touched them deliberately.
final class TestDefaultsTests: XCTestCase {

    override func setUpWithError() throws {
        try super.setUpWithError()
        try TestDefaults.install()
    }

    override func tearDown() {
        TestDefaults.restore()
        super.tearDown()
    }

    func testARunOfTheSuiteLeavesTheUsersOwnSettingsAlone() throws {
        let key = Config.lyrebirdPathKey
        let sentinel = "/tmp/a-path-no-test-may-leave-in-the-users-settings"
        let userPath = TestDefaults.userValue(forKey: key)
        XCTAssertNotEqual(userPath, sentinel, "the sentinel must be a value the user cannot already have")

        Config.defaults.set(sentinel, forKey: key)

        // Both halves matter: the write has to land somewhere the app will read — otherwise the
        // redirection has quietly disabled every test that configures a path — and it must not land
        // in the domain the menu bar app comes back to.
        XCTAssertEqual(Config.lyrebirdPath, sentinel, "Config no longer reads what the tests set")
        XCTAssertEqual(
            UserDefaults.standard.string(forKey: key), userPath,
            "a test's launcher path was written into the user's own Settings")

        // And removal, which is the half that actually did the damage: `removeObject` against the
        // real domain is how the user's typed-in paths disappeared on every `make check`.
        Config.defaults.removeObject(forKey: key)

        XCTAssertEqual(
            UserDefaults.standard.string(forKey: key), userPath,
            "clearing a test's launcher path deleted the user's")
    }
}
