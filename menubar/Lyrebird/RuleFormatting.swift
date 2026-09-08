import SwiftUI

/// How a rule reads on screen. Pure functions over the decoded models and nothing else, so every
/// line the window shows can be checked without standing a view up — see `RuleFormattingTests`.
enum RuleFormatting {

    static func scenarioNotes(_ name: String, in scenarios: ScenarioList?) -> String? {
        let notes = scenarios?.scenarios.first { $0.name == name }?.notes?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return notes?.isEmpty == false ? notes : nil
    }

    enum ResponseKind: Equatable {
        case replace
        case patch
        case sequence
        case unknown(String)

        var title: String {
            switch self {
            case .replace: return "Replace"
            case .patch: return "Patch"
            case .sequence: return "Sequence"
            case .unknown(let mode): return mode.isEmpty ? "Unknown mode" : mode
            }
        }

        var explanation: String {
            switch self {
            case .replace: return "Returns the configured response instead of calling the server."
            case .patch: return "Merges changes into the server’s JSON response."
            case .sequence: return "Returns configured responses as the sequence advances."
            case .unknown: return "This response mode is not recognized by this app."
            }
        }
    }

    static func responseKind(_ rewrite: Rewrite) -> ResponseKind {
        switch rewrite.mode {
        case "patch": return .patch
        case "replace": return rewrite.sequence == nil ? .replace : .sequence
        default: return .unknown(rewrite.mode ?? "")
        }
    }

    // MARK: - The two lines of a rule row

    /// `ANY` rather than a blank, because a rule with no method matches every one of them, and a gap
    /// there reads as a missing value instead of as the constraint it is not.
    static func method(of match: RuleMatch?) -> String { match?.method?.uppercased() ?? "ANY" }

    /// `*` for the same reason: a rule with no path answers every intercepted request.
    static func path(of match: RuleMatch?) -> String { match?.path ?? "*" }

    /// The toolbar's status item: `intercepting · orders-outage`.
    /// See testTheToolbarNamesTheActiveScenarioAndNotTheBrowsedOne.
    static func statusItem(status: AppModel.Status, activeScenario: String?) -> String {
        ([status.word] + (activeScenario.map { [$0] } ?? [])).joined(separator: " · ")
    }

    /// What to draw where a step's body goes.
    enum StepBody: Equatable {
        case none
        /// The line to show instead of the block, already worded with the size.
        case omitted(String)
        case json(JSONValue)
    }

    static func stepBody(_ step: StepSummary) -> StepBody {
        if step.bodyOmitted == true { return .omitted(omittedBodyLine(bytes: step.bodyBytes)) }
        guard let body = step.body else { return .none }
        return .json(body)
    }

    /// `body of 1.2 MB not included in the snapshot` — the line that replaces a step's body block
    /// when the engine left it out. A size and no body is not "no body": the step answers with one,
    /// and a body block reading empty there would describe a response nobody receives.
    static func omittedBodyLine(bytes: Int?) -> String {
        "body of " + (bytes.map(byteSize) ?? "unknown size") + " not included in the snapshot"
    }

    /// `inherited from the rule`, beside a block whose value the step did not write. Nil when the
    /// step wrote it, so the caption appears only where it changes what an edit would have to touch.
    static func inheritedCaption(_ step: StepSummary, field: String) -> String? {
        (step.inherited ?? []).contains(field) ? "inherited from the rule" : nil
    }

    /// A labelled fact in the detail pane.
    struct Fact: Equatable, Identifiable {
        var label: String
        var value: String
        var id: String { label }

        init(_ label: String, _ value: String) {
            self.label = label
            self.value = value
        }
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
        /// Between a label and what it labels.
        static let step: CGFloat = 12
        /// Between sections that are about different things.
        static let section: CGFloat = 16
    }

    // MARK: - Numbers and colours

    /// `512 B`, `1.2 KB`, `3.0 MB`. Binary units, matching what the engine counts: `bodyBytes` is
    /// the length of the encoded body in bytes, not a file size a Finder would round differently.
    static func byteSize(_ bytes: Int) -> String {
        let kilobyte = 1024.0
        let value = Double(bytes)
        if value < kilobyte { return "\(bytes) B" }
        if value < kilobyte * kilobyte { return String(format: "%.1f KB", value / kilobyte) }
        return String(format: "%.1f MB", value / (kilobyte * kilobyte))
    }

    // AppKit's system colours rather than SwiftUI's `.red` / `.green` / `.orange`: those are fixed
    // sRGB values that look the same in both appearances, and the red one sits at 3.0:1 on a dark
    // pane. The system ones are resolved against the appearance the view is drawn in, so they also
    // follow Increase Contrast — see testEveryTintTheWindowUsesIsOneTheAppearanceResolves.
    static let danger = Color(nsColor: .systemRed)
    static let success = Color(nsColor: .systemGreen)
    static let warning = Color(nsColor: .systemOrange)

    /// Green below 400, red at or above — the menu's reading of what happened to a request. The
    /// configured rules use neutral status badges; Recent colours observed outcomes.
    /// See testARequestThatNeverGotAResponseIsNotAGreenZero.
    static func statusColor(_ status: Int) -> Color {
        guard status > 0 else { return .secondary }
        return status < 400 ? success : danger
    }

    /// The status as the menu prints it. `0` is the engine's "no status", which every one of the
    /// three digits it looks like would be a lie about.
    static func statusText(_ status: Int) -> String { status > 0 ? String(status) : "no response" }

    /// A query pin's value as written. The engine compares `str(value)`, so `2` and `"2"` pin the
    /// same request — and the bare text is what the rule's author typed, without the quotes a JSON
    /// print would wrap a string in.
    static func scalarText(_ value: JSONValue?) -> String {
        guard let value else { return "" }
        if case .string(let text) = value { return text }
        return jsonText(value)
    }

    // MARK: - Searching and grouping

    /// Substring over the fields someone would search by: the id they wrote in a test, the path they
    /// are debugging, the method, and the notes they left themselves.
    /// See testSearchIgnoresAccentsTheReaderDidNotType.
    static func matches(_ rule: RuleRow, query: String) -> Bool {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return true }
        let haystack = [rule.id, rule.match?.path, rule.match?.method, rule.notes]
            .compactMap { $0 }
            .joined(separator: "\n")
        return haystack.range(of: needle, options: [.caseInsensitive, .diacriticInsensitive]) != nil
    }

    /// The rules a query leaves, in the order the snapshot listed them — which is the order the
    /// proxy holds them in, and the one an operator looking for a rule by position needs.
    static func filter(_ rows: [RuleRow], query: String) -> [RuleRow] {
        rows.filter { matches($0, query: query) }
    }

    /// Split for the list: the rules that can answer, and the ones switched off. Inactive rules are
    /// still listed — a rule you cannot find is a rule you will write a second time — but they go
    /// below, under a header of their own, because they cannot explain anything the proxy just did.
    static func grouped(_ rows: [RuleRow]) -> (active: [RuleRow], inactive: [RuleRow]) {
        (rows.filter(\.isActive), rows.filter { !$0.isActive })
    }

    /// Resolve against the full snapshot so filtering never changes the response being read.
    static func detailRule(selection: ListSelection?, in snapshot: RulesSnapshot?) -> RuleRow? {
        guard let snapshot else { return nil }
        let id: String?
        if case .rule(let rule) = selection { id = rule } else { id = flowRow(selection, in: snapshot)?.ruleId }
        return snapshot.rules.first { $0.id == id }
    }

    enum ListSelection: Hashable {
        case rule(String)
        case flow(String, String)
    }

    /// Preserve an existing row; otherwise open the first configured request.
    static func selection(current: ListSelection?, in snapshot: RulesSnapshot?) -> ListSelection? {
        guard let snapshot else { return current }
        let rows = flowSections(snapshot).flatMap(\.rows)
        if let current, rows.contains(where: { $0.selection == current }) { return current }
        return rows.first?.selection
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
    /// See testThePrintedJsonParsesBackToTheValueItCameFrom.
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

    /// One header per row, sorted, for the detail pane.
    static func headerRows(_ headers: [String: String]?) -> [Fact] {
        (headers ?? [:]).sorted { $0.key < $1.key }.map { Fact($0.key, $0.value) }
    }

    // MARK: - Nothing to show, and why

    /// The load problems recorded against the scenario on screen, whatever else the window is
    /// showing.
    /// See testAScenarioWhoseRulesWereAllDroppedStillNamesWhatWentWrong.
    static func problems(in read: MockClient.RulesRead?) -> [String] {
        if case .ok(let snapshot) = read { return snapshot.notWhole }
        return []
    }

    /// One line and a hint, shown where a list would be. Never a blank list: "the proxy is not
    /// running", "another profile holds the port" and "this scenario has no rules" are three
    /// different facts, and an empty list states none of them — the same failure the menu's
    /// `scenariosPlaceholder` exists to avoid.
    struct Vacancy: Equatable {
        var message: String
        var hint: String
    }

    /// Why there is no proxy of ours to read, or nil when there is one.
    /// See testAForeignProxysRefusalIsNeverReportedAsAnEngineTooOld.
    static func proxyVacancy(status: AppModel.Status, controlPort: Int?) -> Vacancy? {
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
            return nil
        }
    }

    /// Why there is no rules list, or nil when there is a snapshot with rules in it.
    static func rulesVacancy(
        status: AppModel.Status, read: MockClient.RulesRead?, controlPort: Int?
    ) -> Vacancy? {
        if let vacancy = proxyVacancy(status: status, controlPort: controlPort) { return vacancy }
        switch read {
        case .ok(let snapshot):
            guard snapshot.rules.isEmpty else { return nil }
            return Vacancy(
                message: "\(snapshot.scenario) has no rules yet.",
                hint: "Add one with lyrebird override add, or edit the scenario file.")
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

    /// An empty snapshot retains the scenario's list and shows its vacancy within it.
    enum RulesColumn {
        case vacancy(Vacancy)
        case list(RulesSnapshot, note: Vacancy?)
    }

    static func rulesColumn(
        status: AppModel.Status, read: MockClient.RulesRead?, controlPort: Int?
    ) -> RulesColumn {
        if let vacancy = proxyVacancy(status: status, controlPort: controlPort) { return .vacancy(vacancy) }
        guard case .ok(let snapshot) = read else {
            return .vacancy(rulesVacancy(status: status, read: read, controlPort: controlPort)!)
        }
        return .list(snapshot, note: rulesVacancy(status: status, read: read, controlPort: controlPort))
    }

    /// What a failed action left behind, or nil when there is nothing to say.
    /// See testAWriteTheProxyRefusedLeavesSomethingTheWindowCanShow.
    static func actionFailure(_ lastError: String?) -> String? {
        let message = (lastError ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return message.isEmpty ? nil : message
    }
}
