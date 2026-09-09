import AppKit
import SwiftUI

struct MenuContentView: View {
    let model: AppModel
    @State private var showSettings = false

    var body: some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.step) {
            MenuHeader(model: model)
            Divider()
            MenuControls(model: model)
            if let error = model.lastError, !error.isEmpty {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(RuleFormatting.danger)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            MenuScenarios(model: model)
            Divider()
            MenuRecentTraffic(model: model)
            Divider()
            MenuFooter(showSettings: $showSettings)
        }
        .padding(RuleFormatting.Space.section)
        .frame(width: 340)
        // Refresh the profile and discard old readings after settings change.
        .sheet(isPresented: $showSettings, onDismiss: { Task { await model.settingsChanged() } }) {
            SettingsView()
        }
    }

}

private struct MenuHeader: View {
    let model: AppModel

    var body: some View {
        HStack(spacing: 8) {
            StatusGlyph(status: model.status).font(.title3)
            VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
                Text("Lyrebird").font(.headline)
                Text(model.statusLine).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

private struct MenuControls: View {
    let model: AppModel

    var body: some View {
        HStack {
            Button {
                Task { await model.toggle() }
            } label: {
                Label(
                    model.stopsRatherThanStarts ? "Stop" : "Start",
                    systemImage: model.stopsRatherThanStarts ? "stop.fill" : "play.fill")
            }
            .disabled(model.busy)

            Button {
                Task { await model.relaunchApp() }
            } label: {
                Label("Relaunch app", systemImage: "arrow.clockwise")
            }
            .disabled(model.busy || (model.simBundleId ?? "").isEmpty)
            .help(model.simBundleId.map { "Relaunch \($0)" } ?? "Set simBundleId in profile.json")

            if model.busy { ProgressView().controlSize(.small) }
        }
    }
}

/// The scenarios in the *active* scenario's folder, and nothing else.
///
/// A profile can hold more scenarios than a menu can show, and this menu is for switching between
/// the ones a run is about — which is what a folder groups. The whole tree is the window's job
/// (`RulesSidebarView`), reached from Scenarios below. Switching folder is `lyrebird use group/name`
/// or a click in that window; the menu follows once the engine reports the new active scenario.
private struct MenuScenarios: View {
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let list = model.scenarios {
                let shown = list.shownFolder()
                // The caption carries the empty case on its own — it is the one that says the
                // active scenario is not in this list. `scenariosPlaceholder` is not the sentence
                // for it: that one explains why there is no list at all, so it would read "proxy
                // not running" while the proxy is answering.
                caption(shown.caption)
                ForEach(shown.scenarios) { scenario in
                    Button {
                        Task { await model.activate(scenario.name) }
                    } label: {
                        HStack(spacing: RuleFormatting.Space.snug) {
                            Image(
                                systemName: scenario.name == list.active
                                    ? "largecircle.fill.circle" : "circle")
                            // The leaf reads better in a folder that is already named above it; the
                            // qualified name is what gets sent, and what the tooltip shows, because
                            // that is the name every command takes.
                            Text(scenario.leaf)
                            if scenario.verified {
                                Image(systemName: "checkmark.seal.fill").foregroundStyle(RuleFormatting.success)
                            }
                            Spacer()
                            Text("\(scenario.overrideCount)").foregroundStyle(.secondary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help(scenario.name)
                }
            } else {
                caption(nil)
                Text(model.scenariosPlaceholder).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func caption(_ folder: String?) -> some View {
        Text(folder.map { "SCENARIOS · \($0)" } ?? "SCENARIOS")
            .font(.caption2)
            .foregroundStyle(.secondary)
    }
}

private struct MenuRecentTraffic: View {
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
            HStack {
                Text("RECENT").font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Button("Clear") { Task { await model.clearRecent() } }
                    .font(.caption2)
                    .buttonStyle(.borderless)
                    .disabled(model.busy || model.recent.isEmpty)
                    .help("Clear recent traffic")
                    .accessibilityLabel("Clear recent traffic")
            }
            if model.recent.isEmpty {
                Text(model.recentPlaceholder).font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(RecentTrafficItem.items(Array(model.recent.prefix(8)))) { item in
                    let entry = item.entry
                    HStack(spacing: RuleFormatting.Space.snug) {
                        Text(entry.method).font(.caption2.monospaced())
                            .frame(width: 42, alignment: .leading)
                        Text(RuleFormatting.statusText(entry.status)).font(.caption2.monospaced())
                            .foregroundStyle(RuleFormatting.statusColor(entry.status))
                        Text(entry.path).font(.caption2.monospaced())
                            .lineLimit(1).truncationMode(.middle)
                    }
                }
            }
        }
    }
}

private struct MenuFooter: View {
    @Binding var showSettings: Bool

    var body: some View {
        HStack {
            Button("Scenarios") { WindowLauncher.show() }
            Button("Settings") { showSettings = true }
            Spacer()
            Button("Quit") { NSApplication.shared.terminate(nil) }
        }
        .font(.caption)
    }
}
