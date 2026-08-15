import SwiftUI

extension AppModel.Status {
    /// Bird outline when idle, filled when the proxy is up — a native template glyph for the menu bar.
    var symbolName: String { self == .down ? "bird" : "bird.fill" }

    /// The status-dot colour, or nil (no dot) when stopped.
    var dotColor: Color? {
        switch self {
        case .intercepting: return .green
        case .pacDisabled: return .orange
        case .down: return nil
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
