import Foundation

extension RuleFormatting {
    struct FlowListRow: Identifiable {
        var selection: ListSelection
        var number: Int?
        var request: RequestLine
        var status: Int?
        var subtitle: String
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

    /// Preserve configured order and trigger identity; see flowListRowsOpenTheCorrectRuleAndStep.
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
        let others = outline.otherRules.map { rule in
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
            // Nothing borrowed from a candidate: one matched by method and path alone may not be the
            // rule that answers, and a 204 badge here said it would — see
            // aTriggerIncludesItsConditionalResponseWithoutNarrowingAdvancement.
            return FlowListRow(
                selection: .flow(sequence.id, item.id), number: number,
                request: transition.request, status: nil,
                subtitle: triggerSubtitle(transition), delay: nil,
                conditions: transition.conditions, inactive: sequence.inactive,
                ruleId: nil, transition: transition, responseKind: nil)
        }
    }

    private static func triggerSubtitle(_ transition: ScenarioOutline.Transition) -> String {
        let candidates = transition.relatedResponses
        if candidates.isEmpty {
            return "Advances the sequence · response not identified"
        }
        if candidates.allSatisfy(\.inactive) {
            return "Advances the sequence · no active response rule identified"
        }
        return candidates.count == 1
            ? "Advances the sequence"
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
