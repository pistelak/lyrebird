import AppKit
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct BrowserControllerTests {
        @Test func toolbarInterceptionActionFollowsStatusAndBusyState() async throws {
            try await withAppTestEnvironment {
                let model = AppModel(autoStart: false, expectedFingerprint: "toolbar")
                let controller = RulesWindowController(model: model, restore: false)
                let toolbar = try #require(controller.window?.toolbar)
                let item = try #require(toolbar.items.first { $0.itemIdentifier.rawValue == "interception" })
                let menu = NSMenuItem(
                    title: "", action: #selector(RulesWindowController.toggleInterception(_:)), keyEquivalent: "")
                for intercepting in [false, true, false] {
                    model.healthRead = .up(
                        Health(proxyUp: true, intercepting: intercepting, profileFingerprint: "toolbar"))
                    controller.render()
                    #expect(item.label == (intercepting ? "Stop interception" : "Start interception"))
                    #expect(item.isEnabled)
                    #expect(controller.validateMenuItem(menu))
                    #expect(menu.title == (intercepting ? "Stop Interception" : "Start Interception"))
                    model.busy = true
                    controller.render()
                    #expect(!item.isEnabled)
                    #expect(!controller.validateMenuItem(menu))
                    model.busy = false
                }
            }
        }

        /// The rewrite left folder selection in the model but showed every qualified name in the menu,
        /// making users hunt through unrelated scenarios; opening the menu must follow each active folder.
        @Test func menuOpeningScopesScenariosToTheActiveFolder() async throws {
            try await withAppTestEnvironment {
                let model = AppModel(autoStart: false, expectedFingerprint: RulesFixture.ours)
                let controller = StatusItemController(model: model)
                let scenarios: [ScenarioSummary] = [
                    .init(name: "orders/pending", overrideCount: 2, verified: true),
                    .init(name: "orders/complete", overrideCount: 1, verified: false),
                    .init(name: "checkout/pending", overrideCount: 3, verified: false),
                    .init(name: "default", overrideCount: 0, verified: false),
                ]
                let delegate = try #require(controller.menu.delegate)
                for (active, names, caption) in [
                    ("orders/pending", ["orders/pending", "orders/complete"], "orders/"),
                    ("checkout/pending", ["checkout/pending"], "checkout/"),
                    ("default", ["default"], ""),
                    ("missing/pending", ["default"], "missing/pending is not in this profile"),
                ] {
                    model.scenarios = ScenarioList(active: active, scenarios: scenarios)
                    for busy in [false, true] {
                        model.busy = busy
                        controller.menu.removeAllItems()
                        delegate.menuWillOpen?(controller.menu)
                        let entries = controller.menu.items.filter { $0.representedObject is String }
                        #expect(Set(entries.compactMap { $0.representedObject as? String }) == Set(names))
                        if !caption.isEmpty {
                            let heading = try #require(controller.menu.items.first { $0.title == caption })
                            #expect(!heading.isEnabled)
                        } else {
                            #expect(!controller.menu.items.contains { $0.title.hasSuffix("/") })
                        }
                        for entry in entries {
                            let scenario = try #require(
                                scenarios.first { $0.name == entry.representedObject as? String })
                            let leaf = String(scenario.name.split(separator: "/").last!)
                            #expect(
                                entry.title == leaf + "  (\(scenario.overrideCount))" + (scenario.verified ? "  ✓" : "")
                            )
                            #expect(entry.state == (scenario.name == active ? .on : .off))
                            #expect(entry.isEnabled == !busy)
                        }
                    }
                }
                model.scenarios = nil
                controller.menu.removeAllItems()
                delegate.menuWillOpen?(controller.menu)
                #expect(controller.menu.items.contains { $0.title == model.scenariosPlaceholder && !$0.isEnabled })
            }
        }

        /// Two folders can hold the same leaf; sending that display name activates the wrong mock
        /// or fails outright, so the constructed item's action must carry the qualified name to the client.
        @Test func menuActionActivatesTheQualifiedNameForDuplicateLeaves() async throws {
            try await withAppTestEnvironment {
                let model = model()
                await model.refresh()
                model.scenarios = ScenarioList(
                    active: "checkout/ready",
                    scenarios: [
                        .init(name: "orders/pending", overrideCount: 1, verified: false),
                        .init(name: "checkout/pending", overrideCount: 1, verified: false),
                        .init(name: "checkout/ready", overrideCount: 0, verified: false),
                    ])
                let controller = StatusItemController(model: model)
                let delegate = try #require(controller.menu.delegate)
                controller.menu.removeAllItems()
                delegate.menuWillOpen?(controller.menu)
                let entry = try #require(
                    controller.menu.items.first { $0.representedObject as? String == "checkout/pending" })
                let action = try #require(entry.action)
                #expect(NSApp.sendAction(action, to: entry.target, from: entry))
                try await waitFor("menu activation did not reach the client") {
                    StubURLProtocol.requests.contains { $0.httpMethod == "PUT" }
                }
                let request = try #require(StubURLProtocol.requests.first { $0.httpMethod == "PUT" })
                #expect(request.url?.path == "/__mock__/scenarios/active")
                let body = try #require(
                    JSONSerialization.jsonObject(with: StubURLProtocol.body(of: request)) as? [String: String])
                #expect(body["name"] == "checkout/pending")
                try await waitFor("activation did not finish") { !model.busy }
            }
        }

        /// A disconnected reload leaves externally edited profiles invisible until a restart;
        /// the toolbar action must request a disk read and update the native sidebar on its own.
        @Test func reloadToolbarActionUpdatesTheDisplayedScenarios() async throws {
            try await withAppTestEnvironment {
                let model = model()
                let controller = RulesWindowController(model: model, restore: false)
                controller.showWindow(nil)
                defer { controller.close() }
                try await waitFor("initial scenario did not load") { model.rulesRead != nil }
                StubURLProtocol.install { request in
                    if request.url?.path == "/__mock__/scenarios/reload" {
                        return (Stub.response(request, 200), Data("{}".utf8))
                    }
                    if request.url?.path == "/__mock__/scenarios",
                        StubURLProtocol.requests.contains(where: { $0.url?.path == "/__mock__/scenarios/reload" })
                    {
                        let body =
                            #"{"active":"orders-outage","scenarios":[{"name":"disk-added","overrideCount":0,"verified":false}]}"#
                        return (Stub.response(request, 200), Data(body.utf8))
                    }
                    return RulesFixture.serve(request)
                }
                let toolbar = try #require(controller.window?.toolbar)
                let item = try #require(toolbar.items.first { $0.itemIdentifier.rawValue == "reload" })
                #expect(item.label == "Reload from disk")
                let action = try #require(item.action)
                #expect(NSApp.sendAction(action, to: item.target, from: item))
                try await waitFor("reload did not update the displayed sidebar") {
                    (0..<controller.sidebar.outline.numberOfRows).contains { row in
                        let entry = controller.sidebar.outline.item(atRow: row) as? ScenarioSidebarController.Item
                        return entry?.destination == .scenario("disk-added")
                    }
                }
                #expect(
                    StubURLProtocol.requests.contains {
                        $0.httpMethod == "POST" && $0.url?.path == "/__mock__/scenarios/reload"
                    })
                try await waitFor("reload did not finish") { !model.busy }
            }
        }

        /// A generic reload error hides which file needs repair, costing another debugging cycle;
        /// firing the toolbar control must put the engine's own refusal in the visible request list.
        @Test func refusedReloadToolbarActionDisplaysTheEngineSentence() async throws {
            try await withAppTestEnvironment {
                let model = model()
                let controller = RulesWindowController(model: model, restore: false)
                controller.showWindow(nil)
                defer { controller.close() }
                try await waitFor("initial scenario did not load") { model.rulesRead != nil }
                let sentence = "skipped broken.json: Expecting value"
                StubURLProtocol.install { request in
                    if request.url?.path == "/__mock__/scenarios/reload" {
                        let body = #"{"error":"reload_refused","detail":"skipped broken.json: Expecting value"}"#
                        return (Stub.response(request, 409), Data(body.utf8))
                    }
                    return RulesFixture.serve(request)
                }
                let toolbar = try #require(controller.window?.toolbar)
                let item = try #require(toolbar.items.first { $0.itemIdentifier.rawValue == "reload" })
                let action = try #require(item.action)
                #expect(NSApp.sendAction(action, to: item.target, from: item))
                try await waitFor("engine refusal did not reach the visible error") {
                    controller.requests.rows.contains { $0.text.string.contains(sentence) }
                }
                try await waitFor("refused reload did not finish") { !model.busy }
            }
        }

        private func model() -> AppModel {
            StubURLProtocol.install { request in RulesFixture.serve(request) }
            return AppModel(
                client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
                autoStart: false, expectedFingerprint: RulesFixture.ours)
        }

        private func waitFor(_ description: String, _ condition: () -> Bool) async throws {
            let deadline = ContinuousClock.now.advanced(by: .seconds(3))
            while ContinuousClock.now < deadline {
                if condition() { return }
                try await Task.sleep(for: .milliseconds(20))
            }
            try #require(condition(), Comment(rawValue: description))
        }

        @Test func panesStayBelowToolbarAcrossEmptyAndLongContent() async throws {
            try await withAppTestEnvironment {
                let model = model()
                let controller = RulesWindowController(model: model, restore: false)
                controller.showWindow(nil)
                defer { controller.close() }
                try await waitFor("rules did not load") { model.rulesRead != nil && controller.state.scenario != nil }
                let window = try #require(controller.window)
                var snapshot = try JSONDecoder().decode(RulesSnapshot.self, from: Data(RulesFixture.snapshot.utf8))
                snapshot.scenario = controller.state.scenario!
                for size in [
                    NSSize(width: 940, height: 460), NSSize(width: 1180, height: 720), NSSize(width: 1500, height: 900),
                ] {
                    window.setContentSize(size)
                    let frame = window.frame
                    for empty in [false, true, false, true] {
                        var value = snapshot
                        if empty {
                            value.rules = []
                        } else {
                            value.rules[0].body = .object([
                                "text": .string(String(repeating: "long synthetic payload ", count: 400))
                            ])
                        }
                        model.rulesRead = .ok(value)
                        controller.render()
                        window.contentView?.layoutSubtreeIfNeeded()
                        try await Task.sleep(for: .milliseconds(40))
                        #expect(window.frame == frame)
                        for pane in [
                            controller.sidebar.scrollView!, controller.requests.scrollView!,
                            controller.detail.scrollView!,
                        ] {
                            let rect = pane.convert(pane.bounds, to: nil)
                            let safe = window.contentLayoutRect.insetBy(dx: -1, dy: -1)
                            #expect(safe.contains(rect), "Pane escaped window content area: \(rect), safe: \(safe)")
                        }
                        let header = controller.requests.heading
                        #expect(window.contentLayoutRect.contains(header.convert(header.bounds, to: nil)))
                        #expect(controller.detail.textView.isHorizontallyResizable == false)
                        #expect(controller.detail.textView.textContainer?.widthTracksTextView == true)
                    }
                }
            }
        }

        @Test func activationIsExplicitAndUnavailableForStaleOrActiveSelections() async throws {
            try await withAppTestEnvironment {
                let model = model()
                await model.refresh()
                let controller = RulesWindowController(model: model, restore: false)
                let item = NSMenuItem(
                    title: "Activate Scenario", action: #selector(RulesWindowController.activateSelectedScenario(_:)),
                    keyEquivalent: "\r")
                controller.state.select(.scenario("checkout"))
                controller.render()
                #expect(controller.validateMenuItem(item))
                #expect(!StubURLProtocol.requests.contains { $0.httpMethod == "PUT" })
                controller.activateSelectedScenario(nil)
                try await waitFor("explicit activation did not issue its request") {
                    StubURLProtocol.requests.contains { $0.httpMethod == "PUT" }
                }
                let request = try #require(StubURLProtocol.requests.first { $0.httpMethod == "PUT" })
                #expect(request.url?.path == "/__mock__/scenarios/active")
                try await waitFor("activation did not finish") { !model.busy }
                controller.state.select(.scenario("orders-outage"))
                #expect(!controller.validateMenuItem(item))
                controller.state.select(.scenario("missing"))
                #expect(!controller.validateMenuItem(item))
                controller.state.select(.scenario("checkout"))
                model.scenarios = nil
                #expect(!controller.validateMenuItem(item))
                controller.state.select(.recent)
                #expect(!controller.validateMenuItem(item))
            }
        }

        @Test func refreshPreservesBrowsingAndNativeSelection() async throws {
            try await withAppTestEnvironment {
                let model = model()
                let controller = RulesWindowController(model: model, restore: false)
                controller.showWindow(nil)
                defer { controller.close() }
                controller.select(.scenario("checkout"))
                try await waitFor("checkout did not load") {
                    if case .ok(let snapshot) = model.rulesRead { return snapshot.scenario == "checkout" }
                    return false
                }
                controller.render()
                let selected = controller.state.ruleSelection
                try #require(selected != nil)
                for _ in 0..<4 {
                    await model.refresh()
                    controller.render()
                    #expect(controller.state.destination == .scenario("checkout"))
                    #expect(controller.state.ruleSelection == selected)
                    #expect(controller.requests.table.selectedRow >= 0)
                    #expect(controller.requests.rows[controller.requests.table.selectedRow].selection == selected)
                }
                controller.state.query = "no-synthetic-path-matches-this"
                controller.render()
                #expect(controller.state.ruleSelection == selected)
                #expect(controller.requests.table.selectedRow == -1)
                #expect(controller.requests.rows.contains { $0.text.string.contains("hidden by the search") })
                #expect(controller.detail.textView.string.contains("Request"))
                controller.state.query = ""
                controller.render()
                #expect(controller.requests.table.selectedRow >= 0)
                #expect(!StubURLProtocol.requests.contains { $0.httpMethod == "PUT" })
            }
        }

        @Test func windowLifecycleUsesNativeOwnership() async throws {
            try await withAppTestEnvironment {
                let model = model()
                let controller = RulesWindowController(model: model, restore: false)
                controller.showWindow(nil)
                controller.showWindow(nil)
                #expect(DockPresence.openWindows == 1)
                #expect(model.rulesWindowOpen)
                controller.close()
                #expect(!model.rulesWindowOpen)
                #expect(DockPresence.openWindows == 0)
                controller.showWindow(nil)
                #expect(model.rulesWindowOpen)
                #expect(DockPresence.openWindows == 1)
                controller.close()
                try await Task.sleep(for: .milliseconds(80))
                #expect(!model.rulesWindowOpen)
                #expect(DockPresence.openWindows == 0)
            }
        }

        @Test func deselectionAndActivationDoNotMoveTheReader() {
            let state = BrowserState()
            state.select(.scenario("checkout"))
            state.select(nil)
            state.reconcile(nil, activeScenario: "orders-outage")
            #expect(state.scenario == "checkout")
            state.rulesQuery = "orders"
            state.select(.recent)
            state.query = "PATCH"
            #expect(state.rulesQuery == "orders")
            #expect(state.recentQuery == "PATCH")
            state.select(.scenario("checkout"))
            #expect(state.query == "orders")
        }

        @Test func scenarioChangesDoNotReuseRuleOrStepIdentity() throws {
            var snapshot = try JSONDecoder().decode(RulesSnapshot.self, from: Data(RulesFixture.snapshot.utf8))
            let state = BrowserState()
            state.select(.scenario(snapshot.scenario))
            state.reconcile(snapshot, activeScenario: snapshot.scenario)
            state.pickStep(3, rule: "ovr_items")
            state.ruleSelection = .rule("missing")
            snapshot.scenario = "checkout"
            state.select(.scenario("checkout"))
            state.reconcile(snapshot, activeScenario: "orders-outage")
            #expect(state.pickedStep == nil)
            #expect(state.ruleSelection != .rule("missing"))
        }

        /// Identical leaf names in different folders must remain separate navigation targets;
        /// qualified labels or merged identities would make browsing the intended scenario ambiguous.
        @Test func nativeSidebarKeepsSameNamedLeavesInTheirFolders() async throws {
            try await withAppTestEnvironment {
                let sidebar = ScenarioSidebarController()
                var selections: [BrowserState.Destination] = []
                sidebar.onSelect = { selections.append($0) }
                let list = ScenarioList(
                    active: "orders/pending",
                    scenarios: [
                        .init(name: "orders/pending", overrideCount: 1, verified: false),
                        .init(name: "checkout/pending", overrideCount: 1, verified: false),
                    ])
                sidebar.update(list, selection: nil, problems: [:], stale: false)
                let items = (0..<sidebar.outline.numberOfRows).compactMap {
                    sidebar.outline.item(atRow: $0) as? ScenarioSidebarController.Item
                }
                let orders = try #require(items.first { $0.destination == .scenario("orders/pending") })
                let checkout = try #require(items.first { $0.destination == .scenario("checkout/pending") })
                #expect(orders !== checkout)
                #expect(orders.id == "scenario:orders/pending")
                #expect(checkout.id == "scenario:checkout/pending")
                for (item, folder) in [(orders, "orders"), (checkout, "checkout")] {
                    let parent = try #require(sidebar.outline.parent(forItem: item) as? ScenarioSidebarController.Item)
                    #expect(parent.id == "group:" + folder)
                    #expect(parent.title == folder)
                    let cell = try #require(
                        sidebar.outlineView(sidebar.outline, viewFor: sidebar.outline.outlineTableColumn, item: item)
                            as? NSTableCellView)
                    #expect(cell.textField?.stringValue == "pending")
                    sidebar.outline.selectRowIndexes(
                        IndexSet(integer: sidebar.outline.row(forItem: item)), byExtendingSelection: false)
                    let selected =
                        sidebar.outline.item(atRow: sidebar.outline.selectedRow)
                        as? ScenarioSidebarController.Item
                    #expect(selected === item)
                }
                #expect(selections == [.scenario("orders/pending"), .scenario("checkout/pending")])
            }
        }

        @Test func nativeSidebarRefreshKeepsSelectionAndGroups() async throws {
            try await withAppTestEnvironment {
                let sidebar = ScenarioSidebarController()
                var writes: [BrowserState.Destination] = []
                sidebar.onSelect = { writes.append($0) }
                let list = ScenarioList(
                    active: "orders/pending",
                    scenarios: [
                        .init(name: "orders/pending", overrideCount: 1, verified: false),
                        .init(name: "orders/complete", overrideCount: 1, verified: true),
                    ])
                sidebar.update(list, selection: .scenario("orders/complete"), problems: [:], stale: false)
                let selected = sidebar.outline.selectedRow
                try #require(selected >= 0)
                for _ in 0..<6 {
                    sidebar.update(list, selection: .scenario("orders/complete"), problems: [:], stale: false)
                    #expect(sidebar.outline.selectedRow == selected)
                }
                #expect(writes.isEmpty)
                let item = try #require(sidebar.outline.item(atRow: selected) as? ScenarioSidebarController.Item)
                #expect(item.destination == .scenario("orders/complete"))
            }
        }

        @Test func detailShowsInheritedSequenceBodyAndOmission() async throws {
            try await withAppTestEnvironment {
                let model = model()
                await model.refresh()
                var snapshot = try JSONDecoder().decode(RulesSnapshot.self, from: Data(RulesFixture.snapshot.utf8))
                snapshot.rules = [
                    RuleRow(
                        id: "ovr_items", match: RuleMatch(method: "GET", path: "/api/items"),
                        rewrite: Rewrite(
                            active: true, mode: "replace", bodyKind: "json",
                            sequence: RewriteSequence(steps: [
                                StepSummary(
                                    status: 200, body: .object(["items": .array([])]), bodyKind: "json",
                                    inherited: ["body", "headers"]),
                                StepSummary(status: 200, bodyOmitted: true, bodyKind: "json", bodyBytes: 1_258_291),
                            ])))
                ]
                model.rulesRead = .ok(snapshot)
                let state = BrowserState()
                state.select(.scenario(snapshot.scenario))
                state.ruleSelection = .rule("ovr_items")
                let detail = RuleDetailController()
                detail.update(model: model, state: state)
                #expect(detail.textView.string.contains("inherited from the rule"))
                #expect(detail.textView.string.contains("\"items\": []"))
                #expect(!detail.stepPicker.isHidden)
                detail.onPickStep = { step, rule in
                    state.pickStep(step, rule: rule)
                    detail.update(model: model, state: state)
                }
                detail.stepPicker.selectItem(at: 1)
                // Invoke the control's target/action without opening a tracking menu.
                NSApp.sendAction(detail.stepPicker.action!, to: detail.stepPicker.target, from: detail.stepPicker)
                #expect(state.pickedStep?.step == 2)
                #expect(detail.textView.string.contains("1.2 MB not included"))
                #expect(!detail.textView.string.contains("\"items\""))
            }
        }
        @Test func aNewHeadingNeverDisplaysThePreviousScenariosRows() async throws {
            try await withAppTestEnvironment {
                let model = model()
                await model.refresh()
                model.rulesRead = .ok(BrowserPreview.snapshot("orders-pending"))
                let state = BrowserState()
                state.select(.scenario("empty"))
                let list = RequestListController()
                list.update(model: model, state: state)
                #expect(list.rows.allSatisfy { $0.selection == nil })
                #expect(list.rows.contains { $0.text.string.contains("Reading the rules") })
            }
        }

        @Test func settingsCancelAndInvalidURLDoNotChangePreferences() async throws {
            try await withAppTestEnvironment {
                Config.defaults.set("http://localhost:8088", forKey: Config.controlURLKey)
                let settings = SettingsWindowController(model: model())
                settings.showWindow(nil)
                settings.controlURL.stringValue = "not a URL"
                settings.profile.stringValue = "/tmp/unused-profile"
                settings.save()
                #expect(settings.window?.isVisible == true)
                #expect(Config.controlURL.absoluteString == "http://localhost:8088")
                #expect(Config.profilePath.isEmpty)
                settings.close()
                #expect(Config.profilePath.isEmpty)
                #expect(DockPresence.openWindows == 0)
            }
        }
        @Test func trafficUpdatesAfterSwitchingFromRulesWithoutAnotherHealthChange() async throws {
            try await withAppTestEnvironment {
                let model = model()
                let controller = RulesWindowController(model: model, restore: false)
                controller.showWindow(nil)
                defer { controller.close() }
                try await waitFor("initial scenario did not load") {
                    model.browsedScenario != nil && model.rulesRead != nil
                }
                // Finish the initial browse refresh before injecting the traffic event.
                await model.refresh()
                controller.select(.recent)
                model.recentRead = .ok([
                    RecentEntry(id: "fresh-event", method: "GET", path: "/api/new-event", status: 200)
                ])
                try await waitFor("traffic change did not reach the native table") {
                    controller.requests.rows.contains { $0.text.string.contains("/api/new-event") }
                }
            }
        }
    }
}
