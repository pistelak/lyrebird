import AppKit

@MainActor
final class DetailDocument {
    private let document = NSMutableAttributedString()

    private var cardStart: Int?

    private var cards: [NSRange] = []

    private(set) var copyRange: NSRange?

    private(set) var copyText: String?

    var length: Int {
        document.length
    }

    var attributedString: NSAttributedString {
        let result = NSMutableAttributedString(attributedString: document)
        var ranges = cards
        if let cardStart, document.length > cardStart {
            ranges.append(NSRange(location: cardStart, length: document.length - cardStart))
        }
        for (index, range) in ranges.enumerated() { result.addAttribute(.detailCard, value: index, range: range) }
        return result
    }

    private func paragraph(before: CGFloat = 0, after: CGFloat = 4) -> NSMutableParagraphStyle {
        let value = NSMutableParagraphStyle()
        value.firstLineHeadIndent = BrowserMetrics.detailContentInset
        value.headIndent = BrowserMetrics.detailContentInset
        value.tailIndent = -BrowserMetrics.detailContentInset
        value.paragraphSpacingBefore = before
        value.paragraphSpacing = after
        value.lineBreakMode = .byWordWrapping
        return value
    }

    func line(_ value: String, mono: Bool = false, secondary: Bool = false) {
        guard !value.isEmpty else { return }
        let color: NSColor = secondary ? .secondaryLabelColor : .labelColor
        let text = NSMutableAttributedString(
            attributedString: NativeStyle.text(value + "\n", color: color, mono: mono))
        text.addAttribute(.paragraphStyle, value: paragraph(), range: NSRange(location: 0, length: text.length))
        document.append(text)
    }

    private func separator() {
        let start = document.length
        line(" ")
        document.addAttributes(
            [.detailSeparator: true, .font: NSFont.systemFont(ofSize: 6)],
            range: NSRange(location: start, length: 1))
    }

    func section(_ title: String) {
        if let cardStart, document.length > cardStart {
            cards.append(NSRange(location: cardStart, length: document.length - cardStart))
        }
        let text = NSMutableAttributedString(
            attributedString: NativeStyle.text(title + "\n", size: 13, weight: .semibold))
        text.addAttribute(
            .paragraphStyle, value: paragraph(before: document.length == 0 ? 0 : 30, after: 20),
            range: NSRange(location: 0, length: text.length))
        document.append(text)
        cardStart = document.length
    }

    func request(_ method: String, _ path: String) {
        let start = document.length
        line(" " + method + "   " + path, mono: true)
        document.addAttributes(
            [.detailBadge: NSColor.labelColor, .font: BrowserTypography.badge],
            range: NSRange(location: start, length: method.utf16.count + 2))
    }

    func mode(_ kind: RuleFormatting.ResponseKind) {
        let start = document.length
        line(" " + kind.title + " ")
        document.addAttributes(
            [
                .detailBadge: BrowserAppearance.tint(kind), .foregroundColor: BrowserAppearance.tint(kind),
                .font: NSFont.systemFont(ofSize: 11, weight: .medium),
            ], range: NSRange(location: start, length: kind.title.utf16.count + 2))
        line(kind.explanation, secondary: true)
        line(" ")
    }

    func facts(_ facts: [RuleFormatting.Fact]) {
        // Between the rows, not around them: the section heading above and whatever follows already
        // bound the block, and a rule on both sides of a lone header read as two mistakes.
        for (index, fact) in facts.enumerated() {
            if index > 0 { separator() }
            let start = document.length
            line(fact.label + "\t" + fact.value)
            let range = NSRange(location: start, length: document.length - start)
            document.addAttribute(.detailHeaderRow, value: true, range: range)
            document.addAttributes(
                [
                    .font: BrowserTypography.code,
                    .foregroundColor: NSColor.secondaryLabelColor,
                ], range: NSRange(location: start + fact.label.utf16.count + 1, length: fact.value.utf16.count))
        }
    }

    func body(_ value: JSONValue, title: String = "Body", note: String? = nil) {
        separator()
        let start = document.length
        line(title)
        copyRange = NSRange(location: start, length: title.utf16.count)
        document.addAttributes(
            [
                .font: NSFont.systemFont(ofSize: 12, weight: .semibold),
                .paragraphStyle: paragraph(before: 3, after: 12),
            ], range: NSRange(location: start, length: title.utf16.count + 1))
        if let note { line(note, secondary: true) }
        let styled = NSMutableAttributedString(
            attributedString: NSAttributedString(RuleFormatting.attributedJSON(value)))
        let style = paragraph(after: 0)
        style.lineSpacing = 2
        styled.addAttributes(
            [.font: BrowserTypography.code, .paragraphStyle: style],
            range: NSRange(location: 0, length: styled.length))
        document.append(styled)
        document.append(
            NSAttributedString(
                string: "\n",
                attributes: [.paragraphStyle: paragraph(after: 0), .font: NSFont.systemFont(ofSize: 4)]))
        copyText = RuleFormatting.jsonText(value)
    }

    func query(_ query: [String: JSONValue]) {
        separator()
        let titleStart = document.length
        line("Query parameters", secondary: true)
        document.addAttribute(
            .font, value: NSFont.systemFont(ofSize: 11),
            range: NSRange(location: titleStart, length: document.length - titleStart))
        for key in query.keys.sorted() {
            let start = document.length
            line(key + "  =  " + RuleFormatting.scalarText(query[key]), mono: true)
            document.addAttribute(
                .font, value: BrowserTypography.code,
                range: NSRange(location: start, length: document.length - start))
            document.addAttribute(
                .foregroundColor, value: NSColor.secondaryLabelColor,
                range: NSRange(location: start, length: key.utf16.count))
            document.addAttribute(
                .foregroundColor, value: NSColor.tertiaryLabelColor,
                range: NSRange(location: start + key.utf16.count, length: 5))
        }
    }

    func bodyContains(_ contains: String) {
        separator()
        let start = document.length
        line("Body contains", secondary: true)
        document.addAttribute(
            .font, value: NSFont.systemFont(ofSize: 11),
            range: NSRange(location: start, length: document.length - start))
        line(contains, mono: true)
    }

    func link(_ value: String, token: String) {
        let link = NSMutableAttributedString(attributedString: NativeStyle.text(value + "\n"))
        link.addAttribute(
            .paragraphStyle, value: paragraph(), range: NSRange(location: 0, length: link.length))
        link.addAttribute(.link, value: token, range: NSRange(location: 0, length: link.length - 1))
        document.append(link)
    }
}
