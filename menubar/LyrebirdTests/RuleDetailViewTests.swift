import SwiftUI
import XCTest

@testable import Lyrebird

/// What a click on a step does. The response section is below the selector, so on any pane taller
/// than a few rows the section that changed is off the bottom of it and the click reads as having
/// done nothing.
@MainActor
final class RuleDetailViewTests: XCTestCase {

    func testPickingAStepBringsItsResponseIntoView() {
        // The scroll is a closure so this can press what a `Form` will not let a test press.
        var picked: RuleFormatting.StepPick?
        var scrolled: [String] = []
        let rule = RuleRow(
            id: "ovr_items", rewrite: Rewrite(active: true, mode: "replace", bodyKind: "none"))
        let view = RuleDetailView(
            model: AppModel(autoStart: false), ruleSelection: .rule("ovr_items"),
            pickedStep: Binding(get: { picked }, set: { picked = $0 }), openRule: { _ in })

        view.pickStep(3, of: rule) { scrolled.append($0) }

        XCTAssertEqual(picked?.rule, "ovr_items")
        XCTAssertEqual(picked?.step, 3)
        XCTAssertEqual(scrolled, [RuleDetailView.responseSectionId])
    }
}
