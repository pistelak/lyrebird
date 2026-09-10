import AppKit
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct PaneDecisionTests {
        @Test func requestListShowsEveryRuleAndTrafficEntryAcrossNavigation() {
            let rules = [
                RuleRow(
                    id: "inactive", match: RuleMatch(method: "DELETE", path: "/api/items/*"),
                    rewrite: Rewrite(active: false, mode: "replace", status: 503, bodyKind: "none")),
                RuleRow(
                    id: "active", match: RuleMatch(method: "GET", path: "/api/orders"),
                    rewrite: Rewrite(active: true, mode: "replace", status: 200, bodyKind: "none")),
            ]
            let snapshot = RulesSnapshot(scenario: "baseline", notWhole: [], rules: rules)
            let entries = [
                RecentEntry(id: "newest", method: "PATCH", path: "/api/cart", status: 204),
                RecentEntry(id: "older", method: "GET", path: "/api/orders", status: 200),
            ]
            let content = BrowserContent(
                status: .intercepting, controlPort: 8088, rulesRead: .ok(snapshot), recentRead: .ok(entries),
                recentPlaceholder: "No recorded requests", scenarios: nil, busy: false, lastError: nil)
            let state = BrowserState()
            let list = RequestListController()
            for _ in 0..<2 {
                state.select(.scenario("baseline"))
                list.update(content, state: state)
                #expect(list.rows.compactMap { $0.flow?.ruleId } == ["active", "inactive"])
                state.select(.recent)
                list.update(content, state: state)
                #expect(list.rows.compactMap { $0.traffic?.id } == ["newest", "older"])
            }
        }

        /// Building a candidate rule before deciding which pane wins can leave its copy payload,
        /// step target or related links live behind a vacancy; exercise the actions after each losing branch.
        @Test func aRuleThatLosesThePaneLeavesNoActionsBehind() throws {
            let snapshot = BrowserPreview.snapshot("orders-pending")
            let original = BrowserContent(
                status: .intercepting, controlPort: 8088, rulesRead: .ok(snapshot), recentRead: .ok([]),
                recentPlaceholder: "No recorded requests", scenarios: nil, busy: false, lastError: nil)
            let ending = try #require(
                RuleFormatting.flowSections(snapshot).flatMap(\.rows).first { $0.endingTransition != nil })
            let state = BrowserState()
            let detail = RuleDetailController()
            var picked: [String] = []
            var opened: [RuleFormatting.Destination] = []
            detail.onPickStep = { step, rule in picked.append("\(step):\(rule)") }
            detail.onOpenRule = { opened.append($0) }

            for loser in ["proxy", "scenario", "recent", "missing"] {
                state.select(.scenario(snapshot.scenario))
                state.ruleSelection = .rule("ovr_orders")
                detail.update(original, state: state)
                let copy = try #require(detail.textView.subviews.compactMap { $0 as? ActionButton }.first)
                #expect(!copy.isHidden)
                #expect(!detail.stepPicker.isHidden)
                detail.stepPicker.selectItem(at: 1)
                let action = try #require(detail.stepPicker.action)
                #expect(NSApp.sendAction(action, to: detail.stepPicker.target, from: detail.stepPicker))
                #expect(picked.last == "2:ovr_orders")

                state.ruleSelection = ending.selection
                detail.update(original, state: state)
                #expect(detail.stepPicker.isHidden)
                #expect(!copy.isHidden)
                let text = detail.textView.attributedString()
                var token: Any?
                text.enumerateAttribute(.link, in: NSRange(location: 0, length: text.length)) { value, _, _ in
                    if token == nil { token = value }
                }
                let link = try #require(token)
                #expect(detail.textView(detail.textView, clickedOnLink: link, at: 0))
                #expect(opened.last?.scenario == snapshot.scenario)
                #expect(opened.last?.selection == .rule("ovr_update"))
                let titles = detail.stepPicker.itemTitles
                let selectedStep = detail.stepPicker.indexOfSelectedItem
                picked.removeAll()
                opened.removeAll()

                var content = original
                switch loser {
                case "proxy": content.status = .down
                case "scenario": state.select(.scenario("another-scenario"))
                case "recent": state.select(.recent)
                default: state.ruleSelection = .rule("missing-rule")
                }
                detail.update(content, state: state)
                #expect(copy.isHidden)
                #expect(detail.stepPicker.isHidden)
                #expect(detail.stepPicker.itemTitles == titles)
                #expect(detail.stepPicker.indexOfSelectedItem == selectedStep)
                #expect(!detail.textView.string.contains("Example order"))
                #expect(!detail.textView(detail.textView, clickedOnLink: link, at: 0))
                #expect(NSApp.sendAction(action, to: detail.stepPicker.target, from: detail.stepPicker))
                let pasteboardVersion = NSPasteboard.general.changeCount
                copy.invoke()
                #expect(NSPasteboard.general.changeCount == pasteboardVersion)
                #expect(picked.isEmpty)
                #expect(opened.isEmpty)
            }
        }

        /// Related matchers are candidates, not a chosen response; an ambiguous trigger must expose
        /// their links without adopting either candidate's body or the previously selected rule's step target.
        @Test func anAmbiguousTransitionLinksCandidatesWithoutAdoptingTheirActions() throws {
            var snapshot = BrowserPreview.snapshot("orders-pending")
            let index = try #require(snapshot.rules.firstIndex { $0.id == "ovr_update" })
            snapshot.rules[index].body = .string("First candidate payload")
            snapshot.rules[index].rewrite.bodyKind = "text"
            var alternative = snapshot.rules[index]
            alternative.id = "ovr_alternative"
            alternative.body = .string("Second candidate payload")
            snapshot.rules.append(alternative)
            let trigger = try #require(
                RuleFormatting.flowSections(snapshot).flatMap(\.rows).first { $0.transition != nil && $0.ruleId == nil }
            )
            let content = BrowserContent(
                status: .intercepting, controlPort: 8088, rulesRead: .ok(snapshot), recentRead: .ok([]),
                recentPlaceholder: "No recorded requests", scenarios: nil, busy: false, lastError: nil)
            let state = BrowserState()
            state.select(.scenario(snapshot.scenario))
            state.ruleSelection = .rule("ovr_orders")
            let detail = RuleDetailController()
            detail.update(content, state: state)
            state.ruleSelection = trigger.selection
            detail.update(content, state: state)
            let copy = try #require(detail.textView.subviews.compactMap { $0 as? ActionButton }.first)
            #expect(copy.isHidden)
            #expect(detail.stepPicker.isHidden)
            #expect(!detail.textView.string.contains("candidate payload"))
            var picked: [String] = []
            detail.onPickStep = { _, rule in picked.append(rule) }
            let action = try #require(detail.stepPicker.action)
            #expect(NSApp.sendAction(action, to: detail.stepPicker.target, from: detail.stepPicker))
            #expect(picked.isEmpty)
            let pasteboardVersion = NSPasteboard.general.changeCount
            copy.invoke()
            #expect(NSPasteboard.general.changeCount == pasteboardVersion)
            var opened: [RuleFormatting.Destination] = []
            detail.onOpenRule = { opened.append($0) }
            #expect(detail.textView(detail.textView, clickedOnLink: "lyrebird-rule:0", at: 0))
            #expect(detail.textView(detail.textView, clickedOnLink: "lyrebird-rule:1", at: 0))
            #expect(
                opened == [
                    RuleFormatting.destination(rule: "ovr_alternative", drawnFrom: snapshot.scenario),
                    RuleFormatting.destination(rule: "ovr_update", drawnFrom: snapshot.scenario),
                ])
        }

        /// An error note must not suppress the empty-traffic message, and replacing traffic with a
        /// read failure must clear native selection without sending a user-selection callback.
        @Test func trafficVacanciesKeepErrorOrderingAndSuppressSelectionCallbacks() {
            let entry = RecentEntry(id: "recorded", method: "GET", path: "/api/items", status: 200)
            var content = BrowserContent(
                status: .intercepting, controlPort: 8088, rulesRead: nil, recentRead: .ok([entry]),
                recentPlaceholder: "No recorded requests", scenarios: nil, busy: false, lastError: "Synthetic failure")
            let state = BrowserState()
            state.select(.recent)
            state.recentSelection = entry.selectionKey
            let list = RequestListController()
            var selected: [RecentEntry.Key] = []
            list.onRecent = { selected.append($0) }
            list.update(content, state: state)
            #expect(list.rows.map(\.id).first == "error")
            #expect(list.table.selectedRow == 1)

            content.recentRead = .ok([])
            list.update(content, state: state)
            #expect(list.rows.map(\.id) == ["error", "empty"])
            #expect(list.rows.last?.text.string == "No recorded requests")
            #expect(list.table.selectedRow == -1)

            content.recentRead = .ok([entry])
            list.update(content, state: state)
            #expect(list.table.selectedRow == 1)
            content.recentRead = .unavailable("Synthetic read failure")
            list.update(content, state: state)
            #expect(list.rows.map(\.id) == ["error", "vacancy"])
            #expect(list.rows.last?.text.string == "Traffic could not be read.\nSynthetic read failure")
            #expect(list.table.selectedRow == -1)
            #expect(selected.isEmpty)
        }
    }
}
