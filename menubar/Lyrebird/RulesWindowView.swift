import AppKit
import SwiftUI

/// What the active scenario rewrites, and how. Read-only apart from "Reset run" and the scenario
/// picker: every line comes from `GET /__mock__/rules`, whose `rewrite` is the engine's own
/// description of what a rule answers with, so this window can show "json 1.2 KB" or "next step 2"
/// without a second reading of step inheritance, the default status or the wire encoding of a body.
///
/// Layout only. Everything it decides about which rules to show is a pure function in
/// `RuleFormatting`, so the searching, the segments and the grouping are checked without a view.
struct RulesWindowView: View {
    /// One spelling of the scene id, so the `Window` that declares it and the `openWindow` that
    /// asks for it cannot drift apart into a button that opens nothing.
    static let sceneId = "rules"

    let model: AppModel
    @State private var selection: RuleRow.ID?
    @State private var query = ""
    @State private var segment = RuleFormatting.Segment.all
    @State private var inactiveExpanded = false

    private var snapshot: RulesSnapshot? {
        if case .ok(let snapshot) = model.rulesRead { return snapshot }
        return nil
    }

    private var allRules: [RuleRow] { snapshot?.rules ?? [] }
    private var shownRules: [RuleRow] { RuleFormatting.filter(allRules, query: query, segment: segment) }

    /// Kept even when the filter hides it: narrowing a search must not throw away what you were
    /// reading. The list says so instead — see `selectionIsHidden`.
    private var selectedRule: RuleRow? { RuleFormatting.detailRule(selection: selection, in: snapshot) }

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
            } else if snapshot != nil {
                panes
            }
        }
        .frame(minWidth: 700, minHeight: 400)
        .toolbar { toolbar }
        .task { await model.rulesWindowAppeared() }
        // The poll skips the rules read while this is false, so a window left shut costs nothing.
        .onDisappear { model.rulesWindowOpen = false }
    }

    // MARK: - Header strip

    private var header: some View {
        HStack(spacing: RuleFormatting.Space.step) {
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
            if snapshot != nil {
                Text(RuleFormatting.ruleCount(shown: shownRules.count, total: allRules.count))
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let error = model.lastError, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(.red).lineLimit(2)
            }
        }
        .padding(.horizontal, RuleFormatting.Space.section)
        .padding(.vertical, RuleFormatting.Space.step)
    }

    // MARK: - Toolbar

    /// The scenario this window is describing, as a binding the picker can drive. Writing it goes
    /// through `model.activate`, which is the one gated path — it refuses when the app does not know
    /// which profile it is configured for, and reports the engine's refusal rather than swallowing it.
    private var activeScenario: Binding<String> {
        Binding(
            get: { model.scenarios?.active ?? "" },
            set: { name in
                guard !name.isEmpty, name != model.scenarios?.active else { return }
                Task { await model.activate(name) }
            })
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .navigation) {
            Picker("Scenario", selection: activeScenario) {
                ForEach(model.scenarios?.scenarios ?? []) { scenario in
                    Text(scenario.name).tag(scenario.name)
                }
            }
            .labelsHidden()
            .disabled(model.busy || model.scenarios == nil)
            .help("Switch the active scenario")
        }
        ToolbarItem(placement: .principal) {
            Picker("Filter", selection: $segment) {
                ForEach(RuleFormatting.Segment.allCases) { one in
                    Text(one.label).tag(one)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
        }
        ToolbarItem(placement: .primaryAction) {
            Button {
                Task { await model.resetRun() }
            } label: {
                Label("Reset run", systemImage: "arrow.counterclockwise")
            }
            .disabled(model.busy || snapshot == nil)
            .help("Rewind every sequence cursor and clear every answer count")
        }
    }

    // MARK: - The two panes

    private var panes: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 340, ideal: 420)
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
        .searchable(text: $query, placement: .toolbar, prompt: "Path, method, id or notes")
    }

    private var sidebar: some View {
        let groups = RuleFormatting.grouped(shownRules)
        return VStack(alignment: .leading, spacing: 0) {
            if RuleFormatting.selectionIsHidden(selection, shown: shownRules, all: allRules) {
                // The detail pane still shows it, so without this line the highlighted row simply is
                // not there and the selection reads as lost.
                Text("selected rule hidden by filter")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, RuleFormatting.Space.step)
                    .padding(.vertical, RuleFormatting.Space.tight)
            }
            List(selection: $selection) {
                ForEach(groups.active) { rule in
                    RuleRowView(rule: rule)
                }
                if !groups.inactive.isEmpty { inactiveGroup(groups.inactive) }
            }
        }
    }

    private func inactiveGroup(_ rules: [RuleRow]) -> some View {
        DisclosureGroup(isExpanded: inactiveExpansion(matching: rules)) {
            ForEach(rules) { rule in
                RuleRowView(rule: rule).opacity(0.55)
            }
        } label: {
            Text("Inactive (" + String(rules.count) + ")")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    /// Open when the reader opened it, and open while a search has found something inside it. The
    /// rule itself is `RuleFormatting.inactiveGroupExpanded`, so it is checked rather than read.
    private func inactiveExpansion(matching rules: [RuleRow]) -> Binding<Bool> {
        Binding(
            get: {
                RuleFormatting.inactiveGroupExpanded(
                    userExpanded: inactiveExpanded, query: query, inactiveMatches: !rules.isEmpty)
            },
            set: { inactiveExpanded = $0 })
    }
}

/// One centred bold line and a hint. Never a blank table: each of these says a different thing
/// about why there is nothing to show.
struct VacancyView: View {
    let vacancy: RuleFormatting.Vacancy

    var body: some View {
        VStack(spacing: RuleFormatting.Space.snug) {
            Text(vacancy.message).font(.headline)
            Text(vacancy.hint).font(.callout).foregroundStyle(.secondary)
        }
        .multilineTextAlignment(.center)
        .padding(RuleFormatting.Space.section * 2)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The scenario loaded with rules missing. Shown above the table rather than beside a rule, because
/// what it names is not there: a dropped rule is invisible in a list of the ones that survived.
struct NotWholeBanner: View {
    let problems: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
            Text("This scenario did not load whole.").font(.callout.bold())
            ForEach(problems, id: \.self) { problem in
                Text(problem).font(.caption).fixedSize(horizontal: false, vertical: true)
            }
        }
        .foregroundStyle(.red)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(RuleFormatting.Space.step)
        .background(Color.red.opacity(0.1))
    }
}
