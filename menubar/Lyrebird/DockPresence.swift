import AppKit

/// Applies the Dock preference while keeping open windows in the foreground app.
@MainActor
enum DockPresence {
    private(set) static var openWindows = 0

    /// Injectable so hosted tests do not change the test runner's activation policy.
    static var apply: (NSApplication.ActivationPolicy) -> Void = { policy in
        let wasAccessory = NSApp.activationPolicy() == .accessory
        NSApp.setActivationPolicy(policy)
        guard policy == .regular else { return }
        guard wasAccessory else {
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        // Coming back from accessory, the new menu bar is drawn but takes no clicks until the app is
        // switched away from and back — FB7743313; activating a moment later is the documented way
        // round it, and only this transition needs it.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    static func policy(forOpenWindows count: Int, dockOnlyWhileWindowOpen: Bool) -> NSApplication.ActivationPolicy {
        guard dockOnlyWhileWindowOpen else { return .regular }
        return count > 0 ? .regular : .accessory
    }

    static func windowOpened() {
        openWindows += 1
        applyCurrent()
    }

    static func windowClosed() {
        openWindows = max(0, openWindows - 1)  // `onDisappear` can arrive for a window that never counted
        applyCurrent()
    }

    static func settingChanged() { applyCurrent() }

    private static func applyCurrent() {
        apply(policy(forOpenWindows: openWindows, dockOnlyWhileWindowOpen: Config.dockOnlyWhileWindowOpen))
    }

    /// For a test to start from a known state; the app never calls it.
    static func reset() { openWindows = 0 }
}
