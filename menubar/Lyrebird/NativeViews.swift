import AppKit

@MainActor
enum NativeStyle {
    static func label(_ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = .systemFont(ofSize: size, weight: weight)
        field.lineBreakMode = .byTruncatingMiddle
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return field
    }

    static func pin(_ child: NSView, in parent: NSView, inset: CGFloat = 0) {
        child.translatesAutoresizingMaskIntoConstraints = false
        parent.addSubview(child)
        NSLayoutConstraint.activate([
            child.leadingAnchor.constraint(equalTo: parent.leadingAnchor, constant: inset),
            child.trailingAnchor.constraint(equalTo: parent.trailingAnchor, constant: -inset),
            child.topAnchor.constraint(equalTo: parent.topAnchor, constant: inset),
            child.bottomAnchor.constraint(equalTo: parent.bottomAnchor, constant: -inset),
        ])
    }

    static func toolbarSeparator(in parent: NSView) {
        let separator = NSBox()
        separator.boxType = .separator
        separator.identifier = .init("toolbar-content-separator")
        separator.translatesAutoresizingMaskIntoConstraints = false
        parent.addSubview(separator)
        NSLayoutConstraint.activate([
            separator.topAnchor.constraint(equalTo: parent.safeAreaLayoutGuide.topAnchor),
            separator.leadingAnchor.constraint(equalTo: parent.leadingAnchor),
            separator.trailingAnchor.constraint(equalTo: parent.trailingAnchor),
            separator.heightAnchor.constraint(equalToConstant: 1),
        ])
    }

    static func scroll(_ document: NSView) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.documentView = document
        return scroll
    }

    static func text(
        _ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular,
        color: NSColor = .labelColor, mono: Bool = false
    ) -> NSAttributedString {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byWordWrapping
        paragraph.paragraphSpacing = 5
        return NSAttributedString(
            string: text,
            attributes: [
                .font: mono
                    ? NSFont.monospacedSystemFont(ofSize: size, weight: weight)
                    : NSFont.systemFont(ofSize: size, weight: weight),
                .foregroundColor: color, .paragraphStyle: paragraph,
            ])
    }
}

@MainActor
final class ActionButton: NSButton {
    var invoke: () -> Void

    init(_ title: String, action: @escaping () -> Void) {
        invoke = action
        super.init(frame: .zero)
        self.title = title
        bezelStyle = .rounded
        target = self
        self.action = #selector(performAction)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    @objc private func performAction() { invoke() }
}

@MainActor
final class TextCell: NSTableCellView {
    let label = NSTextField(wrappingLabelWithString: "")

    override init(frame: NSRect) {
        super.init(frame: frame)
        label.maximumNumberOfLines = 0
        label.isSelectable = false
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        NativeStyle.pin(label, in: self, inset: 8)
        textField = label
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
}
