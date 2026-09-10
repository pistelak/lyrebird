import Foundation

struct RequestLine: Equatable {
    var method: String
    var path: String
}

/// Configured responses and their advance matchers, not a transcript of observed traffic.
struct ScenarioOutline: Equatable {
    var sequences: [Sequence]
    var otherRules: [RuleSummary]

    struct RuleSummary: Equatable, Identifiable {
        var id: String
        var request: RequestLine
        var conditions: [RuleFormatting.Fact]
        var behaviour: String
        var inactive: Bool
        var status: Int?
        /// `3 s`, drawn as a badge of its own where a rule is listed. Nil when nothing waits.
        var delay: String?

        /// The behaviour and its delay in one line, for the places that draw a sentence where a row
        /// draws a badge. Empty behaviour stays empty: `" after 3 s"` names no rule — see
        /// `aCandidateResponseInTheDetailPaneStillReadsAsOneSentence`.
        var line: String {
            behaviour.isEmpty ? "" : behaviour + RuleFormatting.delaySuffix(delay)
        }
    }

    struct Sequence: Equatable, Identifiable {
        var id: String
        var request: RequestLine
        var conditions: [RuleFormatting.Fact]
        var advanceNote: String?
        var inactive: Bool
        var states: [State]
        var footer: String?
        /// The rule's delay, which every one of its steps waits: the engine refuses a `delayMs` on a
        /// step precisely so one rule cannot hold two different answers for two different times.
        var delay: String?

        var flow: [FlowItem] {
            states.flatMap { state -> [FlowItem] in
                var items: [FlowItem] = [.response(state)]
                if state.id != states.last?.id, let transition = state.transition {
                    items.append(.trigger(after: state.id, transition))
                }
                return items
            }
        }
    }

    enum FlowItem: Identifiable {
        case response(State)
        case trigger(after: Int, Transition)

        var id: String {
            switch self {
            case .response(let state): return "response-\(state.id)"
            case .trigger(let state, _): return "trigger-\(state)"
            }
        }
    }

    struct State: Equatable, Identifiable {
        var number: Int
        var transition: Transition?
        var status: Int?
        var id: Int { number }
    }

    struct Transition: Equatable {
        var request: RequestLine
        var conditions: [RuleFormatting.Fact]
        /// Same explicit method and path; these candidates do not predict which rule wins.
        var relatedResponses: [RuleSummary]
    }
}

extension RuleFormatting {

    // MARK: - Response descriptions

    /// The method and path a rule matches. `ANY` and `*` rather than blanks: a rule with neither
    /// answers every intercepted request, and a gap there reads as a value that failed to render.
    static func requestLine(_ match: RuleMatch?) -> RequestLine {
        RequestLine(method: method(of: match), path: path(of: match))
    }

    /// What the matcher pins beyond its method and path, one labelled line each.
    /// See `ScenarioOutlineTests`.
    static func conditionLines(_ match: RuleMatch?) -> [Fact] {
        var lines: [Fact] = []
        let query = (match?.query ?? [:]).keys.sorted()
        if !query.isEmpty {
            lines.append(
                Fact("Query", query.map { "\($0) = \(scalarText(match?.query?[$0]))" }.joined(separator: " · ")))
        }
        if let contains = match?.bodyContains, !contains.isEmpty {
            lines.append(Fact("Body contains", "\"\(contains)\""))
        }
        return lines
    }

    /// What the rule does, in one line: `Returns 200`, `Patches the real response · 3 keys`,
    /// `Sequence · 2 steps`. The delay is left out because every list that shows a rule draws
    /// `delayLabel` beside it, and a row carrying it in both places says it twice — see
    /// `theBehaviourLineLeavesTheDelayToTheBadgeBesideIt`. A pane that draws no badge wants
    /// `behaviourSentence`.
    static func behaviourLine(_ rewrite: Rewrite) -> String {
        if let sequence = rewrite.sequence {
            return "Sequence · \(sequence.steps.count) \(sequence.steps.count == 1 ? "step" : "steps")"
        }
        if rewrite.mode == "patch" {
            let keys = rewrite.patchKeys.map { " · \($0) \($0 == 1 ? "key" : "keys")" } ?? ""
            return "Patches the real response" + keys
        }
        if let status = rewrite.status {
            return "Returns \(status)"
        }
        return ""
    }

    /// `60 s (capped)` — how long the proxy holds a matched response, and whether that is less than
    /// the rule asked for. Nil rather than `0 ms` when nothing waits: a badge over a rule that
    /// answers at once would name a delay the proxy does not apply.
    /// See `ScenarioOutlineTests`.
    static func delayLabel(_ rewrite: Rewrite) -> String? {
        guard let delay = rewrite.delayMs, delay > 0 else { return nil }
        return seconds(delay) + (rewrite.delayCapped == true ? " (capped)" : "")
    }

    /// ` after 3 s`, the delay spelled into a sentence rather than drawn as a badge. One
    /// construction, so the response pane, the candidate button and the text a search reads cannot
    /// come to word the same wait differently.
    static func delaySuffix(_ label: String?) -> String {
        label.map { " after \($0)" } ?? ""
    }

    static func delayPhrase(_ rewrite: Rewrite) -> String {
        delaySuffix(delayLabel(rewrite))
    }

    /// What the rule does *and* how long it is held, for a pane that draws a line of text where a
    /// row draws a badge. The detail pane lost the delay entirely when it was split out of
    /// `behaviourLine` — see `theResponsePaneStillSaysTheWaitTheRowShowsAsABadge`.
    static func behaviourSentence(_ rewrite: Rewrite) -> String {
        let line = behaviourLine(rewrite)
        return line.isEmpty ? line : line + delayPhrase(rewrite)
    }

    /// What one step of a sequence returns. The same words a rule's own behaviour line uses, so a
    /// step and a rule cannot come to read differently.
    static func stepBehaviourLine(_ step: StepSummary) -> String {
        step.status.map { "Returns \($0)" } ?? ""
    }

    /// `1 s`, `1.5 s`, `250 ms` — a delay in the unit a reader thinks in.
    static func seconds(_ milliseconds: Int) -> String {
        guard milliseconds >= 1000 else { return "\(milliseconds) ms" }
        let value = Double(milliseconds) / 1000
        return value == value.rounded() ? "\(Int(value)) s" : String(format: "%.1f s", value)
    }

    /// A patch's supplementary clauses: `sets status to 503 · appends to arrays · JSON responses
    /// only`. Empty for anything else, and each clause only where the field is set.
    static func clauseLine(_ rewrite: Rewrite, includeApplicability: Bool = true) -> String {
        guard rewrite.mode == "patch" else { return "" }
        var clauses: [String] = []
        // Only when the rule forces one: a patch that names no status keeps the real response's, and
        // a number there would be a claim about a response this engine never saw.
        if let status = rewrite.status { clauses.append("sets status to \(status)") }
        if let strategy = rewrite.patchStrategy {
            // The one strategy the engine has, in words; anything a newer engine sends is repeated
            // rather than translated into it.
            clauses.append(strategy == "appendToArray" ? "appends to arrays" : strategy)
        }
        if includeApplicability { clauses.append("JSON responses only") }
        return clauses.joined(separator: " · ")
    }

    /// `JSON · 251 B`, `Text · 12 B`, `No body` — the quietest line, and the last.
    /// See `ScenarioOutlineTests`.
    static func metaLine(_ rewrite: Rewrite) -> String? {
        guard rewrite.mode != "patch" else { return nil }
        return metaLine(kind: rewrite.bodyKind, bytes: rewrite.bodyBytes)
    }

    static func metaLine(kind: String, bytes: Int?) -> String {
        guard kind != "none" else { return "No body" }
        let name = kind == "json" ? "JSON" : kind.prefix(1).uppercased() + kind.dropFirst()
        return bytes.map { "\(name) · \(byteSize($0))" } ?? name
    }

    // MARK: - The outline

    /// Keep unrelated sequences separate and inactive rules visible after active ones.
    static func outline(_ snapshot: RulesSnapshot) -> ScenarioOutline {
        let groups = grouped(snapshot.rules)
        let ordered = groups.active + groups.inactive
        let sequences = ordered.compactMap { rule in
            rule.rewrite.sequence.map { sequenceBlock($0, rule: rule, in: ordered) }
        }
        return ScenarioOutline(
            sequences: sequences,
            otherRules: ordered.filter { $0.rewrite.sequence == nil }.map(summary))
    }

    private static func summary(_ rule: RuleRow) -> ScenarioOutline.RuleSummary {
        ScenarioOutline.RuleSummary(
            id: rule.id, request: requestLine(rule.match), conditions: conditionLines(rule.match),
            behaviour: behaviourLine(rule.rewrite), inactive: !rule.isActive,
            status: rule.rewrite.mode == "replace" ? rule.rewrite.status : nil,
            delay: delayLabel(rule.rewrite))
    }

    private static func sequenceBlock(
        _ sequence: RewriteSequence, rule: RuleRow, in rules: [RuleRow]
    ) -> ScenarioOutline.Sequence {
        let advance = transition(sequence, in: rules)
        let states = sequence.steps.enumerated().map { offset, step in
            ScenarioOutline.State(
                number: offset + 1, transition: advance, status: step.status)
        }
        return ScenarioOutline.Sequence(
            id: rule.id, request: requestLine(rule.match), conditions: conditionLines(rule.match),
            advanceNote: sequence.advanceOn == nil ? "Advances after each answer" : nil,
            inactive: !rule.isActive, states: states, footer: exhaustionFooter(sequence),
            delay: delayLabel(rule.rewrite))
    }

    private static func transition(
        _ sequence: RewriteSequence, in rules: [RuleRow]
    ) -> ScenarioOutline.Transition? {
        guard let matcher = sequence.advanceOn else { return nil }
        return ScenarioOutline.Transition(
            request: requestLine(matcher), conditions: conditionLines(matcher),
            relatedResponses: relatedResponses(matcher, in: rules))
    }

    /// Group by explicit method and path only; show every candidate's conditions without predicting
    /// a winner. See `ScenarioOutlineTests`.
    private static func relatedResponses(_ matcher: RuleMatch, in rules: [RuleRow]) -> [ScenarioOutline
        .RuleSummary]
    {
        guard let method = matcher.method, let path = matcher.path else { return [] }
        return rules.filter {
            $0.rewrite.sequence == nil && $0.match?.method?.uppercased() == method.uppercased()
                && $0.match?.path == path
        }.sorted { $0.id < $1.id }.map(summary)
    }

    /// What happens to requests that arrive after the last state. Nil when the engine sent no
    /// policy: it always sends the one that will actually apply, so a missing one is a snapshot this
    /// app does not understand, and naming a behaviour would promise what the proxy has not agreed
    /// to. See `ScenarioOutlineTests`.
    static func exhaustionFooter(_ sequence: RewriteSequence) -> String? {
        guard let policy = sequence.onExhausted else { return nil }
        switch policy {
        // The proxy answers 500 when a sequence under `error` is asked for another response, which
        // is what the reader will see — the word `error` is the setting, not the outcome.
        case "error": return "Further requests return 500"
        case "repeatLast": return "Further requests repeat step \(sequence.steps.count)"
        case "passThrough": return "Further requests pass through"
        // A newer engine's word is a fact this app does not know, not one it may rename.
        default: return "Further requests: \(policy)"
        }
    }

    // MARK: - Where a click lands

    /// Carry the browsed scenario with a destination so navigation cannot select its active namesake.
    struct Destination: Equatable {
        var scenario: String
        var selection: ListSelection
        /// The step to open the rule at. Nil for a row that names a rule and not one of its steps.
        var step: Int?
    }

    static func destination(rule id: String, step: Int? = nil, drawnFrom scenario: String) -> Destination {
        Destination(scenario: scenario, selection: .rule(id), step: step)
    }

    /// Which step of which rule the reader is looking at.
    /// See `ScenarioOutlineTests`.
    struct StepPick: Equatable {
        var scenario: String
        var rule: String
        var step: Int
    }

    /// The step whose response the detail shows: the pick when it is this scenario's and this
    /// rule's and this rule has such a step, and the first otherwise. Nil for a rule with no steps.
    static func shownStep(_ rule: RuleRow, in scenario: String?, pick: StepPick?) -> Int? {
        guard let sequence = rule.rewrite.sequence, !sequence.steps.isEmpty else { return nil }
        guard let pick, pick.scenario == scenario, pick.rule == rule.id,
            sequence.steps.indices.contains(pick.step - 1)
        else { return 1 }
        return pick.step
    }

    // MARK: - The detail's step selector

    /// `Step 2 of 9`, the label above a sequence rule's response.
    static func stepSelectorLabel(step: Int, of total: Int) -> String {
        "Step \(step) of \(total)"
    }

    /// `Step 3 · 200`, for the menu a sequence too long to segment uses.
    static func stepMenuLabel(number: Int, step: StepSummary) -> String {
        let label = "Step \(number)"
        return step.status.map { "\(label) · \($0)" } ?? label
    }
}
