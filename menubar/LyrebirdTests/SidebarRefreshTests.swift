import AppKit
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct SidebarRefreshTests {
        /// A rebuild must read expansion from the old items and restore it on the new ones before selecting a leaf;
        /// every delegate callback during that transaction must remain a refresh rather than a user navigation.
        @Test func rebuildingTheSidebarPreservesCollapsedFoldersWithoutNavigating() throws {
            let sidebar = ScenarioSidebarController()
            var selections: [BrowserState.Destination] = []
            sidebar.onSelect = { selections.append($0) }
            var list = ScenarioList(
                active: "orders/pending",
                scenarios: [
                    .init(name: "orders/pending", overrideCount: 1, verified: false),
                    .init(name: "orders/complete", overrideCount: 1, verified: true),
                    .init(name: "checkout/pending", overrideCount: 1, verified: false),
                ])
            sidebar.update(list, selection: .scenario("orders/complete"), problems: [:], stale: false)
            let oldCheckout = try item("group:checkout", in: sidebar)
            // Only the folder is collapsed. Collapsing "Scenarios" as well would hide every folder,
            // and a folder AppKit is not showing reports itself as not expanded — so the rebuild would
            // have no expansion to restore and the selected row would not exist to be checked.
            sidebar.outline.collapseItem(oldCheckout)
            selections.removeAll()

            list.scenarios.insert(.init(name: "default", overrideCount: 0, verified: true), at: 0)
            list.scenarios.append(.init(name: "archive/pending", overrideCount: 1, verified: true))
            sidebar.update(list, selection: .scenario("orders/complete"), problems: [:], stale: false)

            let checkout = try item("group:checkout", in: sidebar)
            #expect(checkout !== oldCheckout)
            #expect(!sidebar.outline.isItemExpanded(checkout))
            #expect(sidebar.outline.isItemExpanded(try item("scenarios", in: sidebar)))
            #expect(sidebar.outline.isItemExpanded(try item("group:orders", in: sidebar)))
            #expect(sidebar.outline.isItemExpanded(try item("group:archive", in: sidebar)))
            let selected = try #require(
                sidebar.outline.item(atRow: sidebar.outline.selectedRow) as? ScenarioSidebarController.Item)
            #expect(selected.destination == .scenario("orders/complete"))
            #expect(selections.isEmpty)

            sidebar.update(list, selection: .scenario("checkout/pending"), problems: [:], stale: false)
            #expect(
                sidebar.outline.item(atRow: sidebar.outline.selectedRow) as? ScenarioSidebarController.Item === selected
            )
            sidebar.update(list, selection: .scenario("missing"), problems: [:], stale: false)
            #expect(sidebar.outline.selectedRow == -1)
            #expect(selections.isEmpty)
            sidebar.outline.selectRowIndexes(
                IndexSet(integer: sidebar.outline.row(forItem: try item("recent", in: sidebar))),
                byExtendingSelection: false)
            #expect(selections == [.recent])
        }

        /// Active, warning and stale changes must repaint existing items without rebuilding the tree or emitting
        /// navigation, even when the selected scenario becomes active during the refresh.
        @Test func sidebarAppearanceRefreshKeepsItemsAndSelection() throws {
            let sidebar = ScenarioSidebarController()
            var selections: [BrowserState.Destination] = []
            sidebar.onSelect = { selections.append($0) }
            var list = ScenarioList(
                active: "default",
                scenarios: [
                    .init(name: "default", overrideCount: 0, verified: true),
                    .init(name: "orders/pending", overrideCount: 1, verified: false),
                ])
            sidebar.update(list, selection: .scenario("orders/pending"), problems: [:], stale: false)
            let selected = try item("scenario:orders/pending", in: sidebar)
            list.active = "orders/pending"
            sidebar.update(
                list, selection: selected.destination, problems: ["orders/pending": ["A rule was dropped"]], stale: true
            )
            #expect(try item("scenario:orders/pending", in: sidebar) === selected)
            #expect(
                sidebar.outline.item(atRow: sidebar.outline.selectedRow) as? ScenarioSidebarController.Item === selected
            )
            let cell = try #require(
                sidebar.outlineView(sidebar.outline, viewFor: sidebar.outline.outlineTableColumn, item: selected)
                    as? NSTableCellView)
            #expect(cell.textField?.stringValue == "pending  ⚠")
            #expect(cell.textField?.textColor == .secondaryLabelColor)
            #expect(cell.imageView?.image != nil)
            #expect(cell.toolTip == "orders/pending\nActive scenario\nA rule was dropped")
            #expect(selections.isEmpty)
        }

        private func item(_ id: String, in sidebar: ScenarioSidebarController) throws -> ScenarioSidebarController.Item
        {
            try #require(
                (0..<sidebar.outline.numberOfRows).compactMap {
                    sidebar.outline.item(atRow: $0) as? ScenarioSidebarController.Item
                }.first { $0.id == id })
        }
    }
}
