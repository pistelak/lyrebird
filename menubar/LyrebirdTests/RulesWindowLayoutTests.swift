import AppKit
import SwiftUI
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct RulesWindowLayoutTests {
        @Test
        func emptyScenariosKeepTheSplitViewInsideItsWindow() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    if request.url?.path == "/__mock__/rules" {
                        let name =
                            URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                            .queryItems?.first { $0.name == "scenario" }?.value ?? "baseline"
                        var snapshot = try JSONDecoder().decode(
                            RulesSnapshot.self, from: Data(RulesFixture.snapshot.utf8))
                        snapshot.scenario = name
                        snapshot.notWhole = []
                        if name == "default" { snapshot.rules = [] }
                        return (Stub.response(request, 200), try JSONEncoder().encode(snapshot))
                    }
                    return Stub.read(request)
                }
                let model = AppModel(
                    client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
                    autoStart: false, expectedFingerprint: "probe")
                let controller = NSHostingController(
                    rootView: RulesWindowView(model: model, searchFocus: SearchFocus()))
                let window = NSWindow(contentViewController: controller)
                window.isReleasedWhenClosed = false
                window.setContentSize(NSSize(width: 1120, height: 700))
                window.orderFront(nil)
                defer { window.close() }
                try await Task.sleep(for: .milliseconds(150))
                let original = window.frame
                for scenario in ["baseline", "default", "baseline", "default"] {
                    await model.browse(scenario)
                    try await Task.sleep(for: .milliseconds(100))
                    guard case .ok(let snapshot) = model.rulesRead else {
                        Issue.record("Scenario did not load: \(scenario)")
                        return
                    }
                    try #require(snapshot.scenario == scenario)
                    try #require(snapshot.rules.isEmpty == (scenario == "default"))
                    controller.view.layoutSubtreeIfNeeded()
                    let split = try #require(findSplit(in: controller.view))
                    let frame = split.convert(split.bounds, to: nil)
                    #expect(window.frame == original)
                    #expect(frame.height <= window.frame.height + 1, "Scenario: \(scenario)")
                    #expect(frame.minY >= -1, "Scenario: \(scenario)")
                    #expect(frame.maxY <= window.frame.height + 1, "Scenario: \(scenario)")
                }
                window.close()
                try await Task.sleep(for: .milliseconds(100))
            }
        }

        private func findSplit(in view: NSView) -> NSSplitView? {
            if let split = view as? NSSplitView { return split }
            return view.subviews.lazy.compactMap { self.findSplit(in: $0) }.first
        }
    }
}
