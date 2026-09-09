import AppKit
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct DockPresenceTests {
        @Test(arguments: [0, 1, 2, 5])
        func dockPolicyFollowsThePreferenceAndOpenWindowCount(count: Int) {
            #expect(DockPresence.policy(forOpenWindows: count, dockOnlyWhileWindowOpen: false) == .regular)
            #expect(
                DockPresence.policy(forOpenWindows: count, dockOnlyWhileWindowOpen: true)
                    == (count > 0 ? .regular : .accessory))
        }

        @Test
        func closingTheLastWindowKeepsTheDockIconByDefault() async throws {
            try await withAppTestEnvironment {
                var applied: [NSApplication.ActivationPolicy] = []
                DockPresence.apply = { applied.append($0) }
                DockPresence.windowOpened()
                DockPresence.windowClosed()
                #expect(applied == [.regular, .regular])
            }
        }

        @Test func menuBarModeFollowsWindowOpeningAndClosing()
            async throws
        {
            try await withAppTestEnvironment {
                Config.defaults.set(true, forKey: Config.dockOnlyWhileWindowOpenKey)
                var applied: [NSApplication.ActivationPolicy] = []
                DockPresence.apply = { applied.append($0) }
                DockPresence.windowOpened()
                #expect(applied == [.regular])
                DockPresence.windowClosed()
                #expect(applied == [.regular, .accessory])
            }
        }

        @Test func closingOneOfTwoWindowsPreservesTheDockIcon()
            async throws
        {
            try await withAppTestEnvironment {
                Config.defaults.set(true, forKey: Config.dockOnlyWhileWindowOpenKey)
                var applied: [NSApplication.ActivationPolicy] = []
                DockPresence.apply = { applied.append($0) }
                DockPresence.windowOpened()
                DockPresence.windowOpened()
                applied = []
                DockPresence.windowClosed()
                #expect(applied == [.regular])
                #expect(DockPresence.openWindows == 1)
            }
        }

        @Test
        func closingAnUncountedWindowDoesNotDelayTheNextDockAppearance() async throws {
            try await withAppTestEnvironment {
                Config.defaults.set(true, forKey: Config.dockOnlyWhileWindowOpenKey)
                var applied: [NSApplication.ActivationPolicy] = []
                DockPresence.apply = { applied.append($0) }
                DockPresence.windowClosed()
                applied = []
                DockPresence.windowOpened()
                #expect(applied == [.regular])
                #expect(DockPresence.openWindows == 1)
            }
        }

        @Test
        func enablingMenuBarModeWithNoWindowsHidesTheDockIcon() async throws {
            try await withAppTestEnvironment {
                var applied: [NSApplication.ActivationPolicy] = []
                DockPresence.apply = { applied.append($0) }
                Config.defaults.set(true, forKey: Config.dockOnlyWhileWindowOpenKey)
                DockPresence.settingChanged()
                #expect(applied == [.accessory])
            }
        }

        @Test func disablingMenuBarModeRestoresTheDockIcon()
            async throws
        {
            try await withAppTestEnvironment {
                Config.defaults.set(true, forKey: Config.dockOnlyWhileWindowOpenKey)
                var applied: [NSApplication.ActivationPolicy] = []
                DockPresence.apply = { applied.append($0) }
                DockPresence.settingChanged()
                applied = []
                Config.defaults.set(false, forKey: Config.dockOnlyWhileWindowOpenKey)
                DockPresence.settingChanged()
                #expect(applied == [.regular])
            }
        }

        @Test
        func enablingMenuBarModeKeepsAnOpenWindowInTheDock() async throws {
            try await withAppTestEnvironment {
                DockPresence.windowOpened()
                var applied: [NSApplication.ActivationPolicy] = []
                DockPresence.apply = { applied.append($0) }
                Config.defaults.set(true, forKey: Config.dockOnlyWhileWindowOpenKey)
                DockPresence.settingChanged()
                #expect(applied == [.regular])
            }
        }
    }
}
