import AppKit
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct NativeContentTests {
        /// Passing pane data must retain the failed-read distinction: an unavailable read must
        /// replace old requests with its reason, without claiming the proxy has seen no traffic.
        @Test func panesRejectStaleRulesAndFailedTrafficWithoutAModel() {
            let snapshot = BrowserPreview.snapshot("orders-pending")
            let entry = RecentEntry(id: "recorded", method: "GET", path: "/api/items", status: 200)
            var content = BrowserContent(
                status: .intercepting, controlPort: 8088, rulesRead: .ok(snapshot), recentRead: .ok([entry]),
                recentPlaceholder: "no traffic yet", scenarios: nil, busy: false, lastError: nil)
            let state = BrowserState()
            state.select(.scenario("empty"))
            let list = RequestListController()
            let detail = RuleDetailController()
            list.update(content, state: state)
            detail.update(content, state: state)
            #expect(list.rows.allSatisfy { $0.selection == nil })
            #expect(list.rows.contains { $0.text.string.contains("Reading the rules") })
            #expect(!detail.textView.string.contains("/api/orders"))

            state.select(.recent)
            state.recentSelection = entry.selectionKey
            list.update(content, state: state)
            detail.update(content, state: state)
            #expect(list.table.selectedRow >= 0)
            #expect(detail.textView.string.contains("/api/items"))

            content.recentRead = .unavailable("Synthetic read failure")
            list.update(content, state: state)
            detail.update(content, state: state)
            #expect(list.table.selectedRow == -1)
            #expect(list.rows.allSatisfy { $0.traffic == nil })
            #expect(list.rows.contains { $0.text.string.contains("Synthetic read failure") })
            #expect(!list.rows.contains { $0.text.string.contains("no traffic yet") })
            #expect(detail.textView.string.contains("Traffic could not be read."))
            #expect(detail.textView.string.contains("Synthetic read failure"))
            #expect(!detail.textView.string.contains("/api/items"))
        }

        /// Menu commands must still reach their owners after model access moves to the binding,
        /// and opening the menu must read the current folder and busy state before rendering.
        @Test func statusMenuUsesInjectedActionsAndFreshContentWithoutAModel() throws {
            var content = StatusItemController.Content(
                status: .intercepting, statusLine: "Intercepting", stopsRatherThanStarts: true,
                busy: false, simBundleId: "com.example.Store", lastError: nil,
                scenarios: ScenarioList(
                    active: "orders/ready",
                    scenarios: [
                        .init(name: "orders/ready", overrideCount: 1, verified: true),
                        .init(name: "checkout/ready", overrideCount: 2, verified: false),
                    ]),
                scenariosPlaceholder: "proxy not running",
                recent: [RecentEntry(id: "recorded", method: "GET", path: "/api/items", status: 200)],
                recentPlaceholder: "no traffic yet")
            let controller = StatusItemController(content: content)
            var commands: [String] = []
            controller.onToggle = { commands.append("toggle") }
            controller.onRelaunch = { commands.append("relaunch") }
            controller.onActivate = { commands.append($0) }
            controller.onClear = { commands.append("clear") }
            controller.onBrowse = { commands.append("browse") }
            controller.onSettings = { commands.append("settings") }
            for title in ["Stop", "Relaunch app", "ready  (1)  ✓", "Clear recent traffic", "Scenarios…", "Settings…"] {
                let item = try #require(controller.menu.items.first { $0.title == title })
                #expect(item.isEnabled)
                let action = try #require(item.action)
                #expect(NSApp.sendAction(action, to: item.target, from: item))
            }
            #expect(commands == ["toggle", "relaunch", "orders/ready", "clear", "browse", "settings"])

            content.busy = true
            content.scenarios?.active = "checkout/ready"
            controller.menuContent = { content }
            controller.menuWillOpen(controller.menu)
            let scenarios = controller.menu.items.filter { $0.representedObject is String }
            #expect(scenarios.count == 1)
            #expect(scenarios.first?.representedObject as? String == "checkout/ready")
            #expect(scenarios.first?.state == .on)
            #expect(scenarios.first?.isEnabled == false)
            for title in ["Stop", "Relaunch app", "Clear recent traffic"] {
                #expect(controller.menu.items.first { $0.title == title }?.isEnabled == false)
            }
            #expect(controller.menu.items.first { $0.title == "Settings…" }?.isEnabled == true)
        }
    }
}
