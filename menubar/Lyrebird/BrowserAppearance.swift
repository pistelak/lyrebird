import AppKit

/// Shared colors and type for the native browser's cards, badges and rows.
enum BrowserAppearance {
    static let pane = NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(white: 0.15, alpha: 1) : NSColor(white: 0.98, alpha: 1)
    }
    static let sidebar = NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(white: 0.125, alpha: 1) : NSColor(white: 0.94, alpha: 1)
    }
    static let card = NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(white: 0.19, alpha: 1) : NSColor(white: 0.94, alpha: 1)
    }
    static let sequence = NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(red: 0.38, green: 0.62, blue: 0.60, alpha: 1)
            : NSColor(red: 0.22, green: 0.43, blue: 0.41, alpha: 1)
    }
    static let jsonString = syntaxColor(light: (0.0, 0.38, 0.16), dark: (0.45, 0.84, 0.53))
    static let jsonNumber = syntaxColor(light: (0.0, 0.30, 0.66), dark: (0.48, 0.72, 1.0))
    static let jsonLiteral = syntaxColor(light: (0.52, 0.15, 0.60), dark: (0.85, 0.62, 0.94))

    private static func syntaxColor(light: (CGFloat, CGFloat, CGFloat), dark: (CGFloat, CGFloat, CGFloat)) -> NSColor {
        NSColor(name: nil) { appearance in
            let best = appearance.bestMatch(from: [
                .accessibilityHighContrastAqua, .accessibilityHighContrastDarkAqua,
                .aqua, .darkAqua,
            ])
            let isDark = best == .darkAqua || best == .accessibilityHighContrastDarkAqua
            if best == .accessibilityHighContrastAqua || best == .accessibilityHighContrastDarkAqua {
                return isDark ? .white : .black
            }
            let rgb = isDark ? dark : light
            return NSColor(srgbRed: rgb.0, green: rgb.1, blue: rgb.2, alpha: 1)
        }
    }

    static func tint(_ kind: RuleFormatting.ResponseKind) -> NSColor {
        switch kind {
        case .sequence: return sequence
        case .replace: return .systemBlue
        case .patch: return .systemOrange
        case .unknown: return .secondaryLabelColor
        }
    }
}

@MainActor
final class BrowserSurface: NSView {
    let color: NSColor
    init(_ color: NSColor) {
        self.color = color
        super.init(frame: .zero)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func draw(_ dirtyRect: NSRect) {
        color.setFill()
        bounds.fill()
    }
    override func viewDidChangeEffectiveAppearance() { needsDisplay = true }
}

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
    override func viewDidChangeEffectiveAppearance() { needsDisplay = true }
}

@MainActor
final class BrowserTableRow: NSTableRowView {
    var separates = false
    override func drawSelection(in dirtyRect: NSRect) {
        (isEmphasized ? NSColor.selectedContentBackgroundColor : NSColor.unemphasizedSelectedContentBackgroundColor)
            .setFill()
        // AppKit clips the drawing; rounding the visible slice creates seams in cached scroll pixels.
        NSBezierPath(roundedRect: bounds.insetBy(dx: 8, dy: 1), xRadius: 8, yRadius: 8).fill()
    }
    override func drawBackground(in dirtyRect: NSRect) {
        super.drawBackground(in: dirtyRect)
        guard separates, !isSelected else { return }
        NSColor.separatorColor.setFill()
        NSRect(x: 22, y: bounds.height - 1, width: max(0, bounds.width - 36), height: 0.5).fill()
    }
}

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

extension NSAttributedString.Key {
    static let detailCard = NSAttributedString.Key("LyrebirdDetailCard")
    static let detailBadge = NSAttributedString.Key("LyrebirdDetailBadge")
    static let detailSeparator = NSAttributedString.Key("LyrebirdDetailSeparator")
    static let detailHeaderRow = NSAttributedString.Key("LyrebirdDetailHeaderRow")
}

/// TextKit owns selection and reflow. These decorations follow its glyph geometry instead of
/// introducing content-dependent window sizes; BrowserDesignTests covers narrow cards and headers.
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
