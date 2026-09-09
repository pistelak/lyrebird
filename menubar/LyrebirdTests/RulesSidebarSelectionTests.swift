import SwiftUI
import Testing

@testable import Lyrebird

extension AppTests {
    /// The list writes a deselect before it writes the row that was clicked. Acting on that first
    /// write pointed the window at the active scenario for the moment between the two, which the
    /// window reads as a scenario change: it re-read the rules and the content moved under the
    /// pointer on every click.
    @MainActor
    struct RulesSidebarSelectionTests {
        @MainActor
        final class Box<Value> {
            var value: Value
            init(_ value: Value) { self.value = value }
        }

        @Test
        func destinationBindingKeepsTheBrowsedScenarioThroughADeselect() {
            let selection = Box<String?>("baseline")
            let showsRecent = Box(false)
            let destination = RulesSidebarView.Destination.binding(
                selection: Binding(get: { selection.value }, set: { selection.value = $0 }),
                showsRecent: Binding(get: { showsRecent.value }, set: { showsRecent.value = $0 }))

            #expect(destination.wrappedValue == .scenario("baseline"))

            destination.wrappedValue = nil

            #expect(destination.wrappedValue == .scenario("baseline"), "a deselect moved the browsed scenario")
            #expect(selection.value == "baseline")
            #expect(!showsRecent.value)

            destination.wrappedValue = .scenario("orders-outage")

            #expect(destination.wrappedValue == .scenario("orders-outage"))
            #expect(selection.value == "orders-outage")
            #expect(!showsRecent.value)
        }

        @Test
        func recentAndScenariosFollowEachOther() {
            let selection = Box<String?>(nil)
            let showsRecent = Box(false)
            let destination = RulesSidebarView.Destination.binding(
                selection: Binding(get: { selection.value }, set: { selection.value = $0 }),
                showsRecent: Binding(get: { showsRecent.value }, set: { showsRecent.value = $0 }))

            destination.wrappedValue = .recent
            #expect(destination.wrappedValue == .recent)
            #expect(showsRecent.value)

            destination.wrappedValue = .scenario("orders-outage")
            #expect(destination.wrappedValue == .scenario("orders-outage"))
            #expect(!showsRecent.value)
            #expect(selection.value == "orders-outage")

            // Recent keeps the scenario underneath it, so going back does not need another read.
            destination.wrappedValue = .recent
            #expect(destination.wrappedValue == .recent)
            #expect(selection.value == "orders-outage")
        }
    }
}
