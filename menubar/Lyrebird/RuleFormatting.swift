import SwiftUI

/// How a rule reads on screen. Pure functions over the decoded models and nothing else, so the
/// lines the Rules window shows can be checked without standing a view up — see `RuleFormattingTests`.
///
/// Nothing here decides anything about a rule. Every fact these strings are built from arrives in
/// `rewrite`, which is the engine's own description of what the rule answers with; re-deriving any
/// of it from the stored fields beside it would be a second implementation of rules the engine owns.
enum RuleFormatting {

    // MARK: - The two lines of a row

    /// `ANY` rather than a blank, because a rule with no method matches every one of them, and a gap
    /// there reads as a missing value instead of as the constraint it is not.
    static func method(of match: RuleMatch?) -> String { match?.method?.uppercased() ?? "ANY" }

    /// `*` for the same reason: a rule with no path answers every intercepted request.
    static func path(of match: RuleMatch?) -> String { match?.path ?? "*" }

    /// What the rule answers with, in one line: `replace → 200 json 1.2 KB · +1000 ms`,
    /// `patch → merge 3 keys, force 503 · JSON upstream only`,
    /// `sequence 5 steps · next 2 · then repeatLast`.
    ///
    /// `state` supplies the cursor only. It is passed separately because a sequenced rule's *shape*
    /// is in `rewrite` and its *position* is runtime state, and the two are read from different
    /// slots of the snapshot.
    /// `includingStatus: false` leaves the status out, for the list row that shows it in a column of
    /// its own — the same number twice on one line reads as two different facts.
    static func howLine(
        _ rewrite: Rewrite, state: SequenceState? = nil, includingStatus: Bool = true
    ) -> String {
        var parts: [String] = []
        if let sequence = rewrite.sequence {
            parts.append("sequence \(sequence.steps.count) \(sequence.steps.count == 1 ? "step" : "steps")")
            // Nil `nextStep` is the exhausted sequence, not a missing reading: `sequence_states`
            // sends the number only while a planned step remains.
            parts.append(state?.nextStep.map { "next \($0)" } ?? "exhausted")
            if let policy = sequence.onExhausted { parts.append("then \(policy)") }
        } else if rewrite.mode == "patch" {
            var phrase = "patch → merge \(rewrite.patchKeys ?? 0) \(rewrite.patchKeys == 1 ? "key" : "keys")"
            if let strategy = rewrite.patchStrategy { phrase += ", \(strategy)" }
            // Only when the rule forces one: a patch that names no status keeps the real response's,
            // and printing a number there would be a claim about a response this engine never saw.
            if includingStatus, let status = rewrite.status { phrase += ", force \(status)" }
            parts.append(phrase)
            parts.append("JSON upstream only")
        } else {
            var tail = ""
            if includingStatus, let status = rewrite.status { tail += " \(status)" }
            tail += bodyPhrase(kind: rewrite.bodyKind, bytes: rewrite.bodyBytes)
            // No `?? "replace"`, for the reason the mode chip has none: the engine sends a mode for
            // every validated rule, so a missing one is a snapshot this app does not understand, and
            // the word is dropped rather than guessed — see testAHowLineDoesNotInventAModeEither.
            //
            // And no arrow with nothing after it: a bodyless 204 in a list that shows the status in
            // its own column left every such row reading "replace →", a sentence cut off mid-way.
            // See testAModeWithNothingAfterItDropsTheArrow.
            parts.append(tail.isEmpty ? (rewrite.mode ?? "") : (rewrite.mode.map { "\($0) →" } ?? "→") + tail)
        }
        if let delay = rewrite.delayMs, delay > 0 { parts.append("+\(delay) ms") }
        return parts.joined(separator: " · ")
    }

    /// ` json 1.2 KB`, or nothing at all when the rule answers with no body — a 204 and a rule that
    /// simply carries none both report `"none"`, and "none 0 B" would describe a payload.
    static func bodyPhrase(kind: String?, bytes: Int?) -> String {
        guard let kind, kind != "none" else { return "" }
        guard let bytes else { return " \(kind)" }
        return " \(kind) \(byteSize(bytes))"
    }

    /// `json 1.2 KB`, or `no body` — the same phrase with the absence spelled out, for the places
    /// that show it on a line of its own where a blank would read as a value that failed to render.
    static func bodySummary(kind: String?, bytes: Int?) -> String {
        let phrase = bodyPhrase(kind: kind, bytes: bytes).trimmingCharacters(in: .whitespaces)
        return phrase.isEmpty ? "no body" : phrase
    }

    /// What a rule has done, for the list: `3 answers`, `1 answer`, `no answers yet`, `inactive`.
    ///
    /// One argument, because activeness and the count come from the same row: passing them
    /// separately invited a caller to show one rule's state beside another's evidence. The run id is
    /// deliberately not here — thirty rows each ending in the same opaque token is noise, and the
    /// one place it answers a question is the detail pane, where it appears verbatim and labelled.
    static func answerCaption(_ answer: AnswerState) -> String {
        guard answer.active else { return "inactive" }
        switch answer.count {
        case 0: return "no answers yet"
        case 1: return "1 answer"
        default: return "\(answer.count) answers"
        }
    }

    /// The run a count belongs to, spelled out for the detail pane. `no run` is a rule that has none
    /// — never reset, never near a request — which is a different fact from a count of zero.
    static func runCaption(_ answer: AnswerState) -> String {
        answer.runId.map { "run \($0)" } ?? "no run"
    }

    /// One neutral badge for every method, and a warning tint for the one that destroys something.
    /// Colouring all of them would spend the reader's attention on a field they can already read.
    static func methodTint(_ method: String) -> Color? {
        method.uppercased() == "DELETE" ? .orange : nil
    }

    // MARK: - Spacing
    //
    // Four steps, used everywhere, so that "these two things belong together" is said by distance
    // rather than by a number somebody picked at the moment they wrote the view.

    enum Space {
        /// Between the lines of one thing.
        static let tight: CGFloat = 4
        /// Between neighbouring things in a row.
        static let snug: CGFloat = 8
        /// Between a label and what it labels, and around the edges of a strip.
        static let step: CGFloat = 12
        /// Between sections that are about different things.
        static let section: CGFloat = 16
    }

    // MARK: - Numbers

    /// `512 B`, `1.2 KB`, `3.0 MB`. Binary units, matching what the engine counts: `bodyBytes` is
    /// the length of the encoded body in bytes, not a file size a Finder would round differently.
    static func byteSize(_ bytes: Int) -> String {
        let kilobyte = 1024.0
        let value = Double(bytes)
        if value < kilobyte { return "\(bytes) B" }
        if value < kilobyte * kilobyte { return String(format: "%.1f KB", value / kilobyte) }
        return String(format: "%.1f MB", value / (kilobyte * kilobyte))
    }

    /// Green below 400, red at or above — the same reading the menu's recent-traffic list uses, so
    /// one colour never means two things across the two windows.
    static func statusColor(_ status: Int) -> Color { status < 400 ? .green : .red }

    // MARK: - Chips

    /// One pill in the detail pane. `tint` is nil for the ordinary chip; a status chip carries its
    /// own, so the number and the colour beside it can never come from different readings.
    struct Chip: Equatable {
        var text: String
        var tint: Color?

        init(_ text: String, tint: Color? = nil) {
            self.text = text
            self.tint = tint
        }
    }

    /// What the matcher pins beyond its method and path. Empty when it pins nothing, so the row
    /// disappears instead of sitting there as an empty strip.
    static func matchChips(for match: RuleMatch?) -> [Chip] {
        var chips = (match?.query ?? [:]).keys.sorted().map { key in
            Chip("\(key) = \(scalarText(match?.query?[key]))")
        }
        if let contains = match?.bodyContains, !contains.isEmpty {
            chips.append(Chip("body ∋ \"\(truncated(contains))\""))
        }
        return chips
    }

    /// What the rule answers with, as one row. Every value comes from `rewrite` — the engine's own
    /// description — so this decides nothing; it only chooses what is worth a pill.
    static func answerChips(for rewrite: Rewrite) -> [Chip] {
        // No `?? "replace"`: the engine sends a mode for every validated rule, so a missing one is a
        // snapshot this app does not understand — and a chip reading "replace" over a rule that
        // might be a patch is worse than no chip. See testAModeTheEngineDidNotSendIsNotInvented.
        var chips = rewrite.mode.map { [Chip($0)] } ?? []
        if let status = rewrite.status { chips.append(Chip(String(status), tint: statusColor(status))) }
        if let delay = rewrite.delayMs, delay > 0 { chips.append(Chip("+\(delay) ms")) }
        if let kind = rewrite.bodyKind, kind != "none" {
            chips.append(Chip(rewrite.bodyBytes.map { "\(kind) · \(byteSize($0))" } ?? kind))
        }
        if let keys = rewrite.patchKeys { chips.append(Chip("\(keys) \(keys == 1 ? "key" : "keys")")) }
        if let strategy = rewrite.patchStrategy { chips.append(Chip(strategy)) }
        return chips
    }

    /// Middle-truncated: a `bodyContains` is usually distinctive at both ends, and keeping only the
    /// prefix makes two different pins look like the same one.
    static func truncated(_ text: String, to limit: Int = 40) -> String {
        guard text.count > limit else { return text }
        let head = (limit - 1) / 2
        return String(text.prefix(head)) + "…" + String(text.suffix(limit - 1 - head))
    }

    /// A query pin's value as written. The engine compares `str(value)`, so `2` and `"2"` pin the
    /// same request — and the bare text is what the rule's author typed, without the quotes a JSON
    /// print would wrap a string in.
    static func scalarText(_ value: JSONValue?) -> String {
        guard let value else { return "" }
        if case .string(let text) = value { return text }
        return jsonText(value)
    }

    // MARK: - Searching, filtering and grouping

    /// Which rules the segmented control lets through. Client-side over the snapshot already read —
    /// there is no engine call behind any of these, and none of them decides anything about a rule.
    enum Segment: String, CaseIterable, Identifiable {
        case all
        case answered
        case sequences

        var id: String { rawValue }

        var label: String {
            switch self {
            case .all: return "All"
            case .answered: return "Answered this run"
            case .sequences: return "Sequences"
            }
        }

        func admits(_ rule: RuleRow) -> Bool {
            switch self {
            case .all:
                return true
            case .answered:
                // `count` is already scoped to the run the rule is in — `reset` replaces the slot
                // rather than clearing it — so a positive count under a run id is "answered in this
                // run". A nil run id is a rule with no run at all, which has answered nothing.
                return rule.answer.runId != nil && rule.answer.count > 0
            case .sequences:
                return rule.sequenceState != nil
            }
        }
    }

    /// Substring over the fields someone would search by: the id they wrote in a test, the path they
    /// are debugging, the method, and the notes they left themselves.
    ///
    /// Case- *and* diacritic-insensitive, because neither is a distinction the person typing made on
    /// purpose: a note reading "café outage" was not findable by typing `cafe`, which is how someone
    /// concludes the rule is not there. See testSearchIgnoresAccentsTheReaderDidNotType.
    ///
    /// The fields are joined with a newline so a query cannot match across two of them and appear to
    /// have found a rule whose path contains what is really the end of its id.
    static func matches(_ rule: RuleRow, query: String) -> Bool {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return true }
        let haystack = [rule.id, rule.match?.path, rule.match?.method, rule.notes]
            .compactMap { $0 }
            .joined(separator: "\n")
        return haystack.range(of: needle, options: [.caseInsensitive, .diacriticInsensitive]) != nil
    }

    /// The rules a query and a segment leave, in the order the snapshot listed them — which is the
    /// order the proxy holds them in, and the one an operator looking for a rule by position needs.
    static func filter(_ rows: [RuleRow], query: String, segment: Segment) -> [RuleRow] {
        rows.filter { segment.admits($0) && matches($0, query: query) }
    }

    /// Split for the list: the rules that can answer, and the ones switched off. Inactive rules are
    /// still listed — a rule you cannot find is a rule you will write a second time — but they go
    /// below, behind a disclosure, because they cannot explain anything the proxy just did.
    static func grouped(_ rows: [RuleRow]) -> (active: [RuleRow], inactive: [RuleRow]) {
        (rows.filter(\.isActive), rows.filter { !$0.isActive })
    }

    /// `12 rules`, or `3 of 12 rules` once a filter is hiding some. The total is what stops a
    /// filtered list from reading as a scenario that has lost most of its rules.
    static func ruleCount(shown: Int, total: Int) -> String {
        let noun = total == 1 ? "rule" : "rules"
        return shown == total ? "\(total) \(noun)" : "\(shown) of \(total) \(noun)"
    }

    /// The rule the detail pane shows: looked up in *all* of the snapshot's rules, never in the
    /// filtered ones.
    ///
    /// Narrowing a search must not throw away what you were reading, so the lookup deliberately
    /// ignores the filter and `selectionIsHidden` explains the missing row instead. It is a function
    /// rather than a line in the view because reading it from the shown rules would blank the pane
    /// while every filter test still passed — see testTheDetailPaneResolvesASelectionTheFilterHides.
    static func detailRule(selection: String?, in snapshot: RulesSnapshot?) -> RuleRow? {
        guard let selection, let snapshot else { return nil }
        return snapshot.rules.first { $0.id == selection }
    }

    /// Whether the inactive group is open: because the reader opened it, or because a search has
    /// found something inside it.
    ///
    /// Derived on every render rather than set once when a search first matches — typing on past the
    /// match, or clearing the field, has to close it again, and a one-shot handler would leave it
    /// open over a group holding nothing the search found. An empty field is not a search.
    static func inactiveGroupExpanded(userExpanded: Bool, query: String, inactiveMatches: Bool) -> Bool {
        if userExpanded { return true }
        return !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && inactiveMatches
    }

    /// True when the selected rule exists but the filter is hiding it.
    ///
    /// The detail pane deliberately keeps showing it — narrowing a search must not throw away what
    /// you were reading — so the list has to say why the highlighted row is not there, or the
    /// selection looks lost.
    static func selectionIsHidden(_ selection: String?, shown: [RuleRow], all: [RuleRow]) -> Bool {
        guard let selection, all.contains(where: { $0.id == selection }) else { return false }
        return !shown.contains { $0.id == selection }
    }

    // MARK: - Printing stored JSON

    private static let punctuationColor = Color.secondary
    private static let keyColor = Color.primary
    private static let stringColor = Color(nsColor: .systemGreen)
    private static let numberColor = Color(nsColor: .systemBlue)
    private static let literalColor = Color(nsColor: .systemPurple)

    /// A rule's body, patch or step exactly as stored: two-space indent, keys sorted so two reads of
    /// the same rule look the same, one space after a colon and none before, and empty containers on
    /// one line.
    ///
    /// Ours rather than `JSONSerialization`'s because the pane's whole claim is that this is what the
    /// scenario file says, and that wants colour and a shape a person can read — but it must still
    /// parse back to the value it came from, which is what
    /// testThePrintedJsonParsesBackToTheValueItCameFrom pins.
    ///
    /// Its input is only ever a `JSONValue` that came off the wire, which bounds what it can meet:
    /// `JSONValue` tries `Int` before `Double`, so a whole number arrives as `.int` and `.number`
    /// holds only genuine fractions; and JSON has no infinity or NaN literal, so there is no
    /// non-finite double to print as something no parser will read back. See
    /// testWhatTheWireCanCarryIsWhatThePrinterEverSees.
    static func attributedJSON(_ value: JSONValue) -> AttributedString {
        var out = AttributedString()
        append(value, to: &out, indent: 0)
        return out
    }

    /// The same print as plain text, for the Copy button — derived from the one printer rather than
    /// written twice, so what lands on the pasteboard is exactly what the pane shows.
    static func jsonText(_ value: JSONValue) -> String { String(attributedJSON(value).characters) }

    private static func token(_ text: String, _ color: Color) -> AttributedString {
        var piece = AttributedString(text)
        piece.foregroundColor = color
        return piece
    }

    private static func append(_ value: JSONValue, to out: inout AttributedString, indent: Int) {
        let pad = String(repeating: "  ", count: indent)
        let inner = pad + "  "
        switch value {
        case .null:
            out += token("null", literalColor)
        case .bool(let flag):
            out += token(flag ? "true" : "false", literalColor)
        case .int(let number):
            out += token(String(number), numberColor)
        case .number(let number):
            out += token(String(describing: number), numberColor)
        case .string(let text):
            out += token(quoted(text), stringColor)
        case .array(let values):
            guard !values.isEmpty else {
                out += token("[]", punctuationColor)
                return
            }
            out += token("[\n", punctuationColor)
            for (offset, element) in values.enumerated() {
                out += token(inner, punctuationColor)
                append(element, to: &out, indent: indent + 1)
                out += token(offset == values.count - 1 ? "\n" : ",\n", punctuationColor)
            }
            out += token(pad + "]", punctuationColor)
        case .object(let members):
            guard !members.isEmpty else {
                out += token("{}", punctuationColor)
                return
            }
            out += token("{\n", punctuationColor)
            let keys = members.keys.sorted()
            for (offset, key) in keys.enumerated() {
                out += token(inner, punctuationColor)
                out += token(quoted(key), keyColor)
                out += token(": ", punctuationColor)
                append(members[key] ?? .null, to: &out, indent: indent + 1)
                out += token(offset == keys.count - 1 ? "\n" : ",\n", punctuationColor)
            }
            out += token(pad + "}", punctuationColor)
        }
    }

    /// JSON string escaping: the six named escapes, `\u00XX` for any other control character, and
    /// every other scalar literal. A body copied out of this pane has to paste back into the
    /// scenario file it came from — see testThePrintedJsonParsesBackToTheValueItCameFrom.
    private static func quoted(_ text: String) -> String {
        var out = "\""
        for scalar in text.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            case "\u{08}": out += "\\b"
            case "\u{0C}": out += "\\f"
            default:
                if scalar.value < 0x20 {
                    out += String(format: "\\u%04x", scalar.value)
                } else {
                    out.unicodeScalars.append(scalar)
                }
            }
        }
        return out + "\""
    }

    /// `Accept: application/json` per line, sorted, for the monospaced block in the detail pane.
    static func headerBlock(_ headers: [String: String]) -> String {
        headers.sorted { $0.key < $1.key }.map { "\($0.key): \($0.value)" }.joined(separator: "\n")
    }

    // MARK: - Nothing to show, and why

    /// The load problems recorded against this scenario, whatever else the window is showing.
    ///
    /// Read on its own rather than out of the branch that decides the table, because the case that
    /// matters most is the one where there is no table: a scenario whose rules were *all* dropped
    /// has an empty list and a full set of problems, and folding the two together rendered it as
    /// the cheerful "has no rules yet" — which is the load failure hidden behind a tidy sentence.
    /// See testAScenarioWhoseRulesWereAllDroppedStillNamesWhatWentWrong.
    static func problems(in read: MockClient.RulesRead?) -> [String] {
        if case .ok(let snapshot) = read { return snapshot.notWhole }
        return []
    }

    /// One bold line and a hint, shown instead of the table. Never a blank table: "the proxy is not
    /// running", "another profile holds the port" and "this scenario has no rules" are three
    /// different facts, and an empty list states none of them — the same failure the menu's
    /// `scenariosPlaceholder` exists to avoid.
    struct Vacancy: Equatable {
        var message: String
        var hint: String
    }

    /// Nil means there is a snapshot with rules in it, and the table is what to show.
    ///
    /// The proxy states are read from `status` exactly as the menu reads them, and only then is the
    /// rules read consulted: a 404 from a proxy that is not ours would otherwise be reported as an
    /// old engine, when the real answer is that this window is reading somebody else's proxy.
    static func vacancy(
        status: AppModel.Status,
        read: MockClient.RulesRead?,
        controlPort: Int?
    ) -> Vacancy? {
        switch status {
        case .down:
            return Vacancy(message: "Proxy is not running.", hint: "Start it from the menu.")
        case .foreignProfile(let running):
            let port = controlPort.map { "Port \($0)" } ?? "The control port"
            return Vacancy(
                message: "\(port) is held by profile \(running), not this one.",
                hint: "Stop it with lyrebird down, or switch profiles.")
        case .unreadable(let reason):
            return Vacancy(message: "The proxy could not be read.", hint: reason)
        case .profileUnknown(let reason):
            return Vacancy(
                message: "Lyrebird does not know which profile this is.",
                hint: "\(reason) — check the launcher path in Settings.")
        case .intercepting, .pacDisabled:
            break
        }
        switch read {
        case .ok(let snapshot):
            guard snapshot.rules.isEmpty else { return nil }
            return Vacancy(
                message: "\(snapshot.scenario) has no rules yet.",
                hint: "Add one with lyrebird add, or edit the scenario file.")
        case .unsupported:
            return Vacancy(
                message: "This engine predates the rules view.",
                hint: "The control API answered 404 for the rules snapshot. Update Lyrebird's engine.")
        case .unavailable(let reason):
            return Vacancy(message: "The rules could not be read.", hint: reason)
        case nil:
            return Vacancy(message: "Reading the rules…", hint: "The proxy is answering; this window polls it.")
        }
    }
}
