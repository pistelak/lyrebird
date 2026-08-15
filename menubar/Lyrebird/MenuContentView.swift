import AppKit
import SwiftUI

struct MenuContentView: View {
    let model: AppModel
    @State private var showSettings = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Divider()
            controls
            if let error = model.lastError, !error.isEmpty {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            sessionsSection
            Divider()
            recentSection
            Divider()
            footer
        }
        .padding(12)
        .frame(width: 340)
        .sheet(isPresented: $showSettings) { SettingsView() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            StatusGlyph(status: model.status).font(.title3)
            VStack(alignment: .leading, spacing: 1) {
                Text("Lyrebird").font(.headline)
                Text(model.statusLine).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var controls: some View {
        HStack {
            Button {
                Task { await model.toggle() }
            } label: {
                Label(model.status == .intercepting ? "Stop" : "Start",
                      systemImage: model.status == .intercepting ? "stop.fill" : "play.fill")
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

    private var sessionsSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("SESSIONS").font(.caption2).foregroundStyle(.secondary)
            if let list = model.sessions {
                ForEach(list.sessions) { session in
                    Button {
                        Task { await model.activate(session.name) }
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: session.name == list.active
                                  ? "largecircle.fill.circle" : "circle")
                            Text(session.name)
                            if session.verified {
                                Image(systemName: "checkmark.seal.fill").foregroundStyle(.green)
                            }
                            Spacer()
                            Text("\(session.overrideCount)").foregroundStyle(.secondary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            } else {
                Text("proxy not running").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var recentSection: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("RECENT").font(.caption2).foregroundStyle(.secondary)
            if model.recent.isEmpty {
                Text("no traffic yet").font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(Array(model.recent.prefix(8).enumerated()), id: \.offset) { _, entry in
                    HStack(spacing: 6) {
                        Text(entry.method).font(.caption2.monospaced())
                            .frame(width: 42, alignment: .leading)
                        Text("\(entry.status)").font(.caption2.monospaced())
                            .foregroundStyle(entry.status < 400 ? .green : .red)
                        Text(entry.path).font(.caption2.monospaced())
                            .lineLimit(1).truncationMode(.middle)
                    }
                }
            }
        }
    }

    private var footer: some View {
        HStack {
            Button("Dashboard") { Task { await model.openDashboard() } }
            Button("Settings") { showSettings = true }
            Spacer()
            Button("Quit") { NSApplication.shared.terminate(nil) }
        }
        .font(.caption)
    }
}
