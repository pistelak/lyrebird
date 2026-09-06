import SwiftUI

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

    /// The status-dot colour, or nil (no dot) when there is nothing of ours to report.
    var dotColor: Color? {
        switch self {
        case .intercepting: return .green
        // Up, and not intercepting for this profile — the same thing to the user as a disabled
        // PAC, whatever the reason behind it.
        case .pacDisabled, .foreignProfile, .unreadable: return .orange
        case .down, .profileUnknown: return nil
        }
    }
}

/// A monochrome lyrebird glyph with a small coloured status dot (green / orange / none).
struct StatusGlyph: View {
    let status: AppModel.Status

    var body: some View {
        Image(systemName: status.symbolName)
            .overlay(alignment: .bottomTrailing) {
                if let color = status.dotColor {
                    Circle()
                        .fill(color)
                        .frame(width: 6, height: 6)
                        .alignmentGuide(.bottom) { $0[.bottom] + 2 }
                        .alignmentGuide(.trailing) { $0[.trailing] + 2 }
                }
            }
    }
}
