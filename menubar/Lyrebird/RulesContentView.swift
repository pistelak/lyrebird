import SwiftUI

struct VacancyView: View {
    let vacancy: RuleFormatting.Vacancy

    var body: some View {
        VStack(spacing: RuleFormatting.Space.snug) {
            Text(vacancy.message).font(.headline)
            Text(vacancy.hint)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(RuleFormatting.Space.section)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct RulesContentView: View {
    let model: AppModel
    let query: String
    @Binding var ruleSelection: RuleFormatting.ListSelection?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if let failure = RuleFormatting.actionFailure(model.lastError) { failureLine(failure) }
            rulesList
        }
        .navigationSplitViewColumnWidth(min: 300, ideal: 420, max: 620)
    }

    private var header: some View {
        Text(model.browsedScenario ?? "Rules")
            .font(.title2.weight(.semibold))
            .lineLimit(nil)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(RuleFormatting.Space.section)
    }

    private func failureLine(_ failure: String) -> some View {
        HStack(alignment: .top, spacing: RuleFormatting.Space.snug) {
            Image(systemName: "exclamationmark.circle.fill")
            Text(failure)
                .font(.caption)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: RuleFormatting.Space.tight)
            Button {
                model.dismissError()
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss")
        }
        .foregroundStyle(RuleFormatting.danger)
        .padding(RuleFormatting.Space.step)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary)
    }

    @ViewBuilder
    private var rulesList: some View {
        let problems = RuleFormatting.problems(in: model.rulesRead)
        if !problems.isEmpty { notWholeBanner(problems) }
        switch RuleFormatting.rulesColumn(
            status: model.status, read: model.rulesRead, controlPort: model.ownHealth?.proxyPort)
        {
        case .vacancy(let vacancy):
            VacancyView(vacancy: vacancy)
        case .list(let snapshot, let note):
            ScenarioFlowList(
                snapshot: snapshot, note: note,
                notes: RuleFormatting.scenarioNotes(snapshot.scenario, in: model.scenarios),
                query: query, ruleSelection: $ruleSelection)
        }
    }

    private func notWholeBanner(_ problems: [String]) -> some View {
        HStack(alignment: .top, spacing: RuleFormatting.Space.snug) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(RuleFormatting.warning)
            VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
                ForEach(problems, id: \.self) { problem in
                    Text(problem).font(.caption).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(RuleFormatting.Space.step)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary)
    }
}

private struct FlowRowView: View {
    let row: RuleFormatting.FlowListRow

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if let number = row.number {
                Text(String(number)).font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary).frame(width: 24, height: 24)
                    .overlay { Circle().strokeBorder(.quaternary, lineWidth: 1) }
            }
            VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
                HStack(alignment: .firstTextBaseline, spacing: RuleFormatting.Space.snug) {
                    Text(row.request.method).font(.caption.weight(.semibold))
                        .padding(.horizontal, 4).padding(.vertical, 2)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 4))
                    if let status = row.status {
                        Text(String(status)).font(.caption.monospaced())
                            .padding(.horizontal, 5).padding(.vertical, 2)
                            .background(.quaternary.opacity(0.5), in: Capsule())
                    }
                    Text(row.request.path).font(.body.monospaced())
                        .lineLimit(2).truncationMode(.middle)
                }
                HStack(alignment: .firstTextBaseline, spacing: RuleFormatting.Space.snug) {
                    if let kind = row.responseKind { ResponseKindBadge(kind: kind) }
                    Text(row.subtitle).font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if row.inactive { Text("Inactive").font(.caption).foregroundStyle(.secondary) }
            }
        }
        .padding(.vertical, RuleFormatting.Space.tight)
    }
}

private struct ScenarioFlowList: View {
    let snapshot: RulesSnapshot
    let note: RuleFormatting.Vacancy?
    let notes: String?
    let query: String
    @Binding var ruleSelection: RuleFormatting.ListSelection?

    var body: some View {
        let sections = RuleFormatting.flowSections(snapshot, query: query)
        List(selection: $ruleSelection) {
            // An empty List beside a footer overflows the window; see RulesWindowLayoutTests.
            if let note {
                VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
                    Text(note.message).font(.callout)
                    Text(note.hint).font(.caption).foregroundStyle(.secondary)
                }
                .fixedSize(horizontal: false, vertical: true)
                .padding(RuleFormatting.Space.step)
            } else if sections.isEmpty {
                Text("No requests match your search.")
                    .font(.callout).foregroundStyle(.secondary)
                    .padding(.vertical, RuleFormatting.Space.snug)
            }
            if let notes = notes {
                Section("About this scenario") {
                    Text(notes)
                        .font(.body).foregroundStyle(.primary)
                        .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                        .padding(.vertical, 4)
                }
            }
            ForEach(sections) { section in
                Section {
                    ForEach(section.rows) { row in
                        FlowRowView(row: row).tag(row.selection)
                    }
                    if let footer = section.footer {
                        Text(footer)
                            .font(.callout).foregroundStyle(.secondary)
                            .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
                            .padding(.top, RuleFormatting.Space.snug)
                            .listRowSeparator(.hidden, edges: .bottom)
                    }
                } header: {
                    if section.rows.contains(where: { $0.number != nil }) {
                        Label(section.title, systemImage: "arrow.down")
                    } else {
                        Text(section.title)
                    }
                }
            }
        }
        if let selected = RuleFormatting.flowRow(ruleSelection, in: snapshot),
            !sections.flatMap(\.rows).contains(where: { $0.id == selected.id })
        {
            Text("The selected rule is hidden by the search.")
                .font(.caption).foregroundStyle(.secondary)
                .padding(RuleFormatting.Space.snug)
        }
    }
}
