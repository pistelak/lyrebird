import AppKit

@MainActor
final class RecentRequestCell: NSTableCellView {
    private let method: BadgeView
    private let status: BadgeView

    override var backgroundStyle: NSView.BackgroundStyle {
        didSet {
            method.highlighted = backgroundStyle == .emphasized
            status.highlighted = backgroundStyle == .emphasized
        }
    }

    init(_ entry: RecentEntry) {
        method = BadgeView(entry.method)
        status = BadgeView(RuleFormatting.statusText(entry.status))
        super.init(frame: .zero)
        let outcome =
            entry.patchSkipped != nil ? "Patch skipped" : entry.matched == nil ? "No override" : "Override answered"
        let caption = NativeStyle.label(outcome, size: 11)
        caption.textColor = .secondaryLabelColor
        let header = NSStackView(views: [method, status, NSView(), caption])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 6
        let path = NSTextField(wrappingLabelWithString: entry.path)
        path.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        path.maximumNumberOfLines = 2
        path.lineBreakMode = .byWordWrapping
        path.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        header.translatesAutoresizingMaskIntoConstraints = false
        path.translatesAutoresizingMaskIntoConstraints = false
        addSubview(header)
        addSubview(path)
        NSLayoutConstraint.activate([
            header.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 16),
            header.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -16),
            header.topAnchor.constraint(equalTo: topAnchor, constant: 8),
            header.heightAnchor.constraint(equalToConstant: 18),
            path.leadingAnchor.constraint(equalTo: header.leadingAnchor),
            path.trailingAnchor.constraint(equalTo: header.trailingAnchor),
            path.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 6),
            path.bottomAnchor.constraint(lessThanOrEqualTo: bottomAnchor, constant: -8),
        ])
        toolTip =
            entry.method + " " + RuleFormatting.statusText(entry.status) + " " + entry.path
            + "\n"
            + (entry.patchSkipped.map { "Patch skipped: " + $0 } ?? entry.matched.map { "Answered by rule: " + $0 }
                ?? "No override answered")
        setAccessibilityElement(true)
        setAccessibilityLabel(toolTip)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    static func height(_ entry: RecentEntry, width: CGFloat) -> CGFloat {
        let font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        let pathHeight = (entry.path as NSString).boundingRect(
            with: NSSize(width: max(40, width - 32), height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: [.font: font]
        ).height
        return 40 + min(30, ceil(pathHeight))
    }
}
