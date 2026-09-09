import SwiftUI

struct RulesSidebarView: View {
    let model: AppModel
    @Binding var selection: String?
    @Binding var showsRecent: Bool

    enum Destination: Hashable {
        case recent
        case scenario(String)

        static func binding(selection: Binding<String?>, showsRecent: Binding<Bool>) -> Binding<Self?> {
            Binding(
                get: { showsRecent.wrappedValue ? .recent : selection.wrappedValue.map(Self.scenario) },
                set: { value in
                    switch value {
                    case .recent: showsRecent.wrappedValue = true
                    case .scenario(let name):
                        showsRecent.wrappedValue = false
                        selection.wrappedValue = name
                    // The list deselects before it selects, and taking that literally sent the window
                    // back to the active scenario between every pair of clicks — see
                    // destinationBindingKeepsTheBrowsedScenarioThroughADeselect.
                    case nil: break
                    }
                })
        }
    }

    private var destination: Binding<Destination?> {
        Destination.binding(selection: $selection, showsRecent: $showsRecent)
    }

    private var scenarios: ScenarioList? { model.scenarios ?? model.lastScenarios }

    var body: some View {
        List(selection: destination) {
            Label("Recent", systemImage: "clock")
                // A row selects only where its content has a hit shape, so give the label one that
                // spans the row rather than only the words.
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
                .tag(Destination.recent)
            let folders = (scenarios ?? ScenarioList(active: "", scenarios: [])).folders()
            Section("Scenarios") {
                rows(folders.root)
            }
            // One section per folder, so the window shows the whole tree: it is the place to move
            // between folders, and the menu deliberately shows only the active one's.
            ForEach(folders.groups, id: \.name) { group in
                Section(group.name) {
                    rows(group.scenarios)
                }
            }
        }
        .contextMenu(forSelectionType: Destination.self) { items in
            if items.count == 1, case .scenario(let name) = items.first {
                Button("Activate scenario") { activate(name) }
                    .disabled(model.busy || name == scenarios?.active)
            }
        } primaryAction: { items in
            if items.count == 1, case .scenario(let name) = items.first {
                activate(name)
            }
        }
        .listStyle(.sidebar)

    }

    /// One row per scenario, tagged individually.
    ///
    /// The tag stays on the row rather than moving to the section: `List(selection:)` matches a tag
    /// wherever it sits, and a section that carried one would make the folder itself selectable —
    /// selecting something that is not a scenario.
    @ViewBuilder
    private func rows(_ shown: [ScenarioSummary]) -> some View {
        ForEach(shown) { scenario in
            ScenarioSidebarRow(
                name: scenario.name, leaf: scenario.leaf, isActive: scenario.name == scenarios?.active,
                problems: notWhole(scenario.name), activate: { activate(scenario.name) }
            )
            .tag(Destination.scenario(scenario.name))
            .opacity(model.scenarios == nil ? 0.5 : 1)
        }
    }

    private func activate(_ name: String) {
        guard !model.busy, name != scenarios?.active else { return }
        Task { await model.activate(name) }
    }

    private func notWhole(_ name: String) -> String? {
        guard let problems = model.ownHealth?.scenariosNotWhole?[name], !problems.isEmpty else { return nil }
        return problems.joined(separator: "\n")
    }
}

private struct ScenarioSidebarRow: View {
    let name: String
    /// What the row shows: the name without its folder, which is already the heading above it.
    /// Taken from the model rather than split again here — two implementations of "the leaf" is one
    /// more than the tree has.
    let leaf: String
    let isActive: Bool
    let problems: String?
    let activate: () -> Void

    var body: some View {
        HStack(spacing: RuleFormatting.Space.snug) {
            Image(systemName: "checkmark")
                .foregroundStyle(.tint)
                // Reserve the checkmark width to keep scenario names aligned.
                .opacity(isActive ? 1 : 0)
                .accessibilityHidden(!isActive)
            Text(leaf)
                .fontWeight(isActive ? .semibold : .regular)
                .lineLimit(1).truncationMode(.middle)
            if let problems {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(RuleFormatting.warning)
                    .help(problems)
                    .accessibilityLabel("did not load whole")
            }
            Spacer(minLength: RuleFormatting.Space.tight)
        }
        // The spacer is empty space with no hit shape of its own, so a click to the right of the
        // name landed on nothing and the row stayed unselected; this gives the whole row one shape.
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        // The qualified name as well as the instruction: the row shows a leaf, and two folders may
        // each hold one with the same leaf, so the tooltip is where the name a command would take
        // is readable.
        .help(isActive ? "\(name) — active scenario" : "\(name) — double-click to activate")
        .accessibilityAction(named: "Activate scenario") { activate() }
    }
}
