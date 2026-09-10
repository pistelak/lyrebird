import AppKit

@MainActor
enum BrowserTypography {
    static let badge = NSFont.systemFont(ofSize: 11, weight: .semibold)

    static let code = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)

    static let flowPath = NSFont.monospacedSystemFont(ofSize: 13, weight: .medium)

    static let flowSubtitle = NSFont.systemFont(ofSize: 12)
}
