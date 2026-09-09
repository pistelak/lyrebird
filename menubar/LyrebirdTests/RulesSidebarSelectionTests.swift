import SwiftUI
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct RulesSidebarSelectionTests {
        @Test
        func destinationBindingRoundTripsDeselectThenSelect() {
            var selection: String? = "baseline"
            var showsRecent = false
            let destination = RulesSidebarView.Destination.binding(
                selection: Binding(get: { selection }, set: { selection = $0 }),
                showsRecent: Binding(get: { showsRecent }, set: { showsRecent = $0 }))

            #expect(destination.wrappedValue == .scenario("baseline"))
            for name in ["orders-outage", "baseline", "empty"] {
                destination.wrappedValue = nil
                #expect(destination.wrappedValue == nil)
                #expect(selection == nil)
                #expect(!showsRecent)

                destination.wrappedValue = .scenario(name)
                #expect(destination.wrappedValue == .scenario(name))
                #expect(selection == name)
                #expect(!showsRecent)

                destination.wrappedValue = .recent
                #expect(destination.wrappedValue == .recent)
                #expect(selection == name, "Recent preserves the browsed scenario")
                #expect(showsRecent)

                destination.wrappedValue = nil
                #expect(destination.wrappedValue == nil)
                #expect(selection == nil)
                #expect(!showsRecent)

                destination.wrappedValue = .recent
                #expect(destination.wrappedValue == .recent)
                #expect(selection == nil)
                #expect(showsRecent)

                destination.wrappedValue = .scenario(name)
                #expect(destination.wrappedValue == .scenario(name))
                #expect(selection == name)
                #expect(!showsRecent)
            }
        }
    }
}
