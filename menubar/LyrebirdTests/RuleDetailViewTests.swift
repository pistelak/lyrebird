import SwiftUI
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct RuleDetailViewTests {
        @Test func pickingAStepBringsItsResponseIntoView() async throws {
            try await withAppTestEnvironment {
                // The scroll is a closure so this can press what a `Form` will not let a test press.
                var picked: RuleFormatting.StepPick?
                var scrolled: [String] = []
                let rule = RuleRow(
                    id: "ovr_items", rewrite: Rewrite(active: true, mode: "replace", bodyKind: "none"))
                let view = RuleDetailView(
                    model: AppModel(autoStart: false), ruleSelection: .rule("ovr_items"),
                    pickedStep: Binding(get: { picked }, set: { picked = $0 }), openRule: { _ in })

                view.pickStep(3, of: rule) { scrolled.append($0) }

                #expect(picked?.rule == "ovr_items")
                #expect(picked?.step == 3)
                #expect(scrolled == [RuleDetailView.responseSectionId])
            }
        }
    }
}
