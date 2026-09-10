import AppKit

@MainActor
final class BrowserSplitView: NSSplitView {
    override func drawDivider(in rect: NSRect) {
        BrowserAppearance.pane.setFill()
        rect.fill()
        let content = window.map { convert($0.contentLayoutRect, from: nil) } ?? safeAreaRect
        let contentDivider = rect.intersection(content)
        if !contentDivider.isEmpty { super.drawDivider(in: contentDivider) }
    }
}
