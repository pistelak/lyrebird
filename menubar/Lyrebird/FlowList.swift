import Foundation

extension RuleFormatting {
    struct FlowListRow: Identifiable {
        var selection: ListSelection
        var number: Int?
        var request: RequestLine
        var status: Int?
        var subtitle: String
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

    /// Numbering describes configured order, not observed traffic. Rule and step identity stay
    /// separate from the visible number; flow row 3 may open sequence step 2.
    static func flowSections(_ snapshot: RulesSnapshot, query: String = "") -> [FlowListSection] {
        let outline = outline(snapshot)
        let kinds = Dictionary(
            snapshot.rules.map { ($0.id, responseKind($0.rewrite)) }, uniquingKeysWith: { first, _ in first })
        var sections: [FlowListSection] = outline.sequences.map { sequence in
            let rows = sequence.flow.enumerated().map { index, item -> FlowListRow in
                switch item {
                case .response(let state):
                    return FlowListRow(
                        selection: .flow(sequence.id, item.id), number: index + 1,
                        request: sequence.request, status: state.status,
                        subtitle: state.number == 1 ? "Initial response" : "Response after advancement",
                        conditions: sequence.conditions, inactive: sequence.inactive,
                        ruleId: sequence.id, step: state.number,
                        endingTransition: state.number == sequence.states.last?.number ? state.transition : nil,
                        responseKind: kinds[sequence.id])
                case .trigger(_, let transition):
                    let candidate = transition.relatedResponses.count == 1 ? transition.relatedResponses.first : nil
                    let response = candidate?.inactive == false ? candidate : nil
                    let subtitle: String
                    if response != nil {
                        subtitle = "Advances the sequence"
                    } else if !transition.relatedResponses.isEmpty && transition.relatedResponses.allSatisfy(\.inactive)
                    {
                        subtitle = "Advances the sequence · no active response rule identified"
                    } else {
                        subtitle =
                            transition.relatedResponses.isEmpty
                            ? "Advances the sequence · response not identified"
                            : "Advances the sequence · multiple response rules"
                    }
                    return FlowListRow(
                        selection: .flow(sequence.id, item.id), number: index + 1,
                        request: transition.request, status: response?.status,
                        subtitle: subtitle, conditions: response?.conditions ?? transition.conditions,
                        inactive: sequence.inactive,
                        ruleId: response?.id, transition: transition, responseKind: response.flatMap { kinds[$0.id] })
                }
            }
            var footer = sequence.advanceNote ?? "Reads repeat until the advance request arrives."
            if let ending = sequence.footer {
                if let trigger = sequence.states.last?.transition {
                    footer += " After another matching \(trigger.request.method) request: \(ending.lowercased())."
                } else {
                    footer += " \(ending)."
                }
            }
            return FlowListSection(
                id: .sequence(sequence.id),
                title: outline.sequences.count == 1
                    ? "Sequence · \(sequence.states.count) response states"
                    : "Sequence · " + sequence.request.method + " " + sequence.request.path,
                rows: rows, footer: footer)
        }
        let embedded = Set(sections.flatMap(\.rows).compactMap(\.ruleId))
        let others = outline.otherRules.filter { !embedded.contains($0.id) }.map { rule in
            FlowListRow(
                selection: .rule(rule.id), request: rule.request, status: rule.status,
                subtitle: rule.behaviour, conditions: rule.conditions, inactive: rule.inactive, ruleId: rule.id,
                responseKind: kinds[rule.id])
        }
        if !others.isEmpty {
            sections.append(
                FlowListSection(
                    id: .responses, title: sections.isEmpty ? "Responses" : "Other rules", rows: others))
        }
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return sections }
        let matchingRules = Set(filter(snapshot.rules, query: query).map(\.id))
        return sections.compactMap { section in
            var section = section
            section.rows = section.rows.filter { row in
                row.ruleId.map { matchingRules.contains($0) } == true
                    || [row.request.method, row.request.path, row.subtitle, row.status.map(String.init) ?? ""]
                        .contains { $0.localizedCaseInsensitiveContains(needle) }
            }
            return section.rows.isEmpty ? nil : section
        }
    }

    static func flowRow(_ selection: ListSelection?, in snapshot: RulesSnapshot?) -> FlowListRow? {
        guard let selection, let snapshot else { return nil }
        return flowSections(snapshot).flatMap(\.rows).first { $0.selection == selection }
    }
}
