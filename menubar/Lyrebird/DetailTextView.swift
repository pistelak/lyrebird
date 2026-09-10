import AppKit

extension NSAttributedString.Key {
    static let detailCard = NSAttributedString.Key("LyrebirdDetailCard")
    static let detailBadge = NSAttributedString.Key("LyrebirdDetailBadge")
    static let detailSeparator = NSAttributedString.Key("LyrebirdDetailSeparator")
    static let detailHeaderRow = NSAttributedString.Key("LyrebirdDetailHeaderRow")
}

/// Glyph-relative decorations avoid content-dependent window sizes; see panesStayBelowToolbarAcrossEmptyAndLongContent.
final class DetailLayoutManager: NSLayoutManager {
    func drawDecorations(in dirtyRect: NSRect, at origin: NSPoint) {
        guard let storage = textStorage, let container = textContainers.first else {
            return
        }
        storage.enumerateAttribute(.detailCard, in: NSRange(location: 0, length: storage.length)) { value, range, _ in
            guard value != nil else { return }
            let glyphs = self.glyphRange(forCharacterRange: range, actualCharacterRange: nil)
            let content = self.boundingRect(forGlyphRange: glyphs, in: container)
            let rect = NSRect(
                x: origin.x, y: origin.y + content.minY - 9,
                width: container.size.width, height: content.height + 18)
            guard rect.intersects(dirtyRect) else { return }
            BrowserAppearance.card.setFill()
            NSBezierPath(roundedRect: rect, xRadius: 10, yRadius: 10).fill()
        }
        let visibleGlyphs = glyphRange(
            forBoundingRect: dirtyRect.insetBy(dx: -12, dy: -12).offsetBy(dx: -origin.x, dy: -origin.y), in: container)
        let visible = characterRange(forGlyphRange: visibleGlyphs, actualGlyphRange: nil)
        storage.enumerateAttributes(in: visible) { attributes, range, _ in
            let rect = self.boundingRect(
                forGlyphRange: self.glyphRange(forCharacterRange: range, actualCharacterRange: nil), in: container
            ).offsetBy(dx: origin.x, dy: origin.y)
            if let color = attributes[.detailBadge] as? NSColor {
                color.withAlphaComponent(0.14).setFill()
                NSBezierPath(roundedRect: rect.insetBy(dx: -2, dy: -1), xRadius: 4, yRadius: 4).fill()
            }
            if attributes[.detailSeparator] != nil {
                NSColor.separatorColor.setFill()
                NSRect(x: origin.x + 10, y: rect.minY + 4, width: max(0, container.size.width - 20), height: 0.5).fill()
            }
        }
    }
}

@MainActor
final class DetailTextView: NSTextView {
    var didLayout: (() -> Void)?
    override func accessibilityChildren() -> [Any]? {
        (super.accessibilityChildren() ?? []) + subviews.filter { $0 is NSButton && !$0.isHidden }
    }
    override var isOpaque: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        BrowserAppearance.pane.setFill()
        dirtyRect.fill()
        if let manager = layoutManager as? DetailLayoutManager, let container = textContainer {
            manager.ensureLayout(for: container)
            manager.drawDecorations(in: dirtyRect, at: textContainerOrigin)
        }
        super.draw(dirtyRect)
    }
    override func layout() {
        super.layout()
        didLayout?()
    }
    override func setFrameSize(_ newSize: NSSize) {
        let changed = newSize.width != frame.width
        super.setFrameSize(newSize)
        if changed {
            needsLayout = true
            needsDisplay = true
            enclosingScrollView?.contentView.needsDisplay = true
        }
    }
}
