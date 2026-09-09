import AppKit
import Observation
import SwiftUI

/// Bridges the Find command to a toolbar TextField on macOS 14.
/// A counter lets repeated commands refocus an already-requested field.
@MainActor
@Observable
final class SearchFocus {
    private(set) var requests = 0
    func request() { requests &+= 1 }
}

/// Opens or raises the single browsing window.
@MainActor
enum WindowLauncher {
    /// Installed by the menu-bar label, which has access to the scene environment at launch.
    static var openScene: () -> Void = {}

    static func show() {
        // Match the browsing scene, not the menu-bar panel.
        let existing = NSApp.windows.first { window in
            window.identifier?.rawValue.hasPrefix(RulesWindowView.sceneId) == true && window.isVisible
        }
        if let existing {
            existing.makeKeyAndOrderFront(nil)
        } else {
            openScene()
        }
        NSApp.activate(ignoringOtherApps: true)
    }
}

struct RulesWindowView: View {
    static let sceneId = "rules"

    let model: AppModel
    let searchFocus: SearchFocus

    @State private var selection: String?
    @State private var ruleSelection: RuleFormatting.ListSelection?
    /// The selected detail step is scoped to its scenario and rule.
    @State private var pickedStep: RuleFormatting.StepPick?
    @State private var query = ""
    @State private var recentQuery = ""
    @State private var showsRecent = false
    @State private var recentSelection: RecentEntry.Key?
    @FocusState private var searchFocused: Bool

    var body: some View {
        NavigationSplitView {
            RulesSidebarView(model: model, selection: $selection, showsRecent: $showsRecent)
                .navigationSplitViewColumnWidth(min: 180, ideal: 220, max: 320)
        } content: {
            if showsRecent {
                RecentTrafficView(model: model, query: recentQuery, selection: $recentSelection)
            } else {
                RulesContentView(model: model, query: query, ruleSelection: $ruleSelection)
            }
        } detail: {
            if showsRecent {
                RecentTrafficDetail(model: model, selection: recentSelection)
            } else {
                RuleDetailView(
                    model: model, ruleSelection: ruleSelection, pickedStep: $pickedStep,
                    openRule: openRule)
            }
        }
        .navigationTitle("Lyrebird")
        .toolbar { toolbar }
        .frame(minWidth: 800, minHeight: 400)
        .task {
            DockPresence.windowOpened()
            await model.windowAppeared()
            follow(activeScenario: model.ownHealth?.activeScenario)
        }
        .onDisappear {
            model.windowClosed()
            DockPresence.windowClosed()
        }
        .onChange(of: model.ownHealth?.activeScenario) { _, name in follow(activeScenario: name) }
        .onChange(of: selection) { _, new in
            Task { await model.browse(new) }
            selectDefaultRow()
        }
        // Reconcile after same-scenario edits as well as scenario switches.
        .onChange(of: availableSelections) { _, _ in selectDefaultRow() }
        .onChange(of: browsedSnapshot?.scenario) { _, _ in selectDefaultRow() }
        .onChange(of: searchFocus.requests) { _, _ in searchFocused = true }
    }

    /// Start with the active scenario, then preserve the user's browsing selection.
    private func follow(activeScenario name: String?) {
        guard selection == nil, let name else { return }
        selection = name
    }

    private var browsedSnapshot: RulesSnapshot? {
        if case .ok(let snapshot) = model.rulesRead { return snapshot }
        return nil
    }

    private var availableSelections: [RuleFormatting.ListSelection] {
        browsedSnapshot.map { RuleFormatting.flowSections($0).flatMap(\.rows).map(\.selection) } ?? []
    }

    /// Open the first request when the selected scenario has no surviving selection.
    private func selectDefaultRow() {
        guard selection != nil else { return }
        ruleSelection = RuleFormatting.selection(current: ruleSelection, in: browsedSnapshot)
    }

    /// Resolve a related rule to its row in the configured flow.
    private func openRule(_ destination: RuleFormatting.Destination) {
        selection = destination.scenario
        let rows = browsedSnapshot.map { RuleFormatting.flowSections($0).flatMap(\.rows) } ?? []
        if case .rule(let id) = destination.selection,
            let row = rows.first(where: { $0.ruleId == id && (destination.step == nil || $0.step == destination.step) })
        {
            ruleSelection = row.selection
        } else {
            ruleSelection = destination.selection
        }
        if case .rule(let id) = destination.selection, let step = destination.step {
            pickedStep = RuleFormatting.StepPick(scenario: destination.scenario, rule: id, step: step)
        }
    }

    // MARK: - Toolbar

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .status) {
            HStack(spacing: RuleFormatting.Space.tight) {
                StatusGlyph(status: model.status)
                Text(
                    RuleFormatting.statusItem(
                        status: model.status, activeScenario: model.ownHealth?.activeScenario)
                )
                .foregroundStyle(.secondary)
            }
            .padding(.horizontal, RuleFormatting.Space.step)
            .padding(.vertical, RuleFormatting.Space.tight)
            .help(model.statusLine)
        }
        ToolbarItem {
            Button {
                Task { await model.reloadScenarios() }
            } label: {
                Label("Reload from disk", systemImage: "arrow.triangle.2.circlepath")
            }
            .disabled(model.busy)
            .help("Re-read the scenario files, picking up anything added or moved by hand")
        }
        ToolbarItem {
            HStack(spacing: RuleFormatting.Space.tight) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField(showsRecent ? "Search traffic" : "Search rules", text: showsRecent ? $recentQuery : $query)
                    .textFieldStyle(.plain)
                    .frame(width: 200)
                    .focused($searchFocused)
            }
            .padding(.horizontal, RuleFormatting.Space.step)
            .padding(.vertical, RuleFormatting.Space.tight)
        }
    }
}
