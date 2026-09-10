import AppKit

@MainActor
final class FlowRequestCell: NSTableCellView {
    private var textColors: [(NSTextField, NSColor)] = []
    private var badges: [BadgeView] = []
    override var backgroundStyle: NSView.BackgroundStyle {
        didSet { updateSelectionAppearance() }
    }

    private func updateSelectionAppearance() {
        let highlighted = backgroundStyle == .emphasized
        for (field, normal) in textColors {
            field.textColor = highlighted ? .alternateSelectedControlTextColor : normal
        }
        for badge in badges { badge.highlighted = highlighted }
    }

    init(_ row: RuleFormatting.FlowListRow) {
        super.init(frame: .zero)
        let column = NSStackView()
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 4
        let path = NativeStyle.label(row.request.path)
        path.font = .monospacedSystemFont(ofSize: 13, weight: .medium)
        path.lineBreakMode = .byTruncatingMiddle
        path.maximumNumberOfLines = 2
        let method = BadgeView(row.request.method)
        var parts: [NSView] = [method]
        if let status = row.status { parts.append(BadgeView(String(status))) }
        if let delay = row.delay { parts.append(BadgeView("◷ " + delay)) }
        parts.append(path)
        let request = NSStackView(views: parts)
        request.orientation = .horizontal
        request.alignment = .top
        request.spacing = 8
        column.addArrangedSubview(request)
        let subtitle = NSTextField(wrappingLabelWithString: row.subtitle + (row.inactive ? " · Inactive" : ""))
        subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = .secondaryLabelColor
        subtitle.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        var second: [NSView] = []
        if let kind = row.responseKind { second.append(BadgeView(kind.title, tint: BrowserAppearance.tint(kind))) }
        second.append(subtitle)
        let caption = NSStackView(views: second)
        caption.orientation = .horizontal
        caption.alignment = .top
        caption.spacing = 8
        column.addArrangedSubview(caption)
        column.translatesAutoresizingMaskIntoConstraints = false
        addSubview(column)
        let leading: CGFloat = row.number == nil ? 16 : 48
        if let number = row.number {
            let badge = BadgeView(String(number), circle: true)
            badge.translatesAutoresizingMaskIntoConstraints = false
            addSubview(badge)
            NSLayoutConstraint.activate([
                badge.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
                badge.topAnchor.constraint(equalTo: topAnchor, constant: 8),
            ])
        }
        NSLayoutConstraint.activate([
            column.leadingAnchor.constraint(equalTo: leadingAnchor, constant: leading),
            column.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -16),
            column.topAnchor.constraint(equalTo: topAnchor, constant: 8),
            column.bottomAnchor.constraint(lessThanOrEqualTo: bottomAnchor, constant: -8),
            request.widthAnchor.constraint(equalTo: column.widthAnchor),
            caption.widthAnchor.constraint(equalTo: column.widthAnchor),
        ])
        toolTip = row.request.method + " " + row.request.path + "\n" + row.subtitle
        setAccessibilityElement(true)
        setAccessibilityLabel(toolTip)

        func collect(_ view: NSView) {
            if let field = view as? NSTextField { textColors.append((field, field.textColor ?? .labelColor)) }
            if let badge = view as? BadgeView { badges.append(badge) }
            for child in view.subviews { collect(child) }
        }
        collect(self)
        updateSelectionAppearance()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    static func height(_ row: RuleFormatting.FlowListRow, width: CGFloat) -> CGFloat {
        let content = width - (row.number == nil ? 32 : 64)
        let mono = NSFont.monospacedSystemFont(ofSize: 13, weight: .medium)
        let badgeFont = NSFont.systemFont(ofSize: 11, weight: .semibold)
        let badges = [row.request.method, row.status.map(String.init), row.delay.map { "◷ " + $0 }].compactMap { $0 }
        let reserved = badges.reduce(CGFloat(0)) {
            $0 + ($1 as NSString).size(withAttributes: [.font: badgeFont]).width + 18
        }
        let path = (row.request.path as NSString).boundingRect(
            with: NSSize(width: max(40, content - reserved), height: 34), options: .usesLineFragmentOrigin,
            attributes: [.font: mono]
        ).height
        let modeWidth =
            row.responseKind.map { ($0.title as NSString).size(withAttributes: [.font: badgeFont]).width + 18 } ?? 0
        let subtitle = row.subtitle + (row.inactive ? " · Inactive" : "")
        let caption = (subtitle as NSString).boundingRect(
            with: NSSize(width: max(40, content - modeWidth), height: .greatestFiniteMagnitude),
            options: .usesLineFragmentOrigin, attributes: [.font: NSFont.systemFont(ofSize: 12)]
        ).height
        return ceil(max(18, min(path, 34)) + max(18, caption) + 20)
    }
}
