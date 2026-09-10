import Foundation

extension RuleFormatting {
    struct FlowListRow: Identifiable {
        var selection: ListSelection
        var number: Int?
        var request: RequestLine
        var status: Int?
        var subtitle: String
        /// How long the proxy holds this response, as a badge reads it. Nil where nothing waits —
        /// and on a trigger whose response rule was not identified, whose delay is not this app's
        /// to guess.
        var delay: String?
        var conditions: [Fact]
        var inactive: Bool
        var ruleId: String?
        var step: Int?
        var transition: ScenarioOutline.Transition?
        var endingTransition: ScenarioOutline.Transition? = nil
        var responseKind: ResponseKind? = nil
        var id: ListSelection { selection }
    }

    struct FlowListSection: Identifiable {
        enum ID: Hashable {
            case sequence(String)
            case responses
        }

        var id: ID
        var title: String
        var rows: [FlowListRow]
        var footer: String?
    }

    /// Preserve configured order and trigger identity; see flowListRowsOpenTheCorrectRuleAndStepAndDoNotDuplicateTheTrigger.
    static func flowSections(_ snapshot: RulesSnapshot) -> [FlowListSection] {
        let outline = outline(snapshot)
        let kinds = Dictionary(
            snapshot.rules.map { ($0.id, responseKind($0.rewrite)) }, uniquingKeysWith: { first, _ in first })
        var sections: [FlowListSection] = outline.sequences.map { sequence in
            let rows = sequence.flow.enumerated().map { index, item -> FlowListRow in
                sequenceRow(item, number: index + 1, in: sequence, kinds: kinds)
            }
            return FlowListSection(
                id: .sequence(sequence.id),
                title: outline.sequences.count == 1
                    ? "Sequence · \(sequence.states.count) response states"
                    : "Sequence · " + sequence.request.method + " " + sequence.request.path,
                rows: rows, footer: sequenceFooter(sequence))
        }
        let embedded = Set(sections.flatMap(\.rows).compactMap(\.ruleId))
        let others = outline.otherRules.filter { !embedded.contains($0.id) }.map { rule in
            FlowListRow(
                selection: .rule(rule.id), request: rule.request, status: rule.status,
                subtitle: rule.behaviour, delay: rule.delay, conditions: rule.conditions,
                inactive: rule.inactive, ruleId: rule.id,
                responseKind: kinds[rule.id])
        }
        if !others.isEmpty {
            sections.append(
                FlowListSection(
                    id: .responses, title: sections.isEmpty ? "Responses" : "Other rules", rows: others))
        }
        return sections
    }

    private static func sequenceRow(
        _ item: ScenarioOutline.FlowItem, number: Int, in sequence: ScenarioOutline.Sequence,
        kinds: [String: ResponseKind]
    ) -> FlowListRow {
        switch item {
        case .response(let state):
            return FlowListRow(
                selection: .flow(sequence.id, item.id), number: number,
                request: sequence.request, status: state.status,
                subtitle: state.number == 1 ? "Initial response" : "Response after advancement",
                delay: sequence.delay, conditions: sequence.conditions, inactive: sequence.inactive,
                ruleId: sequence.id, step: state.number,
                endingTransition: state.number == sequence.states.last?.number ? state.transition : nil,
                responseKind: kinds[sequence.id])
        case .trigger(_, let transition):
            let candidate = transition.relatedResponses.count == 1 ? transition.relatedResponses.first : nil
            let response = candidate?.inactive == false ? candidate : nil
            return FlowListRow(
                selection: .flow(sequence.id, item.id), number: number,
                request: transition.request, status: response?.status,
                subtitle: triggerSubtitle(transition, response: response), delay: response?.delay,
                conditions: response?.conditions ?? transition.conditions,
                inactive: sequence.inactive,
                ruleId: response?.id, transition: transition, responseKind: response.flatMap { kinds[$0.id] })
        }
    }

    private static func triggerSubtitle(
        _ transition: ScenarioOutline.Transition, response: ScenarioOutline.RuleSummary?
    ) -> String {
        if response != nil {
            return "Advances the sequence"
        }
        if !transition.relatedResponses.isEmpty && transition.relatedResponses.allSatisfy(\.inactive) {
            return "Advances the sequence · no active response rule identified"
        }
        return transition.relatedResponses.isEmpty
            ? "Advances the sequence · response not identified"
            : "Advances the sequence · multiple response rules"
    }

    private static func sequenceFooter(_ sequence: ScenarioOutline.Sequence) -> String {
        var footer = sequence.advanceNote ?? "Reads repeat until the advance request arrives."
        if let ending = sequence.footer {
            if let trigger = sequence.states.last?.transition {
                footer += " After another matching \(trigger.request.method) request: \(ending.lowercased())."
            } else {
                footer += " \(ending)."
            }
        }
        return footer
    }

    static func flowRow(_ selection: ListSelection?, in snapshot: RulesSnapshot?) -> FlowListRow? {
        guard let selection, let snapshot else { return nil }
        return flowSections(snapshot).flatMap(\.rows).first { $0.selection == selection }
    }
}
