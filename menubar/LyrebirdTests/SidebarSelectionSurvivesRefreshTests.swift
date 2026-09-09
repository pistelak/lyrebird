import AppKit
import SwiftUI
import Testing

@testable import Lyrebird

extension AppTests {
    /// The sidebar's rows are rebuilt on every poll, two seconds apart, while the user reads one
    /// scenario. A selection binding that accepts SwiftUI's deselect writes clears the selection on
    /// any such write, which would drop the user back onto the active scenario mid-read.
    @MainActor
    struct SidebarSelectionSurvivesRefreshTests {
        @MainActor
        final class Holder {
            var selection: String?
            var showsRecent = false
            var clears = 0
        }

        struct Host: View {
            let model: AppModel
            let holder: Holder
            @State private var selection: String?
            @State private var showsRecent = false
            var body: some View {
                RulesSidebarView(model: model, selection: $selection, showsRecent: $showsRecent)
                    .onAppear { selection = "charlie" }
                    .onChange(of: selection) { _, new in
                        holder.selection = new
                        if new == nil { holder.clears += 1 }
                    }
                    .onChange(of: showsRecent) { _, new in holder.showsRecent = new }
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

        @Test
        func aPollingRefreshDoesNotClearTheBrowsedScenario() async throws {
            try await withAppTestEnvironment {
                // A fresh list object per read, with the active scenario moving under the user, is
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
                try await Task.sleep(for: .milliseconds(400))
                controller.view.layoutSubtreeIfNeeded()
                try #require(holder.selection == "charlie", "the host did not establish a selection")

                for round in 0..<6 {
                    active.name = round.isMultiple(of: 2) ? "bravo" : "alpha"
                    await model.refresh()
                    controller.view.layoutSubtreeIfNeeded()
                    try await Task.sleep(for: .milliseconds(120))
                }

                #expect(holder.selection == "charlie", "a refresh moved the user off the scenario they were reading")
                #expect(holder.clears == 0, "a refresh cleared the selection \(holder.clears) time(s)")
                #expect(!holder.showsRecent)
                window.close()
                try await Task.sleep(for: .milliseconds(100))
            }
        }
    }
}
