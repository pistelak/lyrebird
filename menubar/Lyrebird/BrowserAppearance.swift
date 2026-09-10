import AppKit

enum BrowserAppearance {
    static let pane = appearanceColor(light: NSColor(white: 0.98, alpha: 1), dark: NSColor(white: 0.15, alpha: 1))
    static let card = appearanceColor(light: NSColor(white: 0.94, alpha: 1), dark: NSColor(white: 0.19, alpha: 1))
    static let sequence = appearanceColor(
        light: NSColor(red: 0.22, green: 0.43, blue: 0.41, alpha: 1),
        dark: NSColor(red: 0.38, green: 0.62, blue: 0.60, alpha: 1))
    static let jsonString = syntaxColor(light: (0.0, 0.38, 0.16), dark: (0.45, 0.84, 0.53))
    static let jsonNumber = syntaxColor(light: (0.0, 0.30, 0.66), dark: (0.48, 0.72, 1.0))
    static let jsonLiteral = syntaxColor(light: (0.52, 0.15, 0.60), dark: (0.85, 0.62, 0.94))

    private static func appearanceColor(light: NSColor, dark: NSColor) -> NSColor {
        NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
        }
    }

    private static func syntaxColor(light: (CGFloat, CGFloat, CGFloat), dark: (CGFloat, CGFloat, CGFloat)) -> NSColor {
        NSColor(name: nil) { appearance in
            let best = appearance.bestMatch(from: [
                .accessibilityHighContrastAqua, .accessibilityHighContrastDarkAqua,
                .aqua, .darkAqua,
            ])
            let isDark = best == .darkAqua || best == .accessibilityHighContrastDarkAqua
            if best == .accessibilityHighContrastAqua || best == .accessibilityHighContrastDarkAqua {
                return isDark ? .white : .black
            }
            let rgb = isDark ? dark : light
            return NSColor(srgbRed: rgb.0, green: rgb.1, blue: rgb.2, alpha: 1)
        }
    }

    static func tint(_ kind: RuleFormatting.ResponseKind) -> NSColor {
        switch kind {
        case .sequence: return sequence
        case .replace: return .systemBlue
        case .patch: return .systemOrange
        case .unknown: return .secondaryLabelColor
        }
    }
}
