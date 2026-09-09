import SwiftUI

struct RecentTrafficItem: Identifiable {
    enum ID: Hashable {
        case event(RecentEntry.Key)
        case legacy(Int)
    }

    let id: ID
    let entry: RecentEntry

    static func items(_ entries: [RecentEntry]) -> [Self] {
        entries.enumerated().map { index, entry in
            // Older engines have no event IDs; these rows cannot retain a selection across reads.
            Self(id: entry.selectionKey.map(ID.event) ?? .legacy(index), entry: entry)
        }
    }
}

struct RecentTrafficView: View {
    let model: AppModel
    let query: String
    @Binding var selection: RecentEntry.Key?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Recent traffic").font(.headline)
                Spacer()
                Button("Clear") {
                    Task {
                        await model.clearRecent()
                        if model.lastError == nil { selection = nil }
                    }
                }
                .disabled(model.busy || model.recent.isEmpty)
            }
            .padding(RuleFormatting.Space.step)
            Divider()
            if let error = model.lastError {
                Text(error).font(.callout).foregroundStyle(RuleFormatting.danger)
                    .padding(RuleFormatting.Space.step)
            }
            if let vacancy = RuleFormatting.proxyVacancy(status: model.status, controlPort: model.ownHealth?.proxyPort)
            {
                VacancyView(vacancy: vacancy)
            } else if case .unavailable(let reason) = model.recentRead {
                VacancyView(vacancy: .init(message: "Traffic could not be read.", hint: reason))
            } else if model.recent.isEmpty {
                VacancyView(
                    vacancy: .init(
                        message: model.recentPlaceholder, hint: "Requests appear here as the proxy receives them."))
            } else {
                List(selection: $selection) {
                    ForEach(RecentTrafficItem.items(filtered)) { item in
                        let entry = item.entry
                        VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
                            HStack(alignment: .firstTextBaseline, spacing: RuleFormatting.Space.snug) {
                                Text(entry.method).font(.caption.weight(.semibold))
                                Text(RuleFormatting.statusText(entry.status)).font(.caption.monospaced())
                                    .foregroundStyle(RuleFormatting.statusColor(entry.status))
                                Text(entry.path).font(.body.monospaced()).lineLimit(2)
                            }
                            Text(entry.matched == nil ? "No override answered" : "Answered by an override")
                                .font(.callout).foregroundStyle(.secondary)
                        }
                        .padding(.vertical, RuleFormatting.Space.tight)
                        .tag(entry.selectionKey)
                    }
                    if filtered.isEmpty {
                        Text("No requests match your search.").font(.callout).foregroundStyle(.secondary)
                            .padding(.vertical, RuleFormatting.Space.step)
                    }
                }
            }
        }
        .navigationSplitViewColumnWidth(min: 300, ideal: 420, max: 620)
    }

    private var filtered: [RecentEntry] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return model.recent.filter {
            needle.isEmpty
                || [$0.method, $0.path, String($0.status), $0.matched ?? ""]
                    .contains { $0.localizedCaseInsensitiveContains(needle) }
        }
    }
}

struct RecentTrafficDetail: View {
    let model: AppModel
    let selection: RecentEntry.Key?

    var body: some View {
        Group {
            if let vacancy = RuleFormatting.proxyVacancy(status: model.status, controlPort: model.ownHealth?.proxyPort)
            {
                VacancyView(vacancy: vacancy)
            } else if case .unavailable(let reason) = model.recentRead {
                VacancyView(vacancy: .init(message: "Traffic could not be read.", hint: reason))
            } else if let selection, let entry = model.recent.first(where: { $0.selectionKey == selection }) {
                Form {
                    Section("Recorded request") {
                        RequestLineView(line: .init(method: entry.method, path: entry.path))
                        if let time = entry.time { LabeledContent("Time", value: time) }
                        LabeledContent("Status", value: RuleFormatting.statusText(entry.status))
                    }
                    Section("What happened") {
                        if let matched = entry.matched {
                            LabeledContent("Answered by rule", value: matched)
                        } else {
                            Text("No override answered this request.")
                        }
                        if let reason = entry.patchSkipped { LabeledContent("Patch skipped", value: reason) }
                        if let step = entry.selectedStep { LabeledContent("Sequence response", value: String(step)) }
                        if entry.overrun == true { Text("The sequence was exhausted.") }
                        ForEach(entry.advanced ?? [], id: \.self) { id in
                            LabeledContent("Advanced sequence", value: id)
                        }
                        if let run = entry.runId { LabeledContent("Run", value: run) }
                    }
                }
                .formStyle(.grouped)
                .textSelection(.enabled)
            } else {
                VacancyView(
                    vacancy: .init(
                        message: selection == nil
                            ? "Select a request." : "This request is no longer in recent traffic.",
                        hint: "Recent traffic records what happened, independently of the scenario you are browsing."))
            }
        }
        .navigationSplitViewColumnWidth(min: 320, ideal: 420)
    }
}
