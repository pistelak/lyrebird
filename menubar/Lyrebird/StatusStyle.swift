import AppKit

extension AppModel.Status {
    /// Bird outline when the menu is reading no proxy of ours, filled when it is — a native
    /// template glyph for the menu bar. An unreadable or foreign proxy is filled: something is
    /// there, and the dot says it is not usable.
    var symbolName: String {
        switch self {
        case .intercepting, .pacDisabled, .foreignProfile, .unreadable: return "bird.fill"
        case .down, .profileUnknown: return "bird"
        }
    }

    /// Compact toolbar label, preserving each connection state.
    var word: String {
        switch self {
        case .intercepting: return "intercepting"
        case .pacDisabled: return "not intercepting"
        case .down: return "stopped"
        case .foreignProfile: return "another profile"
        case .unreadable: return "unreadable"
        case .profileUnknown: return "profile unknown"
        }
    }

    /// The status-dot colour, or nil (no dot) when there is nothing of ours to report.
    var dotColor: NSColor? {
        switch self {
        case .intercepting: return .systemGreen
        // Up, and not intercepting for this profile — the same thing to the user as a disabled
        // PAC, whatever the reason behind it.
        case .pacDisabled, .foreignProfile, .unreadable: return .systemOrange
        case .down, .profileUnknown: return nil
        }
    }
}
