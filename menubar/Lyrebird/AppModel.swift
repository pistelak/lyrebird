import Foundation
import Observation

@MainActor
@Observable
final class AppModel {
    /// What the menu is looking at. The last three exist because the states they name used to be
    /// rendered as one of the first three: a proxy running *another* profile answered the menu's
    /// unscoped reads and showed up as "Intercepting", and anything that failed to answer at all —
    /// a timeout, an HTTP 500, a body that would not decode — showed up as "Stopped".
    enum Status: Equatable {
        case intercepting
        case pacDisabled
        case down
        /// A proxy holds the port, and it is running the profile named here — not ours.
        case foreignProfile(running: String)
        /// Something answered and could not be understood. Says nothing about what it is.
        case unreadable(String)
        /// The app does not know which profile it is configured for, so it can say nothing about
        /// any proxy: without the fingerprint it cannot scope a request, and an unscoped request
        /// is answered by whatever profile happens to be running.
        case profileUnknown(String)
    }

    var healthRead: MockClient.HealthRead?
    var scenarios: ScenarioList?
    /// The last recent-traffic read, or why there is none. A read that failed is not an empty list
    /// — see `MockClient.RecentRead`.
    var recentRead: MockClient.RecentRead?
    var busy = false
    /// Nil means rules were not requested; `status` explains whether the proxy can be read.
    var rulesRead: MockClient.RulesRead?
    /// Read saved bodies only while a window is open. Count windows so closing one does not
    /// blank another; see `RulesReadTests`.
    private(set) var openWindows = 0

    var rulesWindowOpen: Bool { openWindows > 0 }
    /// Nil follows the active scenario instead of pinning the last active name.
    private(set) var browsedScenario: String?
    /// Retain the sidebar during failed reads, scoped to its profile.
    /// See `RulesReadTests`.
    private var remembered: RememberedScenarios?

    private struct RememberedScenarios: Equatable {
        var fingerprint: String
        var list: ScenarioList
    }

    /// The list to draw a sidebar from when this poll brought none. Nil once the profile has moved
    /// on, or before the first list ever arrived.
    var lastScenarios: ScenarioList? {
        guard let remembered, remembered.fingerprint == expectedFingerprint else { return nil }
        return remembered.list
    }

    /// Bumped when the window stops wanting the rules it asked for — it closed, or moved to another
    /// scenario — so that a read in flight cannot commit one scenario's rules under another's name.
    /// See `RulesReadTests`.
    private var rulesGeneration = 0
    /// Last action failure, shown until dismissed or a later action succeeds.
    var lastError: String?
    /// The fingerprint of the profile in Settings, as the engine reports it. Never derived here.
    private(set) var expectedFingerprint: String?
    private var profileProblem: String?

    var health: Health? {
        if case .up(let health) = healthRead { return health }
        return nil
    }

    /// Hide foreign profile contents; `health` is retained only to explain the connection status.
    /// See `RulesReadTests`.
    var ownHealth: Health? {
        guard let expected = expectedFingerprint, let health, Self.isOurs(health, expected: expected) else {
            return nil
        }
        return health
    }

    /// Older engines omit the fingerprint; accept them as the CLI does.
    private static func isOurs(_ health: Health, expected: String) -> Bool {
        guard health.proxyUp == true else { return false }
        return health.profileFingerprint == nil || health.profileFingerprint == expected
    }

    private var pollTask: Task<Void, Never>?
    /// Newest refresh wins, so a slow one cannot overwrite a newer one's result.
    private var refreshGeneration = 0
    /// Bumped whenever what the app is configured to look at changes — a Settings edit, a new
    /// discovery. Work in flight when it changes is describing the old configuration and is
    /// dropped. It is separate from `refreshGeneration` on purpose: the poll loop bumps that one
    /// every couple of seconds, and a discovery racing it would be thrown away every time.
    private var configGeneration = 0
    private let injectedClient: MockClient?

    /// The settings a piece of work was started under.
    ///
    /// `@AppStorage` writes on every keystroke, so the profile can change halfway through a
    /// typed-in path and long before the sheet is dismissed. The generation counters cannot catch
    /// that — nothing has bumped them yet — so a read begun under one profile would commit its
    /// answer under another's name, which is the whole failure this file is about.
    private struct Settings: Equatable {
        var profilePath: String
        var lyrebirdPath: String
        var controlURL: URL
    }

    private static var currentSettings: Settings {
        Settings(
            profilePath: Config.profilePath,
            lyrebirdPath: Config.lyrebirdPath,
            controlURL: Config.controlURL)
    }

    /// The settings `expectedFingerprint` was discovered under. A fingerprint names the profile it
    /// was asked about and no other, so once Settings has moved on it scopes nothing: the header
    /// built from it would claim profile A on a call meant for profile B.
    private var fingerprintSettings: Settings?

    private let discover: @Sendable () async throws -> String
    /// Rebuilt per use so a control URL edited in Settings takes effect without a relaunch, and so
    /// the client always carries the fingerprint the model currently holds. A test injects one
    /// instead, built on a stub session.
    private var client: MockClient {
        var client = injectedClient ?? MockClient(base: Config.controlURL)
        client.profile = expectedFingerprint
        return client
    }

    /// The app calls this with no arguments. `autoStart: false` lets a test exercise one action
    /// without the poll loop firing refreshes underneath it; `discover` lets it name the profile
    /// without shelling out.
    init(
        client: MockClient? = nil,
        autoStart: Bool = true,
        expectedFingerprint: String? = nil,
        discover: (@Sendable () async throws -> String)? = nil
    ) {
        self.injectedClient = client
        self.expectedFingerprint = expectedFingerprint
        self.discover = discover ?? { try await Control.fingerprint() }
        // A fingerprint handed in belongs to the settings as they stand now, the same way a
        // discovered one belongs to the settings it was discovered under.
        self.fingerprintSettings = expectedFingerprint == nil ? nil : Self.currentSettings
        if autoStart { start() }
    }

    func start() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            // Discovery first: until the profile has a name there is no request this app is
            // entitled to send.
            await self?.discoverProfile()
            while Task.isCancelled == false {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(Config.pollSeconds))
            }
        }
    }

    /// Asks the CLI which profile the app is configured for. On failure the fingerprint stays nil
    /// and the menu says so — it does not fall back to reading the proxy unscoped, because an
    /// absent header is exactly what makes another profile's proxy answer as if it were ours.
    func discoverProfile() async {
        configGeneration &+= 1
        let generation = configGeneration
        let settings = Self.currentSettings
        do {
            let fingerprint = try await discover()
            guard generation == configGeneration, settings == Self.currentSettings else { return }
            expectedFingerprint = fingerprint
            fingerprintSettings = settings
            profileProblem = nil
        } catch {
            guard generation == configGeneration, settings == Self.currentSettings else { return }
            expectedFingerprint = nil
            fingerprintSettings = nil
            profileProblem = error.localizedDescription
            lastError = "could not determine the profile: \(error.localizedDescription)"
            healthRead = nil
            scenarios = nil
            recentRead = nil
            rulesRead = nil
        }
    }

    /// Re-reads everything after the Settings sheet closes. `@AppStorage` has already written the
    /// new values, so what is on screen describes the old ones until this runs.
    func settingsChanged() async {
        configGeneration &+= 1
        healthRead = nil
        scenarios = nil
        recentRead = nil
        rulesRead = nil
        expectedFingerprint = nil
        fingerprintSettings = nil
        profileProblem = nil
        // The Dock setting is read from the same store and changes nothing the reads above cover.
        DockPresence.settingChanged()
        await discoverProfile()
        await refresh()
    }

    /// Results from a superseded refresh are dropped, so a slow refresh cannot overwrite a newer
    /// one, and neither can one that describes the profile Settings held a moment ago. The reads
    /// are still sequential, so a single snapshot spans a few milliseconds.
    func refresh() async {
        refreshGeneration &+= 1
        let generation = refreshGeneration
        let configuration = configGeneration
        let rulesRun = rulesGeneration
        let browsing = browsedScenario
        let settings = Self.currentSettings
        // Not merely "is there a fingerprint" but "is it this profile's". Between a Settings edit
        // and the sheet closing, the fingerprint on hand was discovered under the profile that was
        // configured a keystroke ago; scoping a call with it would name the wrong profile, and the
        // dismissal re-discovers anyway.
        guard let expected = expectedFingerprint, fingerprintSettings == settings else { return }
        let client = self.client

        let read = await client.health()
        var scenarios: ScenarioList?
        var recentRead: MockClient.RecentRead?
        var rulesRead: MockClient.RulesRead?
        // The scenario list, the recent traffic and the rules belong to whichever profile answered,
        // so they are read only when that is ours. Showing another profile's scenarios under this
        // profile's name is the same mistake as showing its health.
        if case .up(let health) = read, Self.isOurs(health, expected: expected) {
            scenarios = await client.scenarios()
            recentRead = await client.recent()
            if rulesWindowOpen { rulesRead = await client.rules(scenario: browsing) }
        }

        guard generation == refreshGeneration, configuration == configGeneration,
            settings == Self.currentSettings
        else { return }
        self.healthRead = read
        self.scenarios = scenarios
        self.recentRead = recentRead
        // Every list that arrives, not only one that renamed the active scenario — see
        // `RulesReadTests`.
        if let scenarios { remembered = RememberedScenarios(fingerprint: expected, list: scenarios) }

        guard rulesRun == rulesGeneration else { return }  // see `rulesGeneration`
        // Nil, not the previous snapshot: the gate above failing means this proxy is not ours to
        // read, and last poll's rules would then be shown beside a header saying so.
        self.rulesRead = rulesRead
    }

    /// Register a window without waiting for a refresh.
    func windowOpened() { openWindows += 1 }

    /// Read immediately on opening instead of waiting for the next poll.
    func windowAppeared() async {
        windowOpened()
        await refresh()
    }

    /// Stop reading rules when the last window closes; ignore duplicate close notifications.
    /// See `RulesReadTests`.
    func windowClosed() {
        openWindows = max(0, openWindows - 1)  // `onDisappear` can arrive for a window that never counted
        guard openWindows == 0 else { return }
        rulesGeneration &+= 1
        rulesRead = nil
    }

    /// Dismiss the last failure. The window shows it until it is read; the next action that succeeds
    /// clears it too.
    func dismissError() { lastError = nil }

    /// Browse without activating. Discard an unrelated snapshot while the new read is in flight.
    /// See `RulesReadTests`.
    func browse(_ scenario: String?) async {
        guard scenario != browsedScenario else { return }
        browsedScenario = scenario
        rulesGeneration &+= 1  // a read for the previous name is still in flight
        if !describes(scenario) { rulesRead = nil }
        await refresh()
    }

    /// Whether the snapshot on screen is of the scenario `browse` is moving to. Nil is "the active
    /// one", which no snapshot can be matched against by name — the read has to happen.
    private func describes(_ scenario: String?) -> Bool {
        guard let scenario, case .ok(let snapshot) = rulesRead else { return false }
        return snapshot.scenario == scenario
    }

    var status: Status {
        guard let expected = expectedFingerprint else {
            return .profileUnknown(profileProblem ?? "asking the CLI which profile this is")
        }
        switch healthRead {
        case .up(let health):
            guard health.proxyUp == true else { return .down }
            // A health with no fingerprint is an older engine, which the CLI accepts too: it is a
            // proxy that cannot answer the question, not one that answered it differently.
            if let running = health.profileFingerprint, running != expected {
                return .foreignProfile(running: running)
            }
            return health.intercepting == true ? .intercepting : .pacDisabled
        case .unreadable(let reason):
            return .unreadable(reason)
        case .down, nil:
            return .down
        }
    }

    var statusLine: String {
        switch status {
        case .intercepting:
            return "Intercepting · \(health?.activeScenario ?? "?") · \(health?.overrideCount ?? 0) override(s)"
        case .pacDisabled:
            return "Proxy up, not intercepting — press Start"
        case .down:
            return "Stopped"
        case .foreignProfile(let running):
            return "Proxy up for another profile (\(running)) — Stop it, or change the profile in Settings"
        case .unreadable(let reason):
            return "Could not read the proxy's health: \(reason)"
        case .profileUnknown(let reason):
            return "Profile unknown: \(reason) — check the launcher path in Settings"
        }
    }

    /// Empty rows can mean an empty response or a failed read; use `recentRead` for the distinction.
    var recent: [RecentEntry] {
        if case .ok(let entries) = recentRead { return entries }
        return []
    }

    /// What the RECENT section says when it lists nothing. "no traffic yet" is a claim about the
    /// proxy, and it was made for a read that never came back.
    var recentPlaceholder: String {
        if case .unavailable(let reason) = recentRead { return "could not be read: \(reason)" }
        return recentRead == nil ? "not read yet" : "no traffic yet"
    }

    /// What the SCENARIOS section says when there is no list to show — the reason differs, and
    /// "proxy not running" is untrue for three of these.
    var scenariosPlaceholder: String {
        switch status {
        case .foreignProfile: return "another profile's proxy"
        case .unreadable: return "the proxy could not be read"
        case .profileUnknown: return "profile unknown"
        case .intercepting, .pacDisabled, .down: return "proxy not running"
        }
    }

    /// True when the button says Stop and runs `down`. A foreign proxy and an unreadable one both
    /// hold the port this profile needs, and `down` is the command that deals with that — it
    /// adopts the pid from the unscoped health reading, and reports "nothing to stop" when that is
    /// the truth.
    var stopsRatherThanStarts: Bool {
        switch status {
        case .intercepting, .foreignProfile, .unreadable: return true
        case .pacDisabled, .down, .profileUnknown: return false
        }
    }

    /// Only ever this profile's. `lyrebird relaunch` does no fingerprint check of its own, so
    /// handing it the bundle id another profile's proxy reported would relaunch someone else's app
    /// against a proxy this menu is not reading.
    var simBundleId: String? {
        switch status {
        case .intercepting, .pacDisabled: return health?.simBundleId
        case .down, .foreignProfile, .unreadable, .profileUnknown: return nil
        }
    }

    func toggle() async {
        guard !busy else { return }  // guard here, not only via .disabled: SwiftUI re-renders late
        busy = true
        defer { busy = false }
        // `up` is what repairs a disabled PAC, so anything short of intercepting starts — except
        // the states where something else holds the port, which have to be stopped first.
        let result = stopsRatherThanStarts ? await Control.down() : await Control.up()
        lastError = result.succeeded ? nil : result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        await refresh()
    }

    /// Settings write on each keystroke, before profile rediscovery. Refuse writes in that gap.
    private var writeRefusal: String? {
        guard expectedFingerprint != nil else {
            return "the app does not know which profile it is configured for — check the launcher path in Settings"
        }
        guard fingerprintSettings == Self.currentSettings else {
            return "the profile in Settings has changed — close Settings so the app can re-read it"
        }
        return nil
    }

    func clearRecent() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        if let refusal = writeRefusal {
            lastError = "clear recent traffic: \(refusal)"
            return
        }
        do {
            try await client.clearRecent()
            lastError = nil
        } catch {
            lastError = "clear recent traffic: \(error.localizedDescription)"
        }
        await refresh()
    }

    func activate(_ name: String) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        if let refusal = writeRefusal {
            // Scoped to nothing, or scoped to the profile configured a keystroke ago: either way the
            // PUT lands on a proxy this app is not describing.
            lastError = "activate '\(name)': \(refusal)"
            return
        }
        do {
            try await client.activate(name)
            lastError = nil
        } catch {
            // Name the scenario: the menu lists several, and the common failure — a 404 — means
            // this one went away between the last refresh and the click, which the bare message
            // would not say.
            lastError = "activate '\(name)': \(error.localizedDescription)"
        }
        // Refresh either way, so the stale list that produced the 404 is corrected in the same
        // tick the error appears.
        await refresh()
    }

    /// Re-read the scenario files, for a profile edited outside the app.
    ///
    /// Mirrors `activate`, including the early return: a write this app cannot scope to a profile
    /// sends nothing and refreshes nothing, because there is no proxy it is entitled to ask. On
    /// every other path it refreshes, so a refusal and the list that produced it are corrected in
    /// the same tick.
    func reloadScenarios() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        if let refusal = writeRefusal {
            lastError = "reload scenarios: \(refusal)"
            return
        }
        do {
            try await client.reloadScenarios()
            lastError = nil
        } catch {
            // The engine's own sentence: a refused reload names the file that could not be read,
            // and "reload failed" without it sends the reader nowhere.
            lastError = "reload scenarios: \(error.localizedDescription)"
        }
        await refresh()
    }

    func relaunchApp() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        guard let bundleId = simBundleId, !bundleId.isEmpty else {
            switch status {
            case .intercepting, .pacDisabled, .down:
                lastError = "No simBundleId in the active profile — set it in profile.json."
            case .foreignProfile, .unreadable, .profileUnknown:
                lastError = "Relaunch needs this profile's own proxy: \(statusLine)"
            }
            return
        }
        let result = await Control.relaunch(bundleId: bundleId)
        lastError = result.succeeded ? nil : result.output.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
