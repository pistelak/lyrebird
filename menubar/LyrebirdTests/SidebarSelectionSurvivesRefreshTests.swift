import AppKit
import SwiftUI
import Testing

@testable import Lyrebird

extension AppTests {
    /// The sidebar's rows are rebuilt on every poll, two seconds apart, while someone is reading one
    /// scenario. A selection that follows the list's deselect writes would be cleared by any of
    /// them, dropping the reader back onto the active scenario mid-read.
    @MainActor
    struct SidebarSelectionSurvivesRefreshTests {
        /// Records what the sidebar writes, as it writes it: a transient nil that a later write
        /// repaired would be invisible in the resulting state.
        @MainActor
        final class Holder {
            var selection: String?
            var showsRecent = false
            var writes: [String?] = []
            var clears: Int { writes.filter { $0 == nil }.count }
        }

        struct Host: View {
            let model: AppModel
            let holder: Holder
            @State private var selection: String?
            @State private var showsRecent = false
            var body: some View {
                RulesSidebarView(
                    model: model,
                    selection: Binding(
                        get: { selection },
                        set: { new in
                            holder.writes.append(new)
                            holder.selection = new
                            selection = new
                        }),
                    showsRecent: Binding(
                        get: { showsRecent },
                        set: { new in
                            holder.showsRecent = new
                            showsRecent = new
                        })
                )
                .onAppear {
                    holder.selection = "charlie"
                    selection = "charlie"
                }
            }
        }

        /// The scenario the engine reports as active. Boxed because the stub's handler runs off the
        /// main actor while the test moves the active scenario between reads.
        final class ActiveBox: @unchecked Sendable {
            private let lock = NSLock()
            private var value = "alpha"
            var name: String {
                get { lock.withLock { value } }
                set { lock.withLock { value = newValue } }
            }
        }

        nonisolated static let names = ["alpha", "bravo", "charlie", "delta"]

        /// Fails rather than returning: a wait that gave up quietly would let every assertion after
        /// it pass on state the test never actually reached.
        private func waitUntil(
            _ description: Comment, timeout: Duration = .seconds(2), _ condition: () -> Bool
        ) async throws {
            let deadline = ContinuousClock.now.advanced(by: timeout)
            while ContinuousClock.now < deadline {
                if condition() { return }
                try await Task.sleep(for: .milliseconds(20))
            }
            try #require(condition(), description)
        }

        @Test
        func aPollingRefreshDoesNotClearTheBrowsedScenario() async throws {
            try await withAppTestEnvironment {
                // A fresh list object per read, with the active scenario moving under the reader, is
                // what the poll loop delivers.
                let active = ActiveBox()
                StubURLProtocol.install { request in
                    if request.url?.path == "/__mock__/scenarios" {
                        let rows = Self.names
                            .map { #"{"name":"\#($0)","overrideCount":1,"verified":false}"# }
                            .joined(separator: ",")
                        return (
                            Stub.response(request, 200),
                            Data(#"{"active":"\#(active.name)","scenarios":[\#(rows)]}"#.utf8)
                        )
                    }
                    return Stub.read(request)
                }
                let model = AppModel(
                    client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
                    autoStart: false, expectedFingerprint: "probe")
                await model.refresh()
                try #require(model.scenarios?.scenarios.map(\.name) == Self.names)

                let holder = Holder()
                let controller = NSHostingController(rootView: Host(model: model, holder: holder))
                let window = NSWindow(contentViewController: controller)
                window.isReleasedWhenClosed = false
                window.setContentSize(NSSize(width: 260, height: 420))
                window.orderFront(nil)
                defer { window.close() }
                try await waitUntil("the host never established a selection") { holder.selection == "charlie" }
                controller.view.layoutSubtreeIfNeeded()
                holder.writes.removeAll()

                for round in 0..<6 {
                    let moved = round.isMultiple(of: 2) ? "bravo" : "alpha"
                    active.name = moved
                    await model.refresh()
                    // Each round has to reach the model, or six refreshes that all failed would
                    // leave the selection untouched and pass.
                    try #require(model.scenarios?.active == moved, "round \(round) did not reach the model")
                    try #require(model.scenarios?.scenarios.map(\.name) == Self.names)
                    controller.view.layoutSubtreeIfNeeded()
                    try await Task.sleep(for: .milliseconds(120))
                }

                #expect(holder.selection == "charlie", "a refresh moved the reader off their scenario")
                #expect(holder.clears == 0, "a refresh cleared the selection \(holder.clears) time(s)")
                #expect(!holder.showsRecent)
                window.close()
                try await Task.sleep(for: .milliseconds(100))
            }
        }
    }
}
