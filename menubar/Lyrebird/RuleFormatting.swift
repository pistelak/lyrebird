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

    /// `GET /api/v1/orders`, for the list. The detail pane sets the two halves in different weights
    /// and so builds them from the pieces above.
    static func matchLine(_ match: RuleMatch?) -> String { "\(method(of: match)) \(path(of: match))" }

    /// What the rule answers with, in one line: `replace → 200 json 1.2 KB · +1000 ms`,
    /// `patch → merge 3 keys, force 503 · JSON upstream only`,
    /// `sequence 5 steps · next 2 · then repeatLast`.
    ///
    /// `state` supplies the cursor only. It is passed separately because a sequenced rule's *shape*
    /// is in `rewrite` and its *position* is runtime state, and the two are read from different
    /// slots of the snapshot.
    static func howLine(_ rewrite: Rewrite, state: SequenceState? = nil) -> String {
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
            if let status = rewrite.status { phrase += ", force \(status)" }
            parts.append(phrase)
            parts.append("JSON upstream only")
        } else {
            var phrase = "\(rewrite.mode ?? "replace") →"
            if let status = rewrite.status { phrase += " \(status)" }
            phrase += bodyPhrase(kind: rewrite.bodyKind, bytes: rewrite.bodyBytes)
            parts.append(phrase)
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

    /// `3 answers · run r7`, or `inactive · no run`. One argument, because activeness and the count
    /// come from the same row: passing them separately invited a caller to show one rule's state
    /// beside another's evidence. `runId` nil is a rule with no run at all, which is a different
    /// fact from a count of zero and must not be shown as one.
    static func answerCaption(_ answer: AnswerState) -> String {
        let served = answer.count == 1 ? "1 answer" : "\(answer.count) answers"
        let run = answer.runId.map { "run \($0)" } ?? "no run"
        return "\(answer.active ? served : "inactive") · \(run)"
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
        var chips = [Chip(rewrite.mode ?? "replace")]
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
            // A `JSONValue` only ever arrives from a JSON decode, and JSON has no infinity or NaN
            // literal, so there is no non-finite double here to print as something JSON cannot read.
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
