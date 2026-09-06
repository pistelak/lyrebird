import Foundation
import Observation

@MainActor
@Observable
final class AppModel {
    enum Status { case intercepting, pacDisabled, down }

    var health: Health?
    var sessions: SessionList?
    var recent: [RecentEntry] = []
    var busy = false
    /// Last CLI failure, surfaced in the menu — a shell-out that fails silently is worse than useless.
    var lastError: String?

    private var pollTask: Task<Void, Never>?
    private var refreshGeneration = 0
    private let injectedClient: MockClient?
    /// Rebuilt per use so a control URL edited in Settings takes effect without a relaunch. A test
    /// injects one instead, built on a stub session.
    private var client: MockClient { injectedClient ?? MockClient(base: Config.controlURL) }

    /// The app calls this with no arguments. `autoStart: false` lets a test exercise one action
    /// without the poll loop firing refreshes underneath it.
    init(client: MockClient? = nil, autoStart: Bool = true) {
        self.injectedClient = client
        if autoStart { start() }
    }

    func start() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while Task.isCancelled == false {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(Config.pollSeconds))
            }
        }
    }

    /// Results from a superseded refresh are dropped, so a slow refresh cannot overwrite a newer
    /// one. The three reads are still sequential, so a single snapshot spans a few milliseconds.
    func refresh() async {
        refreshGeneration &+= 1
        let generation = refreshGeneration
        let client = self.client

        let health = await client.health()
        let sessions = await client.sessions()
        let recent = await client.recent()

        guard generation == refreshGeneration else { return }
        self.health = health
        self.sessions = sessions
        self.recent = recent
    }

    var status: Status {
        guard let health, health.proxyUp == true else { return .down }
        return health.intercepting == true ? .intercepting : .pacDisabled
    }

    var statusLine: String {
        switch status {
        case .intercepting:
            return "Intercepting · \(health?.activeSession ?? "?") · \(health?.overrideCount ?? 0) override(s)"
        case .pacDisabled:
            return "Proxy up, not intercepting — press Start"
        case .down:
            return "Stopped"
        }
    }

    var simBundleId: String? { health?.simBundleId }

    func toggle() async {
        guard !busy else { return }   // guard here, not only via .disabled: SwiftUI re-renders late
        busy = true
        defer { busy = false }
        // `up` is what repairs a disabled PAC, so anything short of intercepting starts.
        let result = status == .intercepting ? await Control.down() : await Control.up()
        lastError = result.succeeded ? nil : result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        await refresh()
    }

    func activate(_ name: String) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            try await client.activate(name)
            lastError = nil
        } catch {
            // Name the session: the menu lists several, and the common failure — a 404 — means
            // this one went away between the last refresh and the click, which the bare message
            // would not say.
            lastError = "activate '\(name)': \(error.localizedDescription)"
        }
        // Refresh either way, so the stale list that produced the 404 is corrected in the same
        // tick the error appears.
        await refresh()
    }

    func relaunchApp() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        guard let bundleId = simBundleId, !bundleId.isEmpty else {
            lastError = "No simBundleId in the active profile — set it in profile.json."
            return
        }
        let result = await Control.relaunch(bundleId: bundleId)
        lastError = result.succeeded ? nil : result.output.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
