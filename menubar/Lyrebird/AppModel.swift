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
        /// Our proxy is up and could not read whether the PAC points at it — `networksetup` failed
        /// or timed out, or the default route moved. Not `pacDisabled`: that is an observation,
        /// this is the absence of one, and the menu used to present the two as the same fact.
        case pacUnobserved(String)
        case down
        /// Nothing answers the port, and the Mac's proxy settings still point at it: the proxy
        /// died — a crash, a `kill -9`, a bad sleep — with its PAC installed. Read as `.down`, this
        /// showed "Stopped" beside a Start that `up` refuses, and no Stop to run the `down` that
        /// puts the network back — see `StaleSessionTests`.
        case stale
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
    /// What `status --json` last said, read at discovery and again when the health poll finds
    /// nothing on the port: the one reading that tells "stopped" from "stopped, with the network
    /// still routed at it". Nil when the last probe failed, which shows as `.down`.
    private(set) var statusReading: Control.StatusReading?
    /// The last scenario-list read, or why there is none. A read that failed is not "no proxy" —
    /// see `MockClient.ScenariosRead`.
    var scenariosRead: MockClient.ScenariosRead?
    /// The list itself, for the readers that only need one.
    var scenarios: ScenarioList? {
        if case .ok(let list) = scenariosRead { return list }
        return nil
    }
    /// The last recent-traffic read, or why there is none. A read that failed is not an empty list
    /// — see `MockClient.RecentRead`.
    var recentRead: MockClient.RecentRead?
    var busy = false
    /// Nil means rules were not requested; `status` explains whether the proxy can be read.
    var rulesRead: MockClient.RulesRead?
    /// What the file preview found, or why there is none. A preview that failed is not "no proxy":
    /// the reason is shown where the rules would be — see `RulesReadTests`.
    enum PreviewRead: Sendable, Equatable {
        case ok(ProfilePreview)
        case unavailable(String)
    }
    /// The last file preview, read only while a window is open and the proxy is stopped. Nil in
    /// every other state, so a preview can never survive a poll into the live one.
    var previewRead: PreviewRead?
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

    /// A proxy that reports no fingerprint predates the guard, so nothing can vouch for whose it
    /// is: it is another profile, as the CLI now treats it. Accepting it let this app read, activate
    /// and reset a stranger's profile behind a daemon that enforces nothing.
    private static func isOurs(_ health: Health, expected: String) -> Bool {
        guard health.proxyUp == true else { return false }
        return health.profileFingerprint == expected
    }

    /// The one reading that means nothing is running: no listener on the port, or a control API
    /// saying so. `status` and the file preview agree through this, so the preview runs in exactly
    /// the state the window calls "stopped".
    private static func isStopped(_ read: MockClient.HealthRead?) -> Bool {
        switch read {
        case .down, nil: return true
        case .up(let health): return health.proxyUp != true
        case .unreadable: return false
        }
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
    /// Configuration can change while a request is in flight. Check the actual values as well as
    /// the generation so a reading cannot arrive under another profile's name; see ProfileScopingTests.
    private struct Settings: Equatable {
        var profilePath: String
        var lyrebirdPath: String
        var controlURL: Result<URL, Config.ControlURLProblem>
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

    private let discover: @Sendable () async throws -> Control.StatusReading
    /// Rebuilt per use so a control URL edited in Settings takes effect without a relaunch, and so
    /// the client always carries the fingerprint the model currently holds. A test injects one
    /// instead, built on a stub session.
    private var client: MockClient {
        get throws {
            let url = try Config.controlURL.get()
            var client = injectedClient ?? MockClient(base: url)
            client.profile = expectedFingerprint
            return client
        }
    }

    /// The app calls this with no arguments. `autoStart: false` lets a test exercise one action
    /// without the poll loop firing refreshes underneath it; `discover` lets it name the profile
    /// without shelling out.
    init(
        client: MockClient? = nil,
        autoStart: Bool = true,
        expectedFingerprint: String? = nil,
        discover: (@Sendable () async throws -> Control.StatusReading)? = nil
    ) {
        self.injectedClient = client
        self.expectedFingerprint = expectedFingerprint
        self.discover = discover ?? { try await Control.statusReading() }
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
            _ = try settings.controlURL.get()
            let reading = try await discover()
            guard generation == configGeneration, settings == Self.currentSettings else { return }
            expectedFingerprint = reading.fingerprint
            statusReading = reading
            fingerprintSettings = settings
            profileProblem = nil
        } catch {
            guard generation == configGeneration, settings == Self.currentSettings else { return }
            expectedFingerprint = nil
            fingerprintSettings = nil
            profileProblem = error.localizedDescription
            lastError = "could not determine the profile: \(error.localizedDescription)"
            clearReadings()
        }
    }

    /// Invalidates old readings and discovers the profile after Settings saves its values.
    func settingsChanged() async {
        configGeneration &+= 1
        clearReadings()
        expectedFingerprint = nil
        fingerprintSettings = nil
        profileProblem = nil
        // The Dock setting is read from the same store and changes nothing the reads above cover.
        DockPresence.settingChanged()
        await discoverProfile()
        await refresh()
    }

    /// Settings, committed. A changed control URL first stops the session the old one addresses:
    /// `down` finds this user's one session from anywhere, so nothing about the old target has to
    /// be kept — only that it runs before the new value is written. It runs on every URL change,
    /// not only when the last health reading said a proxy was up: a stale or unreadable reading is
    /// no proof there is nothing to stop, and `down` over no session already says so and succeeds.
    /// A `down` that fails refuses the edit and says why, rather than leaving a proxy intercepting
    /// on a port the app no longer describes while the menu says Stopped. One save at a time: a
    /// second one arriving while the first's `down` runs would race it for the values written.
    /// Returns the refusal, or nil once the settings are in.
    func commitSettings(controlURL: String, launcher: String, profile: String, dockOnlyWhileWindowOpen: Bool)
        async -> String?
    {
        guard !busy else { return "another save is still running — try again in a moment" }
        busy = true
        defer { busy = false }
        let previous = Config.defaults.string(forKey: Config.controlURLKey) ?? Config.defaultControlURL
        if controlURL != previous {
            if let failure = await Control.down().failure {
                return "the session on \(previous) could not be stopped, so the control URL was not changed: \(failure)"
            }
        }
        Config.defaults.set(controlURL, forKey: Config.controlURLKey)
        Config.defaults.set(launcher, forKey: Config.lyrebirdPathKey)
        Config.defaults.set(profile, forKey: Config.profilePathKey)
        Config.defaults.set(dockOnlyWhileWindowOpen, forKey: Config.dockOnlyWhileWindowOpenKey)
        await settingsChanged()
        return nil
    }

    private func clearReadings() {
        healthRead = nil
        statusReading = nil
        scenariosRead = nil
        recentRead = nil
        rulesRead = nil
        previewRead = nil
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
        let client: MockClient
        do {
            client = try self.client
        } catch {
            expectedFingerprint = nil
            fingerprintSettings = nil
            profileProblem = error.localizedDescription
            lastError = error.localizedDescription
            clearReadings()
            return
        }
        // Not merely "is there a fingerprint" but "is it this profile's". Between a Settings edit
        // and the sheet closing, the fingerprint on hand was discovered under the profile that was
        // configured a keystroke ago; scoping a call with it would name the wrong profile, and the
        // dismissal re-discovers anyway.
        guard let expected = expectedFingerprint, fingerprintSettings == settings else { return }

        let read = await client.health()
        // `status --json` is asked only where the health poll cannot answer: on the first poll
        // that finds nothing on the port after one that did not — a nil previous read counts, so
        // the first poll after discovery probes too — and on every poll while the reading says the
        // network is still routed at the dead proxy, so a `down` run from a terminal clears it
        // within a poll. Not on every poll while merely stopped: that is a Python spawn every two
        // seconds on an idle machine. A probe that failed is not a reading: the state falls back
        // to "stopped", with no `lastError` — a background observation is not a command the user
        // ran — and `up`'s refusal names `down` if there was a session after all.
        // See `StaleSessionTests`.
        var statusReading = statusReading
        if Self.isStopped(read),
            healthRead == nil || !Self.isStopped(healthRead) || statusReading?.routesToADeadProxy == true
        {
            statusReading = try? await discover()
        }
        var scenariosRead: MockClient.ScenariosRead?
        var recentRead: MockClient.RecentRead?
        var rulesRead: MockClient.RulesRead?
        var previewRead: PreviewRead?
        // The scenario list, the recent traffic and the rules belong to whichever profile answered,
        // so they are read only when that is ours. Showing another profile's scenarios under this
        // profile's name is the same mistake as showing its health.
        if case .up(let health) = read, Self.isOurs(health, expected: expected) {
            scenariosRead = await client.scenarios()
            recentRead = await client.recent()
            if rulesWindowOpen { rulesRead = await client.rules(scenario: browsing) }
        } else if Self.isStopped(read), rulesWindowOpen {
            // Nothing to ask, so the files are read instead — through the CLI, which is the one
            // reader of scenario files, and only while a window is open to show them. A foreign or
            // unreadable proxy is not "stopped": those states keep their own placeholders.
            previewRead = await Self.preview()
        }

        guard generation == refreshGeneration, configuration == configGeneration,
            settings == Self.currentSettings
        else { return }
        self.healthRead = read
        self.statusReading = statusReading
        self.scenariosRead = scenariosRead
        self.recentRead = recentRead
        // Every list that arrives, not only one that renamed the active scenario — see
        // `RulesReadTests`.
        if case .ok(let list) = scenariosRead { remembered = RememberedScenarios(fingerprint: expected, list: list) }

        guard rulesRun == rulesGeneration else { return }  // see `rulesGeneration`
        // Nil, not the previous snapshot: the gate above failing means this proxy is not ours to
        // read, and last poll's rules would then be shown beside a header saying so.
        self.rulesRead = rulesRead
        // Behind the same guard as the rules: both are read for a window, and a preview that
        // finished after the last window closed used to be committed to nobody and shown to the
        // next window before its own read began — see `FilePreviewTests`.
        self.previewRead = previewRead
    }

    /// Register a window without waiting for a refresh.
    func windowOpened() {
        openWindows += 1
    }

    /// Read immediately on opening instead of waiting for the next poll.
    func windowAppeared() async {
        windowOpened()
        await refresh()
    }

    /// Stop reading rules when the last window closes; ignore duplicate close notifications.
    /// See `RulesReadTests`.
    func windowClosed() {
        openWindows = max(0, openWindows - 1)  // Duplicate close notifications must not make the count negative.
        guard openWindows == 0 else { return }
        rulesGeneration &+= 1
        rulesRead = nil
        previewRead = nil
    }

    /// Run `lyrebird scenario show` and read what it printed.
    private static func preview() async -> PreviewRead {
        guard let result = await Control.preview() else {
            return .unavailable("`lyrebird scenario show` did not answer within \(Int(Control.fingerprintTimeout))s")
        }
        return decodePreview(result.output)
    }

    /// What a finished `scenario show` printed means. The exit status is not consulted: the command
    /// exits 1 whenever it could not show everything and prints the JSON — with the reason in
    /// `problems` — either way, and the payload's own sentence beats "exit status 1". A payload with
    /// no scenarios is a failure carrying its reasons, never an empty profile — see `RulesReadTests`.
    static func decodePreview(_ output: String) -> PreviewRead {
        guard let span = Control.jsonSpan(in: output),
            let preview = try? JSONDecoder().decode(ProfilePreview.self, from: span)
        else {
            let printed = output.trimmingCharacters(in: .whitespacesAndNewlines)
            return .unavailable(printed.isEmpty ? "`lyrebird scenario show` printed nothing" : printed)
        }
        guard !preview.scenarios.isEmpty else {
            return .unavailable(
                preview.problems.isEmpty
                    ? "the profile has no scenario files" : preview.problems.joined(separator: "\n"))
        }
        return .ok(preview)
    }

    /// Dismiss the last failure. The window shows it until it is read; the next action that succeeds
    /// clears it too.
    func dismissError() {
        lastError = nil
    }

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
        if case .failure(let problem) = Config.controlURL {
            return .profileUnknown(problem.localizedDescription)
        }
        guard let expected = expectedFingerprint else {
            return .profileUnknown(profileProblem ?? "asking the CLI which profile this is")
        }
        if Self.isStopped(healthRead) { return statusReading?.routesToADeadProxy == true ? .stale : .down }
        switch healthRead {
        case .up(let health):
            // A health with no fingerprint is an older engine that cannot answer the question, and
            // the CLI refuses it too: until a proxy says whose it is, it is somebody else's.
            guard let running = health.profileFingerprint else {
                return .foreignProfile(running: "none reported")
            }
            if running != expected {
                return .foreignProfile(running: running)
            }
            // Before the flag: the engine sends `intercepting: false` beside a `pacError`, and the
            // flag then reports what it could not see — see `ProfileScopingTests`.
            if let reason = health.pacError { return .pacUnobserved(reason) }
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
        case .pacUnobserved(let reason):
            return "Proxy up, PAC could not be read: \(reason)"
        case .down:
            return "Stopped"
        case .stale:
            // An instruction, not a promise: with the journal deleted by hand, `down` finds no
            // session and restores nothing, and menubar/README.md names the manual remedy.
            return "Proxy stopped, but the Mac's proxy settings still point at it — press Stop"
        case .foreignProfile(let running):
            return "Proxy up for another profile (\(running)) — Stop it, or change the profile in Settings"
        case .unreadable(let reason):
            return "Could not read the proxy's health: \(reason)"
        case .profileUnknown(let reason):
            return "Profile unknown: \(reason) — check Settings"
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
    /// "proxy not running" is true for exactly one of them. See `RulesReadTests`.
    var scenariosPlaceholder: String {
        switch status {
        case .foreignProfile: return "another profile's proxy"
        case .unreadable: return "the proxy could not be read"
        case .profileUnknown: return "profile unknown"
        case .down, .stale: return "proxy not running"
        case .intercepting, .pacDisabled, .pacUnobserved:
            if case .unavailable(let reason) = scenariosRead { return "scenarios could not be read: \(reason)" }
            return "scenarios not read yet"
        }
    }

    /// True when the button says Stop and runs `down`. A foreign proxy and an unreadable one both
    /// hold the port this profile needs, and `down` is the command that deals with that — it
    /// adopts the pid from the unscoped health reading, and reports "nothing to stop" when that is
    /// the truth.
    var stopsRatherThanStarts: Bool {
        switch status {
        // Stale stops too: `down` restores the settings from the journal, and `up` over that
        // journal is refused — Start was the one button this state used to offer.
        case .intercepting, .foreignProfile, .unreadable, .stale: return true
        // Unobserved starts too: `up` is what re-observes, and it says what it found.
        case .pacDisabled, .pacUnobserved, .down, .profileUnknown: return false
        }
    }

    /// Only ever this profile's. `lyrebird relaunch` does no fingerprint check of its own, so
    /// handing it the bundle id another profile's proxy reported would relaunch someone else's app
    /// against a proxy this menu is not reading.
    var simBundleId: String? {
        switch status {
        case .intercepting, .pacDisabled, .pacUnobserved: return health?.simBundleId
        case .down, .stale, .foreignProfile, .unreadable, .profileUnknown: return nil
        }
    }

    /// Whether Quit runs `down` first: this profile's proxy is up, whether or not it intercepts —
    /// `down` is what releases its journal and puts the network back either way. Never a dead
    /// session, a foreign proxy or an unreadable one: on the shared port a journal may be another
    /// profile's, and `down` consumes whichever exists, so that authorization stays with an
    /// explicit Stop — see `QuitTests`.
    private var quitStopsProxy: Bool {
        switch status {
        case .intercepting, .pacDisabled, .pacUnobserved: return true
        case .down, .stale, .foreignProfile, .unreadable, .profileUnknown: return false
        }
    }

    /// Whether the app may quit now. The menu-bar icon is the only sign on screen that the Mac's
    /// proxy settings are rewritten, so Quit stops this profile's running proxy before it goes —
    /// or stays, with the reason in the menu, when it could not: a "Stop and Quit" that stopped
    /// nothing must not vanish. `leaveProxy` is "Quit, leave the proxy running", and it belongs
    /// to this request alone: kept as model state, a choice made while another quit was still
    /// deciding used to be consumed by the next plain ⌘Q. See `QuitTests`.
    ///
    /// Refused at once while another action runs, rather than waited for: `Control.up()` has no
    /// deadline and neither have the engine's simulator calls, so a wait could hold the app for
    /// as long as a hanging `up`. The refresh that follows is an attempt at a current reading, not
    /// a guarantee — a poll starting during it supersedes it and the last committed status is
    /// what decides.
    func prepareToQuit(leaveProxy: Bool = false) async -> Bool {
        guard !busy else {
            lastError = "another action is still running — quit when it finishes"
            return false
        }
        busy = true
        defer { busy = false }
        await refresh()
        guard !leaveProxy, quitStopsProxy else { return true }
        if let failure = await Control.down().failure {
            lastError = failure
            return false
        }
        return true
    }

    func toggle() async {
        guard !busy else { return }  // Guard here too: a menu action can arrive before its enabled state updates.
        busy = true
        defer { busy = false }
        // `up` is what repairs a disabled PAC, so anything short of intercepting starts — except
        // the states where something else holds the port, which have to be stopped first.
        let result = stopsRatherThanStarts ? await Control.down() : await Control.up()
        lastError = result.failure
        await refresh()
    }

    /// Refuse writes between a settings save and profile rediscovery.
    private var writeRefusal: String? {
        if case .failure(let problem) = Config.controlURL {
            return problem.localizedDescription
        }
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
            case .intercepting, .pacDisabled, .pacUnobserved, .down, .stale:
                lastError = "No simBundleId in the active profile — set it in profile.json."
            case .foreignProfile, .unreadable, .profileUnknown:
                lastError = "Relaunch needs this profile's own proxy: \(statusLine)"
            }
            return
        }
        let result = await Control.relaunch(bundleId: bundleId)
        lastError = result.failure
    }
}
