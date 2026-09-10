import AppKit

@MainActor
final class StatusBadgeView: NSView {
    let label = NativeStyle.label("Stopped", size: 13)
    private let content = StatusBadgeContent(frame: .zero)
    override init(frame: NSRect) {
        super.init(frame: frame)
        if #available(macOS 26.0, *) {
            let glass = NSGlassEffectView()
            glass.style = .regular
            glass.cornerRadius = 16
            glass.contentView = content
            NativeStyle.pin(glass, in: self)
            content.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([
                content.leadingAnchor.constraint(equalTo: glass.leadingAnchor),
                content.trailingAnchor.constraint(equalTo: glass.trailingAnchor),
                content.topAnchor.constraint(equalTo: glass.topAnchor),
                content.bottomAnchor.constraint(equalTo: glass.bottomAnchor),
            ])
        } else {
            content.usesLegacyBackground = true
            NativeStyle.pin(content, in: self)
        }
        label.textColor = .secondaryLabelColor
        label.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 32),
            label.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),
            label.centerYAnchor.constraint(equalTo: content.centerYAnchor),
            heightAnchor.constraint(equalToConstant: 32),
            widthAnchor.constraint(greaterThanOrEqualToConstant: 140),
            widthAnchor.constraint(lessThanOrEqualToConstant: 360),
        ])
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(_ status: AppModel.Status, scenario: String?, help: String) {
        content.status = status
        label.stringValue = RuleFormatting.statusItem(status: status, activeScenario: scenario)
        toolTip = help
        content.needsDisplay = true
    }
}

@MainActor
private final class StatusBadgeContent: NSView {
    var status: AppModel.Status = .down
    var usesLegacyBackground = false
    override func draw(_ dirtyRect: NSRect) {
        if usesLegacyBackground {
            let capsule = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: 16, yRadius: 16)
            BrowserAppearance.sidebar.setFill()
            capsule.fill()
            NSColor.separatorColor.setStroke()
            capsule.stroke()
        }
        NSImage(systemSymbolName: status.symbolName, accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(paletteColors: [.labelColor]))?
            .draw(in: NSRect(x: 8, y: 8, width: 18, height: 18))
        if let dot = status.dotColor {
            dot.setFill()
            NSBezierPath(ovalIn: NSRect(x: 23, y: 7, width: 5, height: 5)).fill()
        }
    }
}
