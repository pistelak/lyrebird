import SwiftUI

/// What the active scenario rewrites, and how. Read-only apart from "Reset run": every line comes
/// from `GET /__mock__/rules`, whose `rewrite` is the engine's own description of what a rule
/// answers with, so this window can show "json 1.2 KB" or "next step 2" without a second reading of
/// step inheritance, the default status or the wire encoding of a body.
struct RulesWindowView: View {
    /// One spelling of the scene id, so the `Window` that declares it and the `openWindow` that
    /// asks for it cannot drift apart into a button that opens nothing.
    static let sceneId = "rules"

    let model: AppModel
    @State private var selection: RuleRow.ID?

    private var snapshot: RulesSnapshot? {
        if case .ok(let snapshot) = model.rulesRead { return snapshot }
        return nil
    }

    private var selectedRule: RuleRow? {
        snapshot?.rules.first { $0.id == selection }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            // Above whichever of the two follows, never inside one of them: a scenario that lost
            // every rule shows the empty state, and that is exactly when the problems must be read.
            let problems = RuleFormatting.problems(in: model.rulesRead)
            if !problems.isEmpty { NotWholeBanner(problems: problems) }
            if let vacancy = RuleFormatting.vacancy(
                status: model.status, read: model.rulesRead, controlPort: Config.controlURL.port)
            {
                VacancyView(vacancy: vacancy)
            } else if let snapshot {
                rules(snapshot)
            }
        }
        .frame(minWidth: 700, minHeight: 400)
        .task { await model.rulesWindowAppeared() }
        // The poll skips the rules read while this is false, so a window left shut costs nothing.
        .onDisappear { model.rulesWindowOpen = false }
    }

    // MARK: - Header strip

    private var header: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(model.status.dotColor ?? Color.secondary)
                .frame(width: 9, height: 9)
            // `ownHealth`, not `health`: a proxy running another profile answers these reads too,
            // and its scenario, port and PAC state used to be printed here directly above the line
            // saying that proxy is not this one's.
            if let health = model.ownHealth {
                Text(snapshot?.scenario ?? health.activeScenario ?? "—").font(.headline)
                if let port = health.proxyPort {
                    Text("proxy :" + String(port)).font(.callout).foregroundStyle(.secondary)
                }
                Text(health.intercepting == true ? "intercepting" : "not intercepting")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                Text("Rules").font(.headline)
            }
            if let snapshot {
                // String(_:) rather than an interpolated literal: `Text` localises an interpolated
                // Int and would put a grouping separator in a port or a count.
                Text(String(snapshot.rules.count) + (snapshot.rules.count == 1 ? " rule" : " rules"))
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let error = model.lastError, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(.red).lineLimit(2)
            }
            Button("Reset run") { Task { await model.resetRun() } }
                .disabled(model.busy || snapshot == nil)
                .help("Rewind every sequence cursor and clear every answer count")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    // MARK: - The two panes

    private func rules(_ snapshot: RulesSnapshot) -> some View {
        NavigationSplitView {
            List(snapshot.rules, selection: $selection) { rule in
                RuleRowView(rule: rule)
            }
            .navigationSplitViewColumnWidth(min: 320, ideal: 400)
        } detail: {
            if let rule = selectedRule {
                // Keyed to the rule, so the step a previous selection was looking at does not carry
                // over into a rule whose steps are different ones.
                RuleDetailView(rule: rule).id(rule.id)
            } else {
                VacancyView(
                    vacancy: RuleFormatting.Vacancy(
                        message: "No rule selected.", hint: "Pick one on the left to see what it answers with."))
            }
        }
        .navigationSplitViewStyle(.balanced)
    }
}

/// One centred bold line and a hint. Never a blank table: each of these says a different thing
/// about why there is nothing to show.
private struct VacancyView: View {
    let vacancy: RuleFormatting.Vacancy

    var body: some View {
        VStack(spacing: 6) {
            Text(vacancy.message).font(.headline)
            Text(vacancy.hint).font(.callout).foregroundStyle(.secondary)
        }
        .multilineTextAlignment(.center)
        .padding(30)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The scenario loaded with rules missing. Shown above the table rather than beside a rule, because
/// what it names is not there: a dropped rule is invisible in a list of the ones that survived.
private struct NotWholeBanner: View {
    let problems: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("This scenario did not load whole.").font(.callout.bold())
            ForEach(problems, id: \.self) { problem in
                Text(problem).font(.caption).fixedSize(horizontal: false, vertical: true)
            }
        }
        .foregroundStyle(.red)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color.red.opacity(0.1))
    }
}

/// A rule in the list: what it matches, what it answers with, and what it has done.
private struct RuleRowView: View {
    let rule: RuleRow

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: rule.isActive ? "largecircle.fill.circle" : "circle")
                .foregroundStyle(rule.isActive ? Color.accentColor : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(RuleFormatting.matchLine(rule.match))
                    .font(.system(.body, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(RuleFormatting.howLine(rule.rewrite, state: rule.sequenceState))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(RuleFormatting.answerCaption(rule.answer))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 3)
        .opacity(rule.isActive ? 1 : 0.55)
    }
}

/// The right-hand pane: everything the snapshot says about the selected rule.
private struct RuleDetailView: View {
    let rule: RuleRow
    /// Which step's stored JSON is printed. Nil until the pane picks the one the cursor is on.
    @State private var selectedStep: Int?

    private var storedSteps: [JSONValue] { rule.sequence?.steps ?? [] }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                matches
                if let sequence = rule.rewrite.sequence {
                    self.sequence(sequence)
                } else {
                    answersWith
                }
                if let notes = rule.notes, !notes.isEmpty {
                    section("NOTES") {
                        Text(notes).font(.callout).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
        }
    }

    // MARK: - Matches

    private var matches: some View {
        section("MATCHES") {
            Grid(alignment: .topLeading, horizontalSpacing: 12, verticalSpacing: 4) {
                field("method", rule.match?.method?.uppercased() ?? "ANY")
                field("path", rule.match?.path ?? "*")
                ForEach((rule.match?.query ?? [:]).keys.sorted(), id: \.self) { key in
                    field("query.\(key)", scalar(rule.match?.query?[key]))
                }
                if let bodyContains = rule.match?.bodyContains, !bodyContains.isEmpty {
                    field("bodyContains", bodyContains)
                }
            }
        }
    }

    // MARK: - Answers with

    private var answersWith: some View {
        section("ANSWERS WITH") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    tag(rule.rewrite.mode ?? "replace")
                    if let status = rule.rewrite.status {
                        Text(String(status))
                            .font(.system(.body, design: .monospaced).bold())
                            .foregroundStyle(RuleFormatting.statusColor(status))
                    }
                    if let delay = rule.rewrite.delayMs, delay > 0 { tag("+" + String(delay) + " ms") }
                    if let keys = rule.rewrite.patchKeys {
                        tag(String(keys) + (keys == 1 ? " patched key" : " patched keys"))
                    }
                    if let strategy = rule.rewrite.patchStrategy { tag(strategy) }
                }
                Text(bodyLine).font(.callout).foregroundStyle(.secondary)
                if let headers = rule.headers, !headers.isEmpty {
                    monospacedBlock(RuleFormatting.headerBlock(headers), caption: "headers")
                }
                if let patch = rule.patch {
                    monospacedBlock(RuleFormatting.prettyJSON(patch), caption: "patch")
                } else if let body = rule.body {
                    monospacedBlock(RuleFormatting.prettyJSON(body), caption: "body")
                }
            }
        }
    }

    private var bodyLine: String {
        // "no body" spelled out: an absent line here would read as a body the pane failed to show.
        if rule.rewrite.bodyKind == nil || rule.rewrite.bodyKind == "none" {
            return rule.rewrite.mode == "patch" ? "merged into the real response" : "no body"
        }
        return "body: " + RuleFormatting.bodySummary(kind: rule.rewrite.bodyKind, bytes: rule.rewrite.bodyBytes)
    }

    // MARK: - Sequence

    private func sequence(_ sequence: RewriteSequence) -> some View {
        section("SEQUENCE") {
            VStack(alignment: .leading, spacing: 8) {
                Text(
                    sequence.advanceOn == "match"
                        ? "advances on its own matcher" : "advances when this rule answers"
                )
                .font(.callout)
                Text("exhausted → \(sequence.onExhausted ?? "error")").font(.callout)
                Text("run " + (rule.sequenceState?.runId ?? "none"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ForEach(Array(sequence.steps.enumerated()), id: \.offset) { index, step in
                    stepRow(number: index + 1, step: step)
                }
                if let step = shownStep, step >= 1, step <= storedSteps.count {
                    monospacedBlock(
                        RuleFormatting.prettyJSON(storedSteps[step - 1]),
                        caption: "step " + String(step) + " as stored")
                }
            }
        }
    }

    /// The step whose stored JSON is printed: the one clicked, else the one the cursor is on, else
    /// the first. A sequence that has run out has no next step, so the fallback matters.
    private var shownStep: Int? {
        selectedStep ?? rule.sequenceState?.nextStep ?? (storedSteps.isEmpty ? nil : 1)
    }

    private func stepRow(number: Int, step: StepSummary) -> some View {
        let isNext = rule.sequenceState?.nextStep == number
        return Button {
            selectedStep = number
        } label: {
            HStack(spacing: 8) {
                Text(String(number)).font(.caption.monospaced()).frame(width: 18, alignment: .trailing)
                if let status = step.status {
                    Text(String(status))
                        .font(.system(.callout, design: .monospaced))
                        .foregroundStyle(RuleFormatting.statusColor(status))
                }
                Text(RuleFormatting.bodySummary(kind: step.bodyKind, bytes: step.bodyBytes))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let count = step.headerCount, count > 0 {
                    Text(String(count) + (count == 1 ? " header" : " headers"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let served = rule.sequenceState?.serves?[String(number)] {
                    Text("served " + String(served) + "×").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if isNext { tag("next") }
            }
            .contentShape(Rectangle())
            .padding(.vertical, 2)
            .padding(.horizontal, 4)
            .background(shownStep == number ? Color.accentColor.opacity(0.12) : Color.clear)
        }
        .buttonStyle(.plain)
    }

    // MARK: - Building blocks

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func field(_ name: String, _ value: String) -> some View {
        GridRow {
            Text(name).font(.caption).foregroundStyle(.secondary).gridColumnAlignment(.trailing)
            Text(value).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
        }
    }

    private func tag(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Color.secondary.opacity(0.15), in: Capsule())
    }

    private func monospacedBlock(_ text: String, caption: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(caption).font(.caption2).foregroundStyle(.secondary)
            Text(text)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 4))
        }
    }

    /// A query pin's value as written. Numbers are compared as `str(value)` by the engine, so `2`
    /// and `"2"` pin the same request and are shown the same way.
    private func scalar(_ value: JSONValue?) -> String {
        guard let value else { return "" }
        if case .string(let text) = value { return text }
        return RuleFormatting.prettyJSON(value)
    }
}
