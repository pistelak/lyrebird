import AppKit

@MainActor
final class RuleDetailController: NSViewController {
    let textView: DetailTextView = {
        let storage = NSTextStorage()
        let manager = DetailLayoutManager()
        storage.addLayoutManager(manager)
        let container = NSTextContainer(size: NSSize(width: 400, height: CGFloat.greatestFiniteMagnitude))
        manager.addTextContainer(container)
        return DetailTextView(frame: .zero, textContainer: container)
    }()

    private(set) var scrollView: NSScrollView!

    let stepPicker = NSPopUpButton(frame: .zero, pullsDown: false)

    private lazy var copyButton = ActionButton("Copy") { [weak self] in self?.copyBody() }

    private var copyText: String?

    private var links: [String: RuleFormatting.Destination] = [:]

    private var currentRule: String?

    private var identity = ""

    private var copyRange: NSRange?

    private var headerHeight: NSLayoutConstraint!

    private var tabWidth: CGFloat = 0

    private var responseRange = NSRange(location: 0, length: 0)

    var onOpenRule: (RuleFormatting.Destination) -> Void = { _ in }

    var onPickStep: (Int, String) -> Void = { _, _ in }

    override func loadView() {
        view = BrowserSurface(BrowserAppearance.pane)
        stepPicker.target = self
        stepPicker.action = #selector(stepChanged)
        stepPicker.setAccessibilityLabel("Sequence response step")
        let header = NSStackView(views: [stepPicker, NSView()])
        header.translatesAutoresizingMaskIntoConstraints = false
        header.orientation = .horizontal
        view.addSubview(header)
        textView.addSubview(copyButton)
        copyButton.controlSize = .small
        copyButton.font = .systemFont(ofSize: 11)
        copyButton.setAccessibilityLabel("Copy body")
        textView.didLayout = { [weak self] in self?.layoutDocumentAccessories() }
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = true
        textView.drawsBackground = false
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.lineFragmentPadding = 0
        textView.textContainerInset = NSSize(width: 20, height: 20)
        textView.minSize = .zero
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.delegate = self
        textView.setAccessibilityIdentifier("response-detail")
        scrollView = NativeStyle.scroll(textView)
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scrollView)
        headerHeight = header.heightAnchor.constraint(equalToConstant: 0)
        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 0),
            header.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 20),
            header.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -20),
            headerHeight,
            scrollView.topAnchor.constraint(equalTo: header.bottomAnchor),
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        NativeStyle.toolbarSeparator(in: view)
    }

    func update(_ content: BrowserContent, state: BrowserState) {
        loadViewIfNeeded()
        let document = DetailDocument()
        links = [:]
        copyText = nil
        currentRule = nil
        stepPicker.isHidden = true

        func vacancy(_ value: RuleFormatting.Vacancy) {
            document.section(value.message)
            document.line(value.hint, secondary: true)
        }

        func body(_ value: JSONValue, title: String = "Body", note: String? = nil) {
            document.body(value, title: title, note: note)
            copyButton.setAccessibilityLabel("Copy body")
        }

        func transition(_ value: ScenarioOutline.Transition, ending: Bool = false, showRelated: Bool = false) {
            document.section(ending ? "Advances past the final response" : "Advances the sequence")
            document.request(value.request.method, value.request.path)
            document.facts(value.conditions)
            if value.conditions.isEmpty {
                document.line("Any query or body can advance this sequence.", secondary: true)
            }
            if showRelated || ending {
                if value.relatedResponses.isEmpty {
                    document.line(
                        "No response rule was identified. Advancement does not depend on a successful response.",
                        secondary: true)
                }
                for related in value.relatedResponses {
                    let token = "lyrebird-rule:\(links.count)"
                    links[token] = RuleFormatting.destination(rule: related.id, drawnFrom: state.scenario ?? "")
                    document.link(
                        (related.line.isEmpty ? "Open response rule" : related.line)
                            + (related.inactive ? " · Inactive" : ""), token: token)
                    document.facts(related.conditions)
                }
            }
        }
        let snapshot: RulesSnapshot?
        if case .ok(let value) = content.rulesRead, value.scenario == state.scenario {
            snapshot = value
        } else {
            snapshot = nil
        }
        let flow = RuleFormatting.flowRow(state.ruleSelection, in: snapshot)
        var newIdentity = String(describing: state.destination) + String(describing: state.ruleSelection)
        if state.showsRecent {
            newIdentity = "recent:" + String(describing: state.recentSelection)
            if let value = RuleFormatting.proxyVacancy(status: content.status, controlPort: content.controlPort) {
                vacancy(value)
            } else if case .unavailable(let reason) = content.recentRead {
                vacancy(.init(message: "Traffic could not be read.", hint: reason))
            } else if let key = state.recentSelection,
                let entry = content.recent.first(where: { $0.selectionKey == key })
            {
                document.section("Recorded request")
                document.request(entry.method, entry.path)
                if let time = entry.time { document.line("Time: " + time) }
                document.line("Status: " + RuleFormatting.statusText(entry.status))
                document.section("What happened")
                document.line(entry.matched.map { "Answered by rule: " + $0 } ?? "No override answered this request.")
                if let reason = entry.patchSkipped { document.line("Patch skipped: " + reason) }
                if let step = entry.selectedStep { document.line("Sequence response: \(step)") }
                if entry.overrun == true { document.line("The sequence was exhausted.") }
                for advanced in entry.advanced ?? [] { document.line("Advanced sequence: " + advanced) }
                if let run = entry.runId { document.line("Run: " + run, mono: true) }
                document.line("Request and response bodies are not captured.", secondary: true)
            } else {
                vacancy(
                    .init(
                        message: state.recentSelection == nil
                            ? "Select a request." : "This request is no longer in recent traffic.",
                        hint: "Recent traffic records what happened, independently of the scenario you are browsing."))
            }
        } else if let value = RuleFormatting.proxyVacancy(status: content.status, controlPort: content.controlPort) {
            vacancy(value)
        } else if let rule = RuleFormatting.detailRule(selection: state.ruleSelection, in: snapshot) {
            currentRule = rule.id
            document.section("Request")
            document.request(RuleFormatting.method(of: rule.match), RuleFormatting.path(of: rule.match))
            if let query = rule.match?.query, !query.isEmpty { document.query(query) }
            if let contains = rule.match?.bodyContains, !contains.isEmpty { document.bodyContains(contains) }
            if !rule.isActive { document.line("Inactive", secondary: true) }
            if let value = flow?.transition { transition(value) }
            responseRange = NSRange(location: document.length, length: 0)
            document.section("Response")
            let kind = RuleFormatting.responseKind(rule.rewrite)
            document.mode(kind)
            if let sequence = rule.rewrite.sequence {
                let shown = flow?.step ?? RuleFormatting.shownStep(rule, in: snapshot?.scenario, pick: state.pickedStep)
                if flow?.step == nil && !sequence.steps.isEmpty {
                    let titles = sequence.steps.enumerated().map {
                        RuleFormatting.stepMenuLabel(number: $0.offset + 1, step: $0.element)
                    }
                    if stepPicker.itemTitles != titles {
                        stepPicker.removeAllItems()
                        stepPicker.addItems(withTitles: titles)
                    }
                    if let shown { stepPicker.selectItem(at: shown - 1) }
                    stepPicker.isHidden = false
                }
                if let shown, sequence.steps.indices.contains(shown - 1) {
                    let step = sequence.steps[shown - 1]
                    if flow?.step == nil { document.line("Step \(shown) of \(sequence.steps.count)", secondary: true) }
                    document.line(RuleFormatting.stepBehaviourLine(step) + RuleFormatting.delayPhrase(rule.rewrite))
                    document.line(RuleFormatting.metaLine(kind: step.bodyKind, bytes: step.bodyBytes), secondary: true)
                    if let note = RuleFormatting.inheritedCaption(step, field: "status") {
                        document.line("Status " + note, secondary: true)
                    }
                    document.facts(RuleFormatting.headerRows(step.headers))
                    if let note = RuleFormatting.inheritedCaption(step, field: "headers") {
                        document.line("Headers " + note, secondary: true)
                    }
                    switch RuleFormatting.stepBody(step) {
                    case .none: document.line("No body", secondary: true)
                    case .omitted(let reason): document.line(reason, secondary: true)
                    case .json(let value): body(value, note: RuleFormatting.inheritedCaption(step, field: "body"))
                    }
                } else {
                    document.line("This sequence has no steps.", secondary: true)
                }
            } else {
                if rule.rewrite.mode == "patch" {
                    let clauses = RuleFormatting.clauseLine(rule.rewrite, includeApplicability: false)
                    if !clauses.isEmpty { document.line(clauses, secondary: true) }
                    if let delay = RuleFormatting.delayLabel(rule.rewrite) { document.line("Delay: " + delay) }
                } else {
                    document.line(RuleFormatting.behaviourSentence(rule.rewrite))
                }
                if let meta = RuleFormatting.metaLine(rule.rewrite) { document.line(meta, secondary: true) }
                document.facts(RuleFormatting.headerRows(rule.headers))
                if rule.rewrite.mode == "patch", let patch = rule.patch {
                    let count = rule.rewrite.patchKeys.map { " · \($0) \($0 == 1 ? "key" : "keys")" } ?? ""
                    body(patch, title: "Changes" + count)
                } else if rule.rewrite.mode != "patch", let value = rule.body {
                    body(value)
                }
            }
            if let ending = flow?.endingTransition { transition(ending, ending: true) }
            if let notes = rule.notes, !notes.isEmpty {
                document.section("Notes")
                document.line(notes)
            }
        } else if let value = flow?.transition {
            transition(value, showRelated: true)
        } else if let value = RuleFormatting.rulesVacancy(
            status: content.status, read: content.rulesRead, controlPort: content.controlPort)
        {
            vacancy(value)
        } else {
            vacancy(.init(message: "No rule selected.", hint: "Pick one on the left to see what it answers with."))
        }
        let rendered = document.attributedString
        copyRange = document.copyRange
        copyText = document.copyText
        headerHeight.constant = stepPicker.isHidden ? 0 : 38
        copyButton.isHidden = copyText == nil
        if !textView.attributedString().isEqual(to: rendered) {
            let origin = scrollView.contentView.bounds.origin
            let selected = textView.selectedRange()
            textView.textStorage?.setAttributedString(rendered)
            // Text-only invalidation leaves stale card edges; see detailRepaintClearsOldDecorations.
            textView.needsDisplay = true
            scrollView.contentView.needsDisplay = true
            tabWidth = 0
            layoutDocumentAccessories()
            textView.layoutManager?.ensureLayout(for: textView.textContainer!)
            if newIdentity == identity {
                if NSMaxRange(selected) <= document.length { textView.setSelectedRange(selected) }
                scrollView.contentView.scroll(to: origin)
            } else {
                textView.setSelectedRange(NSRange(location: 0, length: 0))
                scrollView.contentView.scroll(to: .zero)
            }
            scrollView.reflectScrolledClipView(scrollView.contentView)
        }
        identity = newIdentity
        textView.needsLayout = true
    }

    private func layoutDocumentAccessories() {
        guard let storage = textView.textStorage, let container = textView.textContainer,
            let manager = textView.layoutManager
        else { return }
        let width = container.size.width
        if width > 40, width != tabWidth {
            tabWidth = width
            var updates: [(NSRange, NSMutableParagraphStyle)] = []
            storage.enumerateAttribute(.detailHeaderRow, in: NSRange(location: 0, length: storage.length)) {
                value, range, _ in
                guard value != nil,
                    let style =
                        (storage.attribute(.paragraphStyle, at: range.location, effectiveRange: nil)
                        as? NSParagraphStyle)?.mutableCopy() as? NSMutableParagraphStyle
                else { return }
                style.tabStops = [NSTextTab(textAlignment: .right, location: width - BrowserMetrics.detailContentInset)]
                updates.append((range, style))
            }
            for (range, style) in updates { storage.addAttribute(.paragraphStyle, value: style, range: range) }
        }
        guard let copyRange, NSMaxRange(copyRange) <= storage.length else { return }
        manager.ensureLayout(for: container)
        let rect = manager.boundingRect(
            forGlyphRange: manager.glyphRange(forCharacterRange: copyRange, actualCharacterRange: nil), in: container)
        let origin = textView.textContainerOrigin
        copyButton.frame = NSRect(
            x: origin.x + container.size.width - (52 + BrowserMetrics.detailContentInset), y: origin.y + rect.minY - 3,
            width: 52, height: 22)
    }

    func scrollToResponse() {
        textView.scrollRangeToVisible(responseRange)
    }

    @objc private func stepChanged() {
        guard let currentRule else { return }
        onPickStep(stepPicker.indexOfSelectedItem + 1, currentRule)
        scrollToResponse()
    }

    private func copyBody() {
        guard let copyText else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(copyText, forType: .string)
    }

}

extension RuleDetailController: NSTextViewDelegate {
    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        let key = (link as? URL)?.absoluteString ?? link as? String ?? ""
        guard let destination = links[key] else { return false }
        onOpenRule(destination)
        return true
    }
}
