import AppKit

@MainActor
final class BrowserSurface: NSView {
    let color: NSColor

    init(_ color: NSColor) {
        self.color = color
        super.init(frame: .zero)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func draw(_ dirtyRect: NSRect) {
        color.setFill()
        bounds.fill()
    }

    override func viewDidChangeEffectiveAppearance() {
        needsDisplay = true
    }
}
