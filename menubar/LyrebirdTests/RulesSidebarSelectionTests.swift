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
        @Test
        func destinationBindingKeepsTheBrowsedScenarioThroughADeselect() {
            var selection: String? = "baseline"
            var showsRecent = false
            let destination = RulesSidebarView.Destination.binding(
                selection: Binding(get: { selection }, set: { selection = $0 }),
                showsRecent: Binding(get: { showsRecent }, set: { showsRecent = $0 }))

            #expect(destination.wrappedValue == .scenario("baseline"))

            destination.wrappedValue = nil

            #expect(destination.wrappedValue == .scenario("baseline"), "a deselect moved the browsed scenario")
            #expect(selection == "baseline")
            #expect(!showsRecent)

            destination.wrappedValue = .scenario("orders-outage")

            #expect(destination.wrappedValue == .scenario("orders-outage"))
            #expect(selection == "orders-outage")
            #expect(!showsRecent)
        }

        @Test
        func recentAndScenariosFollowEachOther() {
            var selection: String?
            var showsRecent = false
            let destination = RulesSidebarView.Destination.binding(
                selection: Binding(get: { selection }, set: { selection = $0 }),
                showsRecent: Binding(get: { showsRecent }, set: { showsRecent = $0 }))

            destination.wrappedValue = .recent
            #expect(destination.wrappedValue == .recent)
            #expect(showsRecent)

            destination.wrappedValue = .scenario("orders-outage")
            #expect(destination.wrappedValue == .scenario("orders-outage"))
            #expect(!showsRecent)
            #expect(selection == "orders-outage")

            // Recent keeps the scenario underneath it, so going back does not need another read.
            destination.wrappedValue = .recent
            #expect(destination.wrappedValue == .recent)
            #expect(selection == "orders-outage")
        }
    }
}
