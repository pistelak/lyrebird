import Foundation
import Observation

/// Selection belongs to the reader; activation belongs to the engine.
@MainActor
final class BrowserState {
    enum Destination: Hashable {
        case recent
        case scenario(String)
    }

    var destination: Destination?
    var ruleSelection: RuleFormatting.ListSelection?
    var pickedStep: RuleFormatting.StepPick?
    var recentSelection: RecentEntry.Key?
    var rulesQuery = ""
    var recentQuery = ""
    private var selectionScenario: String?

    var showsRecent: Bool { destination == .recent }
    var scenario: String? {
        if case .scenario(let name) = destination { return name }
        return nil
    }
    var query: String {
        get { showsRecent ? recentQuery : rulesQuery }
        set {
            if showsRecent { recentQuery = newValue } else { rulesQuery = newValue }
        }
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

/// Observation is re-armed after mutations have committed. The generation invalidates queued
/// callbacks on close, so a closed window cannot resume rendering; see BrowserControllerTests.
@MainActor
final class ModelObservation {
    private var generation = 0
    private var render: (() -> Void)?

    func start(_ render: @escaping () -> Void) {
        stop()
        self.render = render
        track(generation)
    }

    func stop() {
        generation &+= 1
        render = nil
    }

    private func track(_ expected: Int) {
        guard generation == expected, let render else { return }
        withObservationTracking {
            render()
        } onChange: { [weak self] in
            Task { @MainActor [weak self] in self?.track(expected) }
        }
    }
}
