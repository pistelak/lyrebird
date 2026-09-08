import AppKit
import XCTest

@testable import Lyrebird

/// Test Dock policy with isolated preferences and no process-level side effects.
@MainActor
final class DockPresenceTests: XCTestCase {

    private var applied: [NSApplication.ActivationPolicy] = []

    private var originalApply: ((NSApplication.ActivationPolicy) -> Void)?

    override func setUpWithError() throws {
        try super.setUpWithError()
        try TestDefaults.install()
        originalApply = DockPresence.apply
        DockPresence.reset()
        applied = []
        DockPresence.apply = { [self] policy in applied.append(policy) }
    }

    override func tearDown() {
        if let originalApply { DockPresence.apply = originalApply }
        DockPresence.reset()
        TestDefaults.restore()
        super.tearDown()
    }

    private func menuBarOnly(_ on: Bool) {
        Config.defaults.set(on, forKey: Config.dockOnlyWhileWindowOpenKey)
    }

    // MARK: - The default: a Dock icon whatever the windows do

    func testTheAppStaysInTheDockWithNoWindowOpenAndWithTwo() {
        // The default, and the reason it is the default: a menu bar extra is easy to miss, and the
        // HIG asks for the app to be reachable some other way as well.
        XCTAssertEqual(DockPresence.policy(forOpenWindows: 0, dockOnlyWhileWindowOpen: false), .regular)
        XCTAssertEqual(DockPresence.policy(forOpenWindows: 2, dockOnlyWhileWindowOpen: false), .regular)
    }

    func testOpeningAndClosingWindowsNeverLeavesTheDockByDefault() {
        DockPresence.windowOpened()
        DockPresence.windowClosed()

        XCTAssertEqual(applied, [.regular, .regular], "the last close took the Dock icon away")
    }

    // MARK: - The setting: the menu bar, with the Dock following the windows

    func testWithTheSettingOnTheDockFollowsTheWindows() {
        XCTAssertEqual(DockPresence.policy(forOpenWindows: 0, dockOnlyWhileWindowOpen: true), .accessory)
        XCTAssertEqual(DockPresence.policy(forOpenWindows: 1, dockOnlyWhileWindowOpen: true), .regular)
        XCTAssertEqual(DockPresence.policy(forOpenWindows: 5, dockOnlyWhileWindowOpen: true), .regular)
    }

    func testAWindowMakesTheAppRegularAndTheLastCloseMakesItAnAccessoryAgain() {
        menuBarOnly(true)

        DockPresence.windowOpened()
        XCTAssertEqual(applied, [.regular])

        DockPresence.windowClosed()
        XCTAssertEqual(applied, [.regular, .accessory], "the icon has to leave the Dock again")
    }

    func testOneCloseOfTwoWindowsLeavesTheAppInTheDock() {
        // Counting rather than toggling: a policy flipped per close would drop the surviving window
        // out of the Dock, and with it the menu bar carrying its shortcuts.
        menuBarOnly(true)
        DockPresence.windowOpened()
        DockPresence.windowOpened()
        applied = []

        DockPresence.windowClosed()

        XCTAssertEqual(applied, [.regular])
        XCTAssertEqual(DockPresence.openWindows, 1)
    }

    func testACloseWithNothingOpenDoesNotLeaveTheAppInTheDock() {
        // `onDisappear` can arrive for a window that never counted — a scene rebuilt under it, say —
        // and a count allowed below zero would need two opens before the next window showed up.
        menuBarOnly(true)
        DockPresence.windowClosed()
        applied = []

        DockPresence.windowOpened()

        XCTAssertEqual(applied, [.regular])
        XCTAssertEqual(DockPresence.openWindows, 1)
    }

    // MARK: - Editing the setting

    func testTurningTheSettingOnWithNoWindowOpenDropsToTheMenuBarAtOnce() {
        // Without this the change would not take effect until the next window opened and closed,
        // which reads as a setting that did nothing.
        menuBarOnly(true)

        DockPresence.settingChanged()

        XCTAssertEqual(applied, [.accessory])
    }

    func testTurningTheSettingOffBringsTheDockIconBackAtOnce() {
        menuBarOnly(true)
        DockPresence.settingChanged()
        applied = []

        menuBarOnly(false)
        DockPresence.settingChanged()

        XCTAssertEqual(applied, [.regular])
    }

    func testTurningTheSettingOnWithAWindowOpenKeepsTheDockIcon() {
        // The window is still on screen, and it is what the Dock icon is for while the setting is on.
        menuBarOnly(true)
        DockPresence.windowOpened()
        applied = []

        DockPresence.settingChanged()

        XCTAssertEqual(applied, [.regular])
    }
}
