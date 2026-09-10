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
        path.font = BrowserTypography.code
        path.maximumNumberOfLines = 2
        path.lineBreakMode = .byWordWrapping
        path.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        header.translatesAutoresizingMaskIntoConstraints = false
        path.translatesAutoresizingMaskIntoConstraints = false
        addSubview(header)
        addSubview(path)
        NSLayoutConstraint.activate([
            header.leadingAnchor.constraint(equalTo: leadingAnchor, constant: BrowserMetrics.requestHorizontalInset),
            header.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -BrowserMetrics.requestHorizontalInset),
            header.topAnchor.constraint(equalTo: topAnchor, constant: BrowserMetrics.requestVerticalInset),
            header.heightAnchor.constraint(equalToConstant: 18),
            path.leadingAnchor.constraint(equalTo: header.leadingAnchor),
            path.trailingAnchor.constraint(equalTo: header.trailingAnchor),
            path.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 6),
            path.bottomAnchor.constraint(
                lessThanOrEqualTo: bottomAnchor, constant: -BrowserMetrics.requestVerticalInset),
        ])
        toolTip =
            entry.method + " " + RuleFormatting.statusText(entry.status) + " " + entry.path
            + "\n"
            + (entry.patchSkipped.map { "Patch skipped: " + $0 } ?? entry.matched.map { "Answered by rule: " + $0 }
                ?? "No override answered")
        setAccessibilityElement(true)
        setAccessibilityLabel(toolTip)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    static func height(_ entry: RecentEntry, width: CGFloat) -> CGFloat {
        let font = BrowserTypography.code
        let pathHeight = (entry.path as NSString).boundingRect(
            with: NSSize(
                width: max(40, width - 2 * BrowserMetrics.requestHorizontalInset), height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: [.font: font]
        ).height
        return (2 * BrowserMetrics.requestVerticalInset + 18 + 6) + min(30, ceil(pathHeight))
    }
}
