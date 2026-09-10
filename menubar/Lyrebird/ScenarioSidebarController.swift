import AppKit

@MainActor
final class ScenarioSidebarController: NSViewController, NSOutlineViewDataSource, NSOutlineViewDelegate, NSMenuDelegate
{
    final class Item: NSObject {
        let id: String
        let title: String
        let destination: BrowserState.Destination?
        var children: [Item] = []

        init(id: String, title: String, destination: BrowserState.Destination? = nil) {
            self.id = id
            self.title = title
            self.destination = destination
        }
    }

    let outline = NSOutlineView()
    private(set) var scrollView: NSScrollView!
    private var roots: [Item] = []
    private var signature: [ScenarioSummary]?
    private var active: String?
    private var problems: [String: [String]] = [:]
    private var stale = false
    private var applying = false
    var onSelect: (BrowserState.Destination) -> Void = { _ in }
    var onActivate: (String) -> Void = { _ in }
    var canActivate: (String) -> Bool = { _ in false }

    override func loadView() {
        if #available(macOS 26.0, *) {
            // The sidebar split item supplies glass; a legacy material here would cover it.
            view = NSView()
        } else {
            let background = NSVisualEffectView()
            background.material = .sidebar
            background.blendingMode = .withinWindow
            view = background
        }
        let column = NSTableColumn(identifier: .init("scenario"))
        outline.addTableColumn(column)
        outline.outlineTableColumn = column
        outline.headerView = nil
        outline.style = .sourceList
        outline.backgroundColor = .clear
        outline.floatsGroupRows = false
        outline.intercellSpacing = NSSize(width: 0, height: 0)
        outline.rowSizeStyle = .default
        outline.indentationPerLevel = 0
        outline.dataSource = self
        outline.delegate = self
        outline.target = self
        outline.doubleAction = #selector(activateClicked)
        outline.setAccessibilityIdentifier("scenario-sidebar")
        let menu = NSMenu()
        menu.delegate = self
        outline.menu = menu
        scrollView = NativeStyle.scroll(outline)
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.automaticallyAdjustsContentInsets = false
        view.addSubview(scrollView)
        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
    }

    func update(_ list: ScenarioList?, selection: BrowserState.Destination?, problems: [String: [String]], stale: Bool)
    {
        loadViewIfNeeded()
        applying = true
        defer { applying = false }
        let changed = signature != list?.scenarios
        let appearanceChanged = active != list?.active || self.problems != problems || self.stale != stale
        active = list?.active
        self.problems = problems
        self.stale = stale
        if changed || roots.isEmpty {
            let expanded = Set(allItems.filter { outline.isItemExpanded($0) }.map(\.id))
            let previousGroups = Set(allItems.filter { $0.destination == nil }.map(\.id))
            let first = roots.isEmpty
            signature = list?.scenarios
            roots = [Item(id: "recent", title: "Recent", destination: .recent)]
            let scenarios = Item(id: "scenarios", title: "Scenarios")
            roots.append(scenarios)
            var groups: [String: Item] = [:]
            for scenario in list?.scenarios ?? [] {
                let parts = scenario.name.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
                var parent = scenarios
                if parts.count > 1 {
                    for depth in 1..<parts.count {
                        let prefix = parts.prefix(depth).joined(separator: "/")
                        if let group = groups[prefix] {
                            parent = group
                        } else {
                            let group = Item(id: "group:" + prefix, title: parts[depth - 1])
                            parent.children.append(group)
                            groups[prefix] = group
                            parent = group
                        }
                    }
                }
                parent.children.append(
                    Item(
                        // The model's leaf, not `parts.last`, so the sidebar and the menu cannot drift apart
                        // on what a row is called. The two agree only because the engine refuses a name
                        // deeper than `group/name` (store.py `scenario_parts`), which is what holds the loop
                        // above to one level — see nativeSidebarKeepsSameNamedLeavesInTheirFolders.
                        id: "scenario:" + scenario.name, title: scenario.leaf,
                        destination: .scenario(scenario.name)))
            }
            outline.reloadData()
            for item in allItems where item.destination == nil {
                if first || !previousGroups.contains(item.id) || expanded.contains(item.id) || item === scenarios {
                    outline.expandItem(item)
                }
            }
        } else if appearanceChanged {
            // Refresh cell appearance without replacing row identities or resetting scroll.
            outline.reloadData(
                forRowIndexes: IndexSet(integersIn: 0..<outline.numberOfRows), columnIndexes: IndexSet(integer: 0))
        }
        if let item = allItems.first(where: { $0.destination == selection && selection != nil }) {
            let row = outline.row(forItem: item)
            if row >= 0, outline.selectedRow != row {
                outline.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
            }
        } else {
            outline.deselectAll(nil)
        }
    }

    private var allItems: [Item] {
        func descend(_ item: Item) -> [Item] {
            [item] + item.children.flatMap(descend)
        }
        return roots.flatMap(descend)
    }

    func outlineView(_ outlineView: NSOutlineView, numberOfChildrenOfItem item: Any?) -> Int {
        (item as? Item)?.children.count ?? roots.count
    }

    func outlineView(_ outlineView: NSOutlineView, child index: Int, ofItem item: Any?) -> Any {
        ((item as? Item)?.children ?? roots)[index]
    }

    func outlineView(_ outlineView: NSOutlineView, isItemExpandable item: Any) -> Bool {
        !(item as! Item).children.isEmpty
    }

    func outlineView(_ outlineView: NSOutlineView, shouldSelectItem item: Any) -> Bool {
        (item as! Item).destination != nil
    }

    // Give native source-list groups enough space for their labels; pinning a label on all four
    // edges compressed it below its font height. See BrowserDesignTests.
    func outlineView(_ outlineView: NSOutlineView, isGroupItem item: Any) -> Bool {
        (item as! Item).destination == nil
    }

    func outlineView(_ outlineView: NSOutlineView, viewFor tableColumn: NSTableColumn?, item: Any) -> NSView? {
        let item = item as! Item
        let cell = SidebarCell()
        cell.isGroup = item.destination == nil
        let field = NativeStyle.label(
            item.title, size: item.destination == nil ? 11 : 13, weight: item.destination == nil ? .semibold : .regular)
        field.lineBreakMode = .byTruncatingTail
        let icon = NSImageView()
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.contentTintColor = .controlAccentColor
        cell.addSubview(icon)
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2),
            icon.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        cell.iconWidth = icon.widthAnchor.constraint(equalToConstant: 14)
        cell.iconHeight = icon.heightAnchor.constraint(equalToConstant: 14)
        cell.iconWidth?.isActive = true
        cell.iconHeight?.isActive = true
        if item.destination == nil { field.textColor = .secondaryLabelColor }
        if case .scenario(let name) = item.destination {
            let warning = problems[name]?.isEmpty == false
            field.stringValue = item.title + (warning ? "  ⚠" : "")
            if active == name {
                icon.image = NSImage(systemSymbolName: "checkmark", accessibilityDescription: nil)
            }
            field.font = .systemFont(ofSize: 13, weight: active == name ? .semibold : .regular)
            cell.isActive = active == name
            field.textColor = stale ? .secondaryLabelColor : .labelColor
            cell.toolTip =
                ([name, active == name ? "Active scenario" : "Double-click to activate"] + (problems[name] ?? []))
                .joined(separator: "\n")
            field.setAccessibilityLabel(
                name + (active == name ? ", active" : "") + (warning ? ", did not load whole" : ""))
        } else if item.destination == .recent {
            icon.image = NSImage(systemSymbolName: "clock", accessibilityDescription: nil)
            icon.contentTintColor = .labelColor
        }
        field.setAccessibilityIdentifier(item.id)
        field.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(field)
        cell.textLeading = field.leadingAnchor.constraint(
            equalTo: cell.leadingAnchor, constant: item.destination == nil ? 2 : 22)
        cell.textLeading?.isActive = true
        NSLayoutConstraint.activate([
            field.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            field.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        cell.textField = field
        cell.imageView = icon
        cell.rowSizeStyle = outline.effectiveRowSizeStyle
        return cell
    }

    func outlineViewSelectionDidChange(_ notification: Notification) {
        guard !applying, let item = outline.item(atRow: outline.selectedRow) as? Item,
            let destination = item.destination
        else { return }
        onSelect(destination)
    }

    @objc private func activateClicked() {
        activate(row: outline.clickedRow >= 0 ? outline.clickedRow : outline.selectedRow)
    }

    private func activate(row: Int) {
        guard let item = outline.item(atRow: row) as? Item, case .scenario(let name) = item.destination,
            canActivate(name)
        else { return }
        onActivate(name)
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        guard let item = outline.item(atRow: outline.clickedRow) as? Item, case .scenario(let name) = item.destination
        else { return }
        let action = NSMenuItem(title: "Activate scenario", action: #selector(activateMenu(_:)), keyEquivalent: "")
        action.target = self
        action.representedObject = name
        action.isEnabled = canActivate(name)
        menu.autoenablesItems = false
        menu.addItem(action)
    }

    @objc private func activateMenu(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String, canActivate(name) else { return }
        onActivate(name)
    }
}

@MainActor
private final class SidebarCell: NSTableCellView {
    var isGroup = false
    var isActive = false
    var iconWidth: NSLayoutConstraint?
    var iconHeight: NSLayoutConstraint?
    var textLeading: NSLayoutConstraint?

    // AppKit forwards changes to the system sidebar size to existing cells as well as new ones.
    override var rowSizeStyle: NSTableView.RowSizeStyle {
        didSet {
            let textSize: CGFloat
            let iconSize: CGFloat
            switch rowSizeStyle {
            case .small: (textSize, iconSize) = (11, 12)
            case .large: (textSize, iconSize) = (15, 18)
            default: (textSize, iconSize) = (13, 14)
            }
            textField?.font = .systemFont(
                ofSize: isGroup ? textSize - 2 : textSize,
                weight: isGroup || isActive ? .semibold : .regular)
            iconWidth?.constant = iconSize
            iconHeight?.constant = iconSize
            textLeading?.constant = isGroup ? 2 : iconSize + 8
            imageView?.symbolConfiguration = .init(pointSize: iconSize, weight: .regular)
        }
    }
}
