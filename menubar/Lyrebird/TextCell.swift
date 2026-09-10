import AppKit

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
