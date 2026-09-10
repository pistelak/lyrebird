import AppKit

@MainActor
final class BrowserTableRow: NSTableRowView {
    var separates = false

    override func drawSelection(in dirtyRect: NSRect) {
        (isEmphasized ? NSColor.selectedContentBackgroundColor : NSColor.unemphasizedSelectedContentBackgroundColor)
            .setFill()
        // Rounding the visible slice creates scroll seams; see selectionShapeDoesNotChangeWhenARowIsPartiallyVisible.
        NSBezierPath(roundedRect: bounds.insetBy(dx: 8, dy: 1), xRadius: 8, yRadius: 8).fill()
    }

    override func drawBackground(in dirtyRect: NSRect) {
        super.drawBackground(in: dirtyRect)
        guard separates, !isSelected else { return }
        NSColor.separatorColor.setFill()
        NSRect(x: 22, y: bounds.height - 1, width: max(0, bounds.width - 36), height: 0.5).fill()
    }
}
