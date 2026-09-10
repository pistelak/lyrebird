import AppKit
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct BrowserDesignTests {
        @Test func nestedSidebarHeadersFitInsideTheirRows() async throws {
            try await withAppTestEnvironment {
                let sidebar = ScenarioSidebarController()
                let window = NSWindow(contentViewController: sidebar)
                window.isReleasedWhenClosed = false
                window.setContentSize(NSSize(width: 230, height: 600))
                window.orderFront(nil)
                defer { window.close() }
                let names = ["default", "orders/pending", "orders/complete", "account/settings", "account/profile"]
                sidebar.update(
                    ScenarioList(
                        active: "default", scenarios: names.map { .init(name: $0, overrideCount: 1, verified: false) }),
                    selection: .scenario("orders/pending"), problems: [:], stale: false)
                window.contentView?.layoutSubtreeIfNeeded()
                var groups = 0
                for row in 0..<sidebar.outline.numberOfRows {
                    let item = try #require(sidebar.outline.item(atRow: row) as? ScenarioSidebarController.Item)
                    guard item.destination == nil else { continue }
                    groups += 1
                    let cell = try #require(
                        sidebar.outline.view(atColumn: 0, row: row, makeIfNecessary: true) as? NSTableCellView)
                    cell.layoutSubtreeIfNeeded()
                    let label = try #require(cell.textField)
                    #expect(label.frame.height >= label.intrinsicContentSize.height)
                    #expect(cell.bounds.insetBy(dx: -1, dy: -1).contains(label.frame))
                }
                #expect(groups == 3)
            }
        }

        @Test func requestColumnReflowsWhenItsViewportShrinks() async throws {
            try await withAppTestEnvironment {
                let model = AppModel(autoStart: false, expectedFingerprint: "design")
                model.healthRead = .up(Health(proxyUp: true, intercepting: true, profileFingerprint: "design"))
                let snapshot = BrowserPreview.snapshot("remove-an-item")
                model.rulesRead = .ok(snapshot)
                model.scenarios = ScenarioList(
                    active: snapshot.scenario,
                    scenarios: [
                        ScenarioSummary(
                            name: snapshot.scenario, overrideCount: 2, verified: false,
                            notes: String(
                                repeating: "Read the list, delete an item, then read the list again. ", count: 12))
                    ])
                let state = BrowserState()
                state.select(.scenario(snapshot.scenario))
                state.reconcile(snapshot, activeScenario: snapshot.scenario)
                let controller = RequestListController()
                let window = NSWindow(contentViewController: controller)
                window.isReleasedWhenClosed = false
                window.orderFront(nil)
                defer { window.close() }
                controller.update(model: model, state: state)
                var wideHeight: CGFloat = 0
                for width: CGFloat in [600, 300, 480, 300] {
                    window.setContentSize(NSSize(width: width, height: 800))
                    window.contentView?.layoutSubtreeIfNeeded()
                    let table = controller.table
                    let column = try #require(table.tableColumns.first)
                    #expect(abs(column.width - controller.scrollView.contentSize.width) < 1)
                    let row = try #require(controller.rows.firstIndex { $0.id == "notes" })
                    let cell = try #require(table.view(atColumn: 0, row: row, makeIfNecessary: true) as? TextCell)
                    cell.layoutSubtreeIfNeeded()
                    #expect(cell.frame.width <= controller.scrollView.contentSize.width + 1)
                    #expect(cell.label.frame.maxX <= cell.bounds.maxX)
                    let height = table.rect(ofRow: row).height
                    if width == 600 { wideHeight = height }
                    if width == 300 { #expect(height > wideHeight + 80) }
                }
            }
        }

        @Test func detailCardsKeepBodyCopyInsideTheResponseAndPreserveScrollOnRefresh() async throws {
            try await withAppTestEnvironment {
                let model = AppModel(autoStart: false, expectedFingerprint: "design")
                model.healthRead = .up(Health(proxyUp: true, intercepting: true, profileFingerprint: "design"))
                let snapshot = BrowserPreview.snapshot("remove-an-item")
                model.rulesRead = .ok(snapshot)
                let state = BrowserState()
                state.select(.scenario(snapshot.scenario))
                state.reconcile(snapshot, activeScenario: snapshot.scenario)
                let detail = RuleDetailController()
                let window = NSWindow(contentViewController: detail)
                window.isReleasedWhenClosed = false
                window.setContentSize(NSSize(width: 390, height: 460))
                window.orderFront(nil)
                defer { window.close() }
                detail.update(model: model, state: state)
                window.contentView?.layoutSubtreeIfNeeded()
                #expect(detail.textView.layoutManager is DetailLayoutManager)
                let storage = try #require(detail.textView.textStorage)
                let body = (storage.string as NSString).range(of: "\"items\"")
                let request = (storage.string as NSString).range(of: "/api/items")
                #expect(storage.attribute(.detailCard, at: body.location, effectiveRange: nil) != nil)
                #expect(storage.attribute(.detailCard, at: request.location, effectiveRange: nil) != nil)
                let copy = try #require(detail.textView.subviews.compactMap { $0 as? NSButton }.first)
                #expect(copy.title == "Copy")
                #expect(copy.frame.minY > 100)
                #expect(copy.frame.maxX <= detail.textView.bounds.width)
                let style = try #require(
                    storage.attribute(.paragraphStyle, at: body.location, effectiveRange: nil) as? NSParagraphStyle)
                #expect(style.lineBreakMode == .byWordWrapping)
                detail.scrollView.contentView.scroll(to: NSPoint(x: 0, y: 100))
                let position = detail.scrollView.contentView.bounds.origin
                detail.update(model: model, state: state)
                #expect(detail.scrollView.contentView.bounds.origin == position)
                let selectedText = detail.textView.string
                #expect(selectedText.contains("Notebook"))
                #expect(selectedText.contains("Sequence"))
            }
        }
    }
}
