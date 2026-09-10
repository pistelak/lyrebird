import Foundation

/// Selection belongs to the reader; activation belongs to the engine.
@MainActor
final class BrowserState {
    enum Destination {
        case recent

        case scenario(String)
    }

    var destination: Destination?

    var ruleSelection: RuleFormatting.ListSelection?

    var pickedStep: RuleFormatting.StepPick?

    var recentSelection: RecentEntry.Key?

    private var selectionScenario: String?

    var showsRecent: Bool {
        destination == .recent
    }

    var scenario: String? {
        if case .scenario(let name) = destination { return name }
        return nil
    }

    func select(_ destination: Destination?) {
        guard let destination else { return }
        self.destination = destination
    }

    func reconcile(_ snapshot: RulesSnapshot?, activeScenario: String?) {
        if destination == nil, let activeScenario { destination = .scenario(activeScenario) }
        guard let snapshot, snapshot.scenario == scenario else { return }
        if selectionScenario != snapshot.scenario {
            ruleSelection = nil
            pickedStep = nil
            selectionScenario = snapshot.scenario
        }
        ruleSelection = RuleFormatting.selection(current: ruleSelection, in: snapshot)
    }

    func pickStep(_ step: Int, rule: String) {
        pickedStep = .init(scenario: scenario ?? "", rule: rule, step: step)
    }
}

extension BrowserState.Destination: Hashable {}
