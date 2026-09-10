import AppKit

@MainActor
final class BadgeView: NSView {
    let label: NSTextField
    let tint: NSColor?
    let circle: Bool
    var highlighted = false { didSet { needsDisplay = true } }

    init(_ text: String, tint: NSColor? = nil, circle: Bool = false) {
        self.tint = tint
        self.circle = circle
        label = NativeStyle.label(text, size: circle ? 11 : 11, weight: circle ? .regular : .semibold)
        super.init(frame: .zero)
        label.textColor = tint ?? (circle ? .secondaryLabelColor : .labelColor)
        label.alignment = .center
        label.setContentCompressionResistancePriority(.required, for: .horizontal)
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: centerXAnchor),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            widthAnchor.constraint(equalToConstant: circle ? 23 : ceil(label.intrinsicContentSize.width) + 10),
            heightAnchor.constraint(equalToConstant: circle ? 23 : 18),
        ])
        setAccessibilityElement(true)
        setAccessibilityLabel(text)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 0.5, dy: 0.5)
        if circle {
            (highlighted ? NSColor.alternateSelectedControlTextColor : NSColor.separatorColor).setStroke()
            NSBezierPath(ovalIn: rect).stroke()
        } else {
            (highlighted
                ? NSColor.alternateSelectedControlTextColor.withAlphaComponent(0.14)
                : tint?.withAlphaComponent(0.14) ?? NSColor.labelColor.withAlphaComponent(0.12)).setFill()
            NSBezierPath(roundedRect: rect, xRadius: 4, yRadius: 4).fill()
        }
    }

    override func viewDidChangeEffectiveAppearance() {
        needsDisplay = true
    }
}
