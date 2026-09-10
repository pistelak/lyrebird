import AppKit

@MainActor
final class RulesWindowController: NSWindowController {
    let model: AppModel
    let state = BrowserState()
    let split = NSSplitViewController()
    let sidebar = ScenarioSidebarController()
    let requests = RequestListController()
    let detail = RuleDetailController()
    private let statusBadge = StatusBadgeView(frame: .zero)
    private let interceptionItem = NSToolbarItem(itemIdentifier: .init("interception"))
    private let observation = ModelObservation()
    private var registered = false
    private var pendingDestination: RuleFormatting.Destination?
    private var browsingTask: Task<Void, Never>?

    init(model: AppModel, restore: Bool = true) {
        self.model = model
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 720),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered,
            defer: false)
        super.init(window: window)
        window.title = "Lyrebird"
        window.titleVisibility = .hidden
        window.identifier = .init("rules")
        window.isReleasedWhenClosed = false
        window.collectionBehavior = [.fullScreenPrimary]
        window.contentMinSize = NSSize(width: 940, height: 460)
        window.delegate = self
        split.splitView = BrowserSplitView()
        split.splitView.isVertical = true
        split.splitView.dividerStyle = .thin
        let left = NSSplitViewItem(sidebarWithViewController: sidebar)
        left.minimumThickness = 190
        left.maximumThickness = 340
        left.canCollapse = true
        left.holdingPriority = NSLayoutConstraint.Priority(260)
        let middle = NSSplitViewItem(viewController: requests)
        middle.minimumThickness = 300
        middle.maximumThickness = 640
        let right = NSSplitViewItem(viewController: detail)
        right.minimumThickness = 380
        split.addSplitViewItem(left)
        split.addSplitViewItem(middle)
        split.addSplitViewItem(right)
        window.contentViewController = split
        // Pane backgrounds extend through the toolbar; content uses each pane’s safe area.
        if restore { split.splitView.autosaveName = "AppKitBrowserSplit" }
        let toolbar = NSToolbar(identifier: "AppKitBrowserToolbar")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        toolbar.centeredItemIdentifiers = [.init("status")]
        window.toolbar = toolbar
        window.toolbarStyle = .unified
        window.titlebarSeparatorStyle = .none
        sidebar.onSelect = { [weak self] destination in self?.select(destination) }
        sidebar.onActivate = { [weak model] name in Task { await model?.activate(name) } }
        sidebar.canActivate = { [weak model] name in
            model?.busy == false && model?.scenarios != nil && name != model?.scenarios?.active
        }
        requests.onActivate = { [weak self] in self?.activateSelectedScenario(nil) }
        requests.onRule = { [weak self] selection in
            self?.state.ruleSelection = selection
            self?.render()
        }
        requests.onRecent = { [weak self] key in
            self?.state.recentSelection = key
            self?.render()
        }
        requests.onClear = { [weak self] in
            guard let self else { return }
            Task {
                await self.model.clearRecent()
                if model.lastError == nil { self.state.recentSelection = nil }
                self.render()
            }
        }
        detail.onPickStep = { [weak self] step, rule in
            self?.state.pickStep(step, rule: rule)
            self?.render()
        }
        detail.onOpenRule = { [weak self] destination in self?.openRule(destination) }
        window.center()
        if restore { window.setFrameAutosaveName("AppKitBrowserWindow") }
        window.contentView?.layoutSubtreeIfNeeded()
        if !restore || UserDefaults.standard.object(forKey: "NSSplitView Subview Frames AppKitBrowserSplit") == nil {
            split.splitView.setPosition(220, ofDividerAt: 0)
            split.splitView.setPosition(610, ofDividerAt: 1)
        }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func showWindow(_ sender: Any?) {
        if !registered {
            registered = true
            model.windowOpened()
            DockPresence.windowOpened()
            observation.start { [weak self] in self?.render() }
            Task { [weak self] in await self?.model.refresh() }
        }
        super.showWindow(sender)
        window?.makeKeyAndOrderFront(sender)
    }

    func select(_ destination: BrowserState.Destination?) {
        state.select(destination)
        if let name = state.scenario, !state.showsRecent {
            browsingTask?.cancel()
            browsingTask = Task { [weak self] in
                guard let self, !Task.isCancelled else { return }
                await model.browse(name)
            }
        }
        render()
    }

    func render() {
        // Capture traffic even while showing rules; see trafficUpdatesAfterSwitchingFromRulesWithoutAnotherHealthChange.
        let content = BrowserContent(model: model)
        let snapshot: RulesSnapshot?
        if case .ok(let value) = content.rulesRead { snapshot = value } else { snapshot = nil }
        state.reconcile(snapshot, activeScenario: model.ownHealth?.activeScenario)
        reconcilePendingDestination(in: snapshot)
        // Browsing an initially active scenario makes subsequent activation changes independent.
        if registered, let scenario = state.scenario, model.browsedScenario != scenario, !state.showsRecent {
            browsingTask?.cancel()
            browsingTask = Task { [weak self] in
                guard let self, !Task.isCancelled else { return }
                await model.browse(scenario)
            }
        }
        statusBadge.update(content.status, scenario: model.ownHealth?.activeScenario, help: model.statusLine)
        updateInterceptionItem()
        updateDismissItem(for: content.lastError)
        window?.toolbar?.validateVisibleItems()
        sidebar.update(
            content.scenarios ?? model.lastScenarios, selection: state.destination,
            problems: model.ownHealth?.scenariosNotWhole ?? [:], stale: content.scenarios == nil)
        requests.update(content, state: state)
        detail.update(content, state: state)
    }

    private func reconcilePendingDestination(in snapshot: RulesSnapshot?) {
        guard let destination = pendingDestination, snapshot?.scenario == destination.scenario else { return }
        let rows = snapshot.map { RuleFormatting.flowSections($0).flatMap(\.rows) } ?? []
        if case .rule(let id) = destination.selection,
            let row = rows.first(where: {
                $0.ruleId == id && (destination.step == nil || $0.step == destination.step)
            })
        {
            state.ruleSelection = row.selection
        } else {
            state.ruleSelection = destination.selection
        }
        if case .rule(let id) = destination.selection, let step = destination.step {
            state.pickStep(step, rule: id)
        }
        pendingDestination = nil
    }

    private func updateDismissItem(for error: String?) {
        guard let toolbar = window?.toolbar else { return }
        let dismissIndex = toolbar.items.firstIndex { $0.itemIdentifier.rawValue == "dismiss" }
        if RuleFormatting.actionFailure(error) != nil, dismissIndex == nil {
            toolbar.insertItem(withItemIdentifier: .init("dismiss"), at: max(0, toolbar.items.count - 1))
        } else if error == nil, let dismissIndex {
            toolbar.removeItem(at: dismissIndex)
        }
    }

    private func openRule(_ destination: RuleFormatting.Destination) {
        pendingDestination = destination
        select(.scenario(destination.scenario))
    }

    var canActivateSelection: Bool {
        guard !model.busy, let name = state.scenario, !state.showsRecent, let scenarios = model.scenarios else {
            return false
        }
        return name != scenarios.active && scenarios.scenarios.contains { $0.name == name }
    }

    @objc func activateSelectedScenario(_ sender: Any?) {
        guard canActivateSelection, let name = state.scenario else { return }
        Task { await model.activate(name) }
    }

    @objc func refresh(_ sender: Any?) {
        Task { await model.refresh() }
    }

    @objc func reload(_ sender: Any?) {
        Task { await model.reloadScenarios() }
    }

    @objc func toggleInterception(_ sender: Any?) {
        guard !model.busy else { return }
        Task { await model.toggle() }
    }

    private func updateInterceptionItem() {
        let stops = model.stopsRatherThanStarts
        interceptionItem.label = stops ? "Stop interception" : "Start interception"
        interceptionItem.image = NSImage(
            systemSymbolName: stops ? "stop.fill" : "play.fill", accessibilityDescription: interceptionItem.label)?
            .withSymbolConfiguration(.init(pointSize: stops ? 12 : 14, weight: .medium))
        interceptionItem.isEnabled = !model.busy
        interceptionItem.toolTip = interceptionItem.label
    }

    @objc func toggleSidebar(_ sender: Any?) {
        split.toggleSidebar(sender)
    }

    @objc func dismissError(_ sender: Any?) {
        model.dismissError()
    }

}

extension RulesWindowController: NSWindowDelegate {
    func windowWillClose(_ notification: Notification) {
        guard registered else { return }
        registered = false
        observation.stop()
        browsingTask?.cancel()
        model.windowClosed()
        DockPresence.windowClosed()
    }
}

extension RulesWindowController: NSToolbarDelegate {
    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarDefaultItemIdentifiers(toolbar) + [.init("dismiss"), .init("refresh")]
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [
            .flexibleSpace, .toggleSidebar, .sidebarTrackingSeparator, .init("title"), .init("interception"),
            .flexibleSpace, .init("status"), .flexibleSpace, .init("reload"),
        ]
    }

    func toolbar(
        _ toolbar: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier, willBeInsertedIntoToolbar flag: Bool
    ) -> NSToolbarItem? {
        if id == .sidebarTrackingSeparator {
            return NSTrackingSeparatorToolbarItem(identifier: id, splitView: split.splitView, dividerIndex: 0)
        }
        if id == .toggleSidebar {
            let item = NSToolbarItem(itemIdentifier: id)
            item.target = split
            item.action = #selector(NSSplitViewController.toggleSidebar(_:))
            item.label = "Toggle sidebar"
            return item
        }
        if id.rawValue == "interception" {
            updateInterceptionItem()
            interceptionItem.target = self
            interceptionItem.action = #selector(toggleInterception)
            interceptionItem.isBordered = true
            interceptionItem.visibilityPriority = .high
            interceptionItem.autovalidates = false
            return interceptionItem
        }
        let item = NSToolbarItem(itemIdentifier: id)
        item.target = self
        switch id.rawValue {
        case "title":
            item.label = "Lyrebird"
            item.view = NativeStyle.label("Lyrebird", size: 15, weight: .semibold)
        case "status":
            item.label = "Interception status"
            item.view = statusBadge
        case "refresh":
            item.label = "Refresh"
            item.toolTip = "Refresh status and lists from the running engine"
            item.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: item.label)
            item.action = #selector(refresh)
        case "reload":
            item.label = "Reload from disk"
            item.toolTip = "Re-read scenario files from disk and update the lists"
            item.image = NSImage(systemSymbolName: "arrow.down.doc", accessibilityDescription: item.label)
            item.action = #selector(reload)
        case "dismiss":
            item.label = "Dismiss error"
            item.image = NSImage(systemSymbolName: "xmark.circle", accessibilityDescription: item.label)
            item.action = #selector(dismissError)
        default: return nil
        }
        // Bordered so the symbol buttons get the system's material, the way the interception
        // control already does; the items that host their own view keep their own background.
        item.isBordered = item.view == nil
        return item
    }
}

extension RulesWindowController: NSMenuItemValidation {
    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(toggleInterception) {
            menuItem.title = model.stopsRatherThanStarts ? "Stop Interception" : "Start Interception"
            return !model.busy
        }
        if menuItem.action == #selector(activateSelectedScenario) { return canActivateSelection }
        if menuItem.action == #selector(toggleSidebar) {
            menuItem.title = split.splitViewItems.first?.isCollapsed == true ? "Show Sidebar" : "Hide Sidebar"
        }
        return true
    }
}

extension RulesWindowController: NSToolbarItemValidation {
    func validateToolbarItem(_ item: NSToolbarItem) -> Bool {
        // A busy Reload used to silently do nothing; see reloadToolbarItemDisablesWhileItsRequestIsOutstanding.
        if item.action == #selector(reload) || item.action == #selector(toggleInterception) {
            return !model.busy
        }
        return true
    }
}
