import AppKit

@MainActor
final class RequestListController: NSViewController, NSTableViewDataSource, NSTableViewDelegate {
    struct Row {
        var id: String
        var text: NSAttributedString
        var selection: RuleFormatting.ListSelection?
        var recent: RecentEntry.Key?
        var flow: RuleFormatting.FlowListRow?
        var isHeading = false
        var selectable: Bool { selection != nil || recent != nil }
    }

    let table = NSTableView()
    let heading = NativeStyle.label("Rules", size: 17, weight: .semibold)
    private(set) var scrollView: NSScrollView!
    private(set) var rows: [Row] = []
    private var applying = false
    private var layoutWidth: CGFloat = 0
    private var scope = ""
    var onRule: (RuleFormatting.ListSelection) -> Void = { _ in }
    var onRecent: (RecentEntry.Key) -> Void = { _ in }
    var onClear: () -> Void = {}
    var onActivate: () -> Void = {}
    private lazy var activate = ActionButton("Activate") { [weak self] in self?.onActivate() }
    private lazy var clear = ActionButton("Clear") { [weak self] in self?.onClear() }

    override func loadView() {
        view = BrowserSurface(BrowserAppearance.pane)
        let header = NSStackView(views: [heading, activate, clear])
        header.orientation = .horizontal
        header.spacing = 8
        header.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(header)
        let column = NSTableColumn(identifier: .init("request"))
        column.minWidth = 0
        column.resizingMask = .autoresizingMask
        table.addTableColumn(column)
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        table.headerView = nil
        table.style = .plain
        table.backgroundColor = BrowserAppearance.pane
        table.intercellSpacing = NSSize(width: 0, height: 2)
        table.dataSource = self
        table.delegate = self
        table.setAccessibilityIdentifier("request-list")
        let divider = NSBox()
        divider.boxType = .separator
        divider.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(divider)
        scrollView = NativeStyle.scroll(table)
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scrollView)
        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 14),
            header.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 14),
            header.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -14),
            header.heightAnchor.constraint(equalToConstant: 28),
            divider.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 12),
            divider.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            divider.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: divider.bottomAnchor, constant: 0),
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        NativeStyle.toolbarSeparator(in: view)
    }

    override func viewDidLayout() {
        super.viewDidLayout()
        let width = scrollView.contentSize.width
        guard width > 0, let column = table.tableColumns.first,
            abs(layoutWidth - width) > 0.5
        else { return }
        layoutWidth = width
        column.width = width
        table.setFrameSize(NSSize(width: width, height: table.frame.height))
        table.noteHeightOfRows(withIndexesChanged: IndexSet(integersIn: rows.indices))
    }

    func update(model: AppModel, state: BrowserState) {
        loadViewIfNeeded()
        heading.stringValue =
            state.showsRecent
            ? "Recent traffic" : state.scenario?.split(separator: "/").last.map(String.init) ?? "Rules"
        heading.toolTip = state.scenario ?? heading.stringValue
        activate.isHidden = state.showsRecent
        activate.toolTip = "Use this scenario to answer subsequent requests"
        activate.isEnabled =
            !model.busy && state.scenario != model.scenarios?.active
            && model.scenarios?.scenarios.contains(where: { $0.name == state.scenario }) == true
        clear.isHidden = !state.showsRecent
        clear.isEnabled = !model.busy && !model.recent.isEmpty
        // A click changes the heading before its async read starts; never pair that heading
        // with the previous scenario's rows (BrowserControllerTests).
        let rulesRead: MockClient.RulesRead?
        if case .ok(let snapshot) = model.rulesRead, snapshot.scenario != state.scenario {
            rulesRead = nil
        } else {
            rulesRead = model.rulesRead
        }
        var next: [Row] = []
        func note(_ id: String, _ text: String, error: Bool = false) {
            next.append(Row(id: id, text: NativeStyle.text(text, color: error ? .systemOrange : .secondaryLabelColor)))
        }
        if let failure = RuleFormatting.actionFailure(model.lastError) { note("error", failure, error: true) }
        if state.showsRecent {
            if let vacancy = RuleFormatting.proxyVacancy(status: model.status, controlPort: model.ownHealth?.proxyPort)
            {
                note("vacancy", vacancy.message + "\n" + vacancy.hint)
            } else if case .unavailable(let reason) = model.recentRead {
                note("vacancy", "Traffic could not be read.\n" + reason)
            } else {
                let needle = state.query.trimmingCharacters(in: .whitespacesAndNewlines)
                for item in RecentTrafficItem.items(model.recent) {
                    let entry = item.entry
                    guard
                        needle.isEmpty
                            || [entry.method, entry.path, String(entry.status), entry.matched ?? ""].contains(where: {
                                $0.localizedCaseInsensitiveContains(needle)
                            })
                    else { continue }
                    let text = NSMutableAttributedString(
                        attributedString: NativeStyle.text(
                            entry.method + "  " + RuleFormatting.statusText(entry.status) + "  " + entry.path + "\n",
                            weight: .medium, mono: true))
                    text.append(
                        NativeStyle.text(
                            entry.matched == nil ? "No override answered" : "Answered by an override", size: 12,
                            color: .secondaryLabelColor))
                    next.append(Row(id: String(describing: item.id), text: text, recent: entry.selectionKey))
                }
                if next.allSatisfy({ $0.recent == nil }) && model.recent.isEmpty {
                    note("empty", model.recentPlaceholder)
                } else if !next.contains(where: { $0.id != "error" }) {
                    note("empty", "No requests match your search.")
                }
            }
        } else {
            let problems = RuleFormatting.problems(in: rulesRead)
            if !problems.isEmpty { note("problems", problems.joined(separator: "\n"), error: true) }
            switch RuleFormatting.rulesColumn(
                status: model.status, read: rulesRead, controlPort: model.ownHealth?.proxyPort)
            {
            case .vacancy(let vacancy): note("vacancy", vacancy.message + "\n" + vacancy.hint)
            case .list(let snapshot, let vacancy):
                if let notes = RuleFormatting.scenarioNotes(snapshot.scenario, in: model.scenarios) {
                    next.append(
                        Row(
                            id: "notes-heading",
                            text: NativeStyle.text(
                                "About this scenario", size: 12, weight: .semibold, color: .secondaryLabelColor),
                            isHeading: true))
                    next.append(Row(id: "notes", text: NativeStyle.text(notes)))
                }
                if let vacancy { note("vacancy", vacancy.message + "\n" + vacancy.hint) }
                let sections = RuleFormatting.flowSections(snapshot, query: state.query)
                for section in sections {
                    let id = String(describing: section.id)
                    next.append(
                        Row(
                            id: id,
                            text: NativeStyle.text(
                                (section.rows.contains { $0.number != nil } ? "↓  " : "") + section.title, size: 12,
                                weight: .semibold, color: .secondaryLabelColor), isHeading: true))
                    for row in section.rows {
                        var line = row.number.map { "\($0).  " } ?? ""
                        line +=
                            row.request.method + "  " + (row.status.map { "\($0)  " } ?? "") + row.request.path + "\n"
                        let text = NSMutableAttributedString(
                            attributedString: NativeStyle.text(line, weight: .medium, mono: true))
                        text.append(
                            NativeStyle.text(
                                (row.responseKind.map { $0.title + " · " } ?? "") + row.subtitle
                                    + (row.delay.map { " · after " + $0 } ?? "") + (row.inactive ? " · Inactive" : ""),
                                size: 12, color: .secondaryLabelColor))
                        next.append(
                            Row(id: String(describing: row.id), text: text, selection: row.selection, flow: row))
                    }
                    if let footer = section.footer { note(id + ":footer", footer) }
                }
                if sections.isEmpty && vacancy == nil { note("empty", "No requests match your search.") }
                if let selected = state.ruleSelection,
                    !sections.flatMap(\.rows).contains(where: { $0.selection == selected })
                {
                    note("hidden", "The selected rule is hidden by the search.")
                }
            }
        }
        let newScope = state.showsRecent ? "recent" : "scenario:" + (state.scenario ?? "")
        let changed =
            next.count != rows.count || zip(next, rows).contains { $0.id != $1.id || !$0.text.isEqual(to: $1.text) }
        applying = true
        defer { applying = false }
        if changed {
            let topRow = table.row(at: NSPoint(x: 1, y: scrollView.contentView.bounds.minY))
            let anchor = rows.indices.contains(topRow) ? rows[topRow].id : nil
            let offset = topRow >= 0 ? scrollView.contentView.bounds.minY - table.rect(ofRow: topRow).minY : 0
            rows = next
            table.reloadData()
            if newScope == scope, let anchor, let index = rows.firstIndex(where: { $0.id == anchor }) {
                scrollView.contentView.scroll(to: NSPoint(x: 0, y: table.rect(ofRow: index).minY + offset))
            } else {
                scrollView.contentView.scroll(to: .zero)
            }
            scrollView.reflectScrolledClipView(scrollView.contentView)
        }
        scope = newScope
        let index = rows.firstIndex {
            state.showsRecent
                ? ($0.recent != nil && $0.recent == state.recentSelection)
                : ($0.selection != nil && $0.selection == state.ruleSelection)
        }
        if let index {
            table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        } else {
            table.deselectAll(nil)
        }
    }

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { rows[row].selectable }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        if let flow = rows[row].flow { return FlowRequestCell(flow) }
        let cell = TextCell(frame: .zero)
        cell.label.attributedStringValue = rows[row].text
        cell.toolTip = rows[row].text.string
        return cell
    }
    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        if let flow = rows[row].flow { return FlowRequestCell.height(flow, width: scrollView.contentSize.width) }
        if rows[row].isHeading { return rows[row].id == "notes-heading" ? 28 : 42 }
        let width = max(100, scrollView.contentSize.width - 32)
        return ceil(
            rows[row].text.boundingRect(
                with: NSSize(width: width, height: .greatestFiniteMagnitude),
                options: [.usesLineFragmentOrigin, .usesFontLeading]
            ).height) + 22
    }
    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        let view = BrowserTableRow()
        view.separates = rows[row].flow != nil || rows[row].id == "notes" || rows[row].id == "notes-heading"
        return view
    }
    func tableViewColumnDidResize(_ notification: Notification) {
        table.noteHeightOfRows(withIndexesChanged: IndexSet(integersIn: rows.indices))
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !applying, rows.indices.contains(table.selectedRow) else { return }
        let row = rows[table.selectedRow]
        if let selection = row.selection { onRule(selection) }
        if let recent = row.recent { onRecent(recent) }
    }
}
