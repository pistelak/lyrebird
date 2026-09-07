import XCTest

@testable import Lyrebird

/// One proxy holds the control port, so with two profiles around the menu used to report on
/// whichever proxy answered: it sent no scoping header, dropped the fingerprint the engine offers,
/// and turned every failed read into "Stopped". A profile-A proxy therefore appeared in a menu
/// configured for profile B as "Intercepting · <A's session>", and clicking a session switched A's.
/// These pin the three readings apart — ours, someone else's, and one that could not be read at all.
///
/// One class on purpose, like `ActivateTests`: `StubURLProtocol`'s handler is process-wide.
@MainActor
final class ProfileScopingTests: XCTestCase {

    override func tearDown() {
        StubURLProtocol.reset()
        UserDefaults.standard.removeObject(forKey: Config.lyrebirdPathKey)
        UserDefaults.standard.removeObject(forKey: Config.profilePathKey)
        super.tearDown()
    }

    private func makeClient(profile: String? = Fixture.ours) -> MockClient {
        MockClient(base: Stub.base, profile: profile, session: StubURLProtocol.session())
    }

    /// A model that already knows its profile, driven by the injected client. Discovery is not
    /// wired up unless a test asks for it, so nothing here shells out.
    private func makeModel(
        expecting fingerprint: String? = Fixture.ours,
        discover: (@Sendable () async throws -> String)? = nil
    ) -> AppModel {
        AppModel(
            client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
            autoStart: false, expectedFingerprint: fingerprint, discover: discover)
    }

    private func headers(ofRequestsTo path: String) -> [String?] {
        StubURLProtocol.requests
            .filter { $0.url?.path == path }
            .map { $0.value(forHTTPHeaderField: MockClient.profileHeader) }
    }

    // MARK: - The header

    func testEveryControlCallNamesTheProfileSoAForeignProxyRefusesInsteadOfAnswering() async throws {
        // Not only the write: an unscoped GET is answered by whichever profile holds the port, and
        // that answer is what the menu renders.
        StubURLProtocol.install { request in
            request.httpMethod == "PUT"
                ? (Stub.response(request, 200), Data(#"{"active":"baseline"}"#.utf8))
                : Stub.read(request)
        }
        let client = makeClient()

        _ = await client.health()
        _ = await client.sessions()
        _ = await client.recent()
        try await client.activate("baseline")

        let sent = StubURLProtocol.requests
        XCTAssertEqual(sent.count, 4, "one request per call")
        for request in sent {
            XCTAssertEqual(
                request.value(forHTTPHeaderField: MockClient.profileHeader),
                Fixture.ours,
                "\(request.httpMethod ?? "") \(request.url?.path ?? "") went unscoped")
        }
    }

    func testAClientWithNoProfileSendsNoHeaderRatherThanAnEmptyOne() async {
        // `control._guard` reads `X-Lyrebird-Profile:` — present and empty — as a caller that named
        // nobody and refuses it. Absent is the older-CLI case it deliberately lets through.
        StubURLProtocol.install { request in Stub.read(request) }

        _ = await makeClient(profile: nil).health()

        XCTAssertEqual(headers(ofRequestsTo: "/__mock__/health"), [nil])
    }

    func testTheModelPutsItsOwnFingerprintOnTheInjectedClientsRequests() async {
        StubURLProtocol.install { request in Stub.read(request) }

        await makeModel().refresh()

        XCTAssertFalse(StubURLProtocol.requests.isEmpty, "the refresh sent nothing at all")
        for request in StubURLProtocol.requests {
            XCTAssertEqual(request.value(forHTTPHeaderField: MockClient.profileHeader), Fixture.ours)
        }
    }

    // MARK: - Reading health

    func testHealthCarriesTheRunningProfilesFingerprintThroughToTheModel() async throws {
        StubURLProtocol.install { request in
            (Stub.response(request, 200), Fixture.health(fingerprint: Fixture.ours))
        }

        guard case .up(let health) = await makeClient().health() else {
            return XCTFail("a well-formed health read as something other than up")
        }

        XCTAssertEqual(health.profileFingerprint, Fixture.ours)
        XCTAssertEqual(health.proxyUp, true)
    }

    func testNothingListeningOnThePortReadsAsDown() async {
        StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }

        guard case .down = await makeClient().health() else {
            return XCTFail("a refused connection is the one case that really is 'stopped'")
        }
    }

    func testATimeoutIsUnreadableRatherThanStoppedBecauseAProxyMayWellBeRunning() async {
        StubURLProtocol.install { _ in throw URLError(.timedOut) }

        guard case .unreadable(let reason) = await makeClient().health() else {
            return XCTFail("a timeout says nothing about whether a proxy is there")
        }
        XCTAssertFalse(reason.isEmpty, "an unreadable health with no reason explains nothing")
    }

    func testAnErrorStatusIsUnreadableAndRepeatsWhatTheServerSaid() async {
        StubURLProtocol.install { request in
            (Stub.response(request, 500), Data("<html>gateway barfed</html>".utf8))
        }

        guard case .unreadable(let reason) = await makeClient().health() else {
            return XCTFail("HTTP 500 is something answering, not nothing listening")
        }
        XCTAssertTrue(reason.contains("500"), reason)
    }

    func testABodyThatIsNotAHealthReadingIsUnreadableRatherThanAnIdleProxy() async {
        // Every field of `Health` is optional, so `{}` decodes cleanly into an all-nil reading —
        // which would render as a proxy that is up and simply not intercepting.
        for body in ["{}", #"{"intercepting":true}"#, "not json at all", "[]"] {
            StubURLProtocol.install { request in (Stub.response(request, 200), Data(body.utf8)) }

            guard case .unreadable = await makeClient().health() else {
                return XCTFail("'\(body)' was accepted as a health reading")
            }
        }
    }

    // MARK: - The 409 the header now provokes

    func testTheScopingRefusalNamesBothProfilesInsteadOfTheBareSlug() async {
        // `control._guard` sends no `detail` for this one — the two fingerprints are the whole
        // explanation, and "profile_mismatch" alone leaves the operator with no way to tell which
        // proxy answered.
        StubURLProtocol.install { request in
            (
                Stub.response(request, 409),
                Data(
                    #"{"error":"profile_mismatch","running":"\#(Fixture.theirs)","requested":"\#(Fixture.ours)"}"#.utf8)
            )
        }

        do {
            try await makeClient().activate("baseline")
            XCTFail("activate returned normally although the proxy refused it")
        } catch {
            let message = error.localizedDescription
            XCTAssertTrue(message.contains(Fixture.theirs), "which proxy answered: \(message)")
            XCTAssertTrue(message.contains(Fixture.ours), "and which profile asked: \(message)")
        }
    }

    // MARK: - What the menu then shows

    func testAProxyRunningAnotherProfileIsNamedAsSuchAndItsDataIsNotShown() async {
        StubURLProtocol.install { request in
            (Stub.response(request, 200), Fixture.health(fingerprint: Fixture.theirs))
        }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.status, .foreignProfile(running: Fixture.theirs))
        XCTAssertTrue(
            model.statusLine.contains(Fixture.theirs),
            "the line must name the proxy that answered: \(model.statusLine)")
        XCTAssertNil(model.sessions, "another profile's sessions are not this profile's to show")
        XCTAssertTrue(model.recent.isEmpty, "nor its traffic")
        XCTAssertNil(model.simBundleId, "relaunching another profile's app is not a thing to offer")
        XCTAssertTrue(model.stopsRatherThanStarts, "the remedy is to stop the proxy holding the port")
        XCTAssertEqual(
            StubURLProtocol.requests.map { $0.url?.path }, ["/__mock__/health"],
            "the secondary reads were made against a proxy that is not ours")
    }

    func testAMatchingFingerprintIsTheOrdinaryInterceptingReading() async {
        StubURLProtocol.install { request in
            request.url?.path == "/__mock__/health"
                ? (Stub.response(request, 200), Fixture.health(fingerprint: Fixture.ours))
                : Stub.read(request)
        }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.status, .intercepting)
        XCTAssertEqual(model.simBundleId, "com.example.Store")
        XCTAssertNotNil(model.sessions)
    }

    func testAnEngineTooOldToReportItsProfileIsAcceptedTheWayTheCLIAcceptsIt() async {
        // A missing fingerprint is a proxy that cannot answer the question, not one that answered
        // it differently — `cli.status` treats it the same way.
        StubURLProtocol.install { request in
            request.url?.path == "/__mock__/health"
                ? (Stub.response(request, 200), Fixture.health(fingerprint: nil))
                : Stub.read(request)
        }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.status, .intercepting)
    }

    func testAPacThatCouldNotBeReadStillRendersAsProxyUpNotIntercepting() async {
        // The engine reports `intercepting: false` with a `pacError` beside it. That is the state
        // Start repairs, and it must not be confused with either of the new ones.
        StubURLProtocol.install { request in
            request.url?.path == "/__mock__/health"
                ? (
                    Stub.response(request, 200),
                    Fixture.health(
                        fingerprint: Fixture.ours, intercepting: false,
                        extra: #","pacError":"networksetup failed""#)
                )
                : Stub.read(request)
        }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.status, .pacDisabled)
        XCTAssertEqual(model.statusLine, "Proxy up, not intercepting — press Start")
    }

    func testAProxyThatCouldNotBeReadIsNeverRenderedAsStopped() async {
        StubURLProtocol.install { request in (Stub.response(request, 500), Data("nope".utf8)) }
        let model = makeModel()

        await model.refresh()

        guard case .unreadable = model.status else {
            return XCTFail("'Stopped' is a claim about the port that nothing here supports")
        }
        XCTAssertTrue(model.statusLine.hasPrefix("Could not read the proxy's health"), model.statusLine)
        XCTAssertTrue(model.stopsRatherThanStarts, "something holds the port; Stop is what deals with it")
        XCTAssertNil(model.simBundleId)
    }

    // MARK: - Not knowing the profile

    func testAProfileTheCLICouldNotNameLeavesTheMenuSilentRatherThanUnscoped() async {
        StubURLProtocol.install { request in Stub.read(request) }
        let model = makeModel(
            expecting: nil,
            discover: { throw Control.ProfileUnknown(reason: "lyrebird not found") })

        await model.discoverProfile()
        await model.refresh()

        guard case .profileUnknown(let reason) = model.status else {
            return XCTFail("without a fingerprint every reading is somebody's, not necessarily ours")
        }
        XCTAssertEqual(reason, "lyrebird not found")
        XCTAssertTrue(model.lastError?.contains("lyrebird not found") == true, model.lastError ?? "nil")
        XCTAssertTrue(
            StubURLProtocol.requests.isEmpty,
            "an unscoped request is answered by whatever profile holds the port")
        XCTAssertNil(model.simBundleId)
        XCTAssertFalse(model.stopsRatherThanStarts)
    }

    func testActivateRefusesRatherThanSwitchingWhicheverProfileHoldsThePort() async {
        StubURLProtocol.install { request in Stub.read(request) }
        let model = makeModel(expecting: nil)

        await model.activate("orders-outage")

        XCTAssertTrue(StubURLProtocol.requests.isEmpty, "the PUT went out unscoped")
        XCTAssertTrue(model.lastError?.contains("orders-outage") == true, model.lastError ?? "nil")
        XCTAssertFalse(model.busy)
    }

    func testChangingTheProfileInSettingsDropsWhatTheOldOneRead() async {
        StubURLProtocol.install { request in
            request.url?.path == "/__mock__/health"
                ? (Stub.response(request, 200), Fixture.health(fingerprint: Fixture.ours))
                : Stub.read(request)
        }
        let fingerprint = MutableFingerprint(Fixture.ours)
        let model = AppModel(
            client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
            autoStart: false, expectedFingerprint: Fixture.ours,
            discover: { fingerprint.value })
        await model.refresh()
        XCTAssertEqual(model.status, .intercepting)
        XCTAssertNotNil(model.sessions)

        fingerprint.value = Fixture.theirs  // Settings now points at another profile
        await model.settingsChanged()

        XCTAssertEqual(
            model.status, .foreignProfile(running: Fixture.ours),
            "the proxy did not move; the profile the menu means did")
        XCTAssertNil(model.sessions, "the old profile's session list outlived the profile")
        XCTAssertTrue(model.recent.isEmpty)
    }

    func testAReadThatBeganUnderTheOldProfileIsNotCommittedUnderTheNewOne() async throws {
        // `@AppStorage` writes on every keystroke, so the profile changes long before the sheet is
        // dismissed and nothing has bumped a generation counter yet. A read already in flight is
        // still describing the profile it started under.
        let gate = DispatchSemaphore(value: 0)
        StubURLProtocol.install { request in
            if request.url?.path == "/__mock__/health" { gate.wait() }
            return Stub.read(request)
        }
        let model = makeModel()

        let refresh = Task { await model.refresh() }
        for _ in 0..<200 where StubURLProtocol.requests.isEmpty {
            try await Task.sleep(for: .milliseconds(5))
        }
        XCTAssertFalse(StubURLProtocol.requests.isEmpty, "the refresh never sent its health request")
        UserDefaults.standard.set("/tmp/another-profile", forKey: Config.profilePathKey)
        gate.signal()
        await refresh.value

        XCTAssertNil(model.healthRead, "a reading taken under the previous profile was kept")
        XCTAssertNil(model.sessions)
        XCTAssertTrue(model.recent.isEmpty)
    }

    func testAFingerprintDiscoveredUnderTheOldProfileScopesNothingAfterTheEdit() async {
        // The sheet has not been dismissed yet, so nothing has re-discovered — but the fingerprint
        // on hand answers a question about the profile that was configured a keystroke ago, and a
        // header built from it would name that one on a call meant for this one.
        StubURLProtocol.install { request in Stub.read(request) }
        let model = makeModel(expecting: nil, discover: { Fixture.ours })
        await model.discoverProfile()
        XCTAssertEqual(model.expectedFingerprint, Fixture.ours)

        UserDefaults.standard.set("/tmp/another-profile", forKey: Config.profilePathKey)
        await model.refresh()

        XCTAssertTrue(
            StubURLProtocol.requests.isEmpty,
            "the call went out scoped to the profile that is no longer configured")
        XCTAssertNil(model.healthRead)
        XCTAssertNil(model.sessions)
        XCTAssertTrue(model.recent.isEmpty)
    }

    // MARK: - Start and Stop

    func testAStartThatCouldNotRunTheLauncherSaysSoInsteadOfLookingLikeNothingHappened() async {
        // Pointed at a path that is not there, so the test cannot start a proxy on the machine
        // running it — the failure it pins is the one an operator sees with the wrong path
        // in Settings.
        UserDefaults.standard.set("/does-not-exist/lyrebird", forKey: Config.lyrebirdPathKey)
        StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
        let model = makeModel()

        await model.toggle()

        XCTAssertEqual(model.status, .down)
        XCTAssertTrue(
            model.lastError?.contains("/does-not-exist/lyrebird") == true,
            model.lastError ?? "nil")
        XCTAssertFalse(model.busy)
    }

    // MARK: - Bounding the shell-out the lookup rides on

    func testACancelledShellOutStopsWaitingForAChildThatDeclinesSigterm() async throws {
        // The discovery timeout cancels the task running the CLI, and cancellation sends SIGTERM
        // and then goes on awaiting the exit and the output drain — so a child that ignores it
        // makes the "15 second" bound as long as the child feels like. The escalation is what
        // makes the bound real, and it belongs to every CLI call, not only this one.
        let reader = ReaderHandle()
        let started = Date()
        let task = Task {
            await Control.shell(
                "/bin/sh", ["-c", #"trap "" TERM; sleep 30"#],
                readerStarted: { reader.buffer = $0 })
        }
        try await Task.sleep(for: .milliseconds(300))

        task.cancel()
        _ = await task.value

        XCTAssertLessThan(
            Date().timeIntervalSince(started), 6,
            "the call waited for a child that had been asked politely and refused")
        // And returning is not enough: the `sleep` still holds the write end for another half
        // minute, so a reader merely stopped being waited for would sit on the pipe and go on
        // filling a buffer nobody will read.
        let buffer = try XCTUnwrap(reader.buffer, "the call never started a reader")
        for _ in 0..<100 where !buffer.isFinished {
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(buffer.isFinished, "the reader was abandoned rather than shut down")
    }

    // MARK: - Where the fingerprint comes from

    func testTheFingerprintIsReadFromStatusJsonRatherThanDerivedFromAPath() throws {
        // `sha256(resolved profile dir)[:12]` is the engine's rule, and the app cannot even see
        // the directory when the profile is unset. Parsing the field is the whole of the app's
        // side of it.
        let payload = #"{"proxyUp":false,"intercepting":false,"profileFingerprint":"\#(Fixture.ours)"}"#

        XCTAssertEqual(Control.fingerprint(fromStatusJSON: Data(payload.utf8)), Fixture.ours)
    }

    func testAStatusThatExitedNonZeroStillNamesTheProfile() throws {
        // `status` exits 1 whenever this profile is not intercepting — including when another
        // profile holds the port — and prints the JSON regardless. Run through the function
        // discovery itself calls, so a later "require exit 0" would fail here rather than in the
        // one situation the menu most needs to explain.
        let payload = #"""
            {"proxyUp":true,"intercepting":false,"profileMismatch":true,
             "profileFingerprint":"\#(Fixture.ours)","runningProfileFingerprint":"\#(Fixture.theirs)"}
            """#

        let fingerprint = try Control.fingerprint(from: Control.Result(output: payload, status: 1))

        XCTAssertEqual(fingerprint, Fixture.ours)
    }

    func testAnOutputWithNoJsonAtAllIsReportedWithWhatTheCLIPrinted() {
        let result = Control.Result(output: "lyrebird: no such profile directory\n", status: 2)

        XCTAssertThrowsError(try Control.fingerprint(from: result)) { error in
            XCTAssertTrue(
                error.localizedDescription.contains("no such profile directory"),
                "the reason lives in the CLI's own output: \(error.localizedDescription)")
        }
    }

    func testTextAroundTheJsonIsToleratedBecauseTheLauncherMergesStderrIntoStdout() {
        let output =
            "warning: something on stderr\n"
            + #"{"profileFingerprint":"\#(Fixture.ours)"}"# + "\ntrailing noise\n"

        XCTAssertEqual(Control.fingerprint(fromStatusJSON: Data(output.utf8)), Fixture.ours)
    }

    func testAnOutputWithNoFingerprintYieldsNilRatherThanAGuess() {
        for output in [
            "", "Usage: lyrebird [OPTIONS]", #"{"proxyUp":true}"#,
            #"{"profileFingerprint":""}"#,
        ] {
            XCTAssertNil(
                Control.fingerprint(fromStatusJSON: Data(output.utf8)),
                "'\(output)' produced a fingerprint out of nothing")
        }
    }
}

/// Fixtures shared by the tests above. They live outside the main-actor class because the stub's
/// handler runs on `URLSession`'s own queue and has to build a body there.
private enum Fixture {
    /// Short and readable rather than realistic: the engine's are `sha256(profile dir)[:12]`, and
    /// nothing here is helped by twelve hex digits.
    static let ours = "a1b2c3"
    static let theirs = "d4e5f6"

    /// A health body with whatever the test needs said about it.
    static func health(fingerprint: String?, intercepting: Bool = true, extra: String = "") -> Data {
        let profile = fingerprint.map { #""profileFingerprint":"\#($0)","# } ?? ""
        return Data(
            (#"{"activeSession":"baseline","overrideCount":2,"proxyUp":true,"#
                + #""intercepting":\#(intercepting),\#(profile)"# + #""simBundleId":"com.example.Store"\#(extra)}"#)
                .utf8)
    }
}

/// Holds the buffer `Control.shell` hands over, so the test can look at it after the call it was
/// handed to has returned.
private final class ReaderHandle: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: Control.OutputBuffer?

    var buffer: Control.OutputBuffer? {
        get {
            lock.lock()
            defer { lock.unlock() }
            return stored
        }
        set {
            lock.lock()
            stored = newValue
            lock.unlock()
        }
    }
}

/// A fingerprint a test can change between calls. The discovery closure is `@Sendable`, so it
/// cannot capture a plain `var`.
private final class MutableFingerprint: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: String

    init(_ value: String) { stored = value }

    var value: String {
        get {
            lock.lock()
            defer { lock.unlock() }
            return stored
        }
        set {
            lock.lock()
            stored = newValue
            lock.unlock()
        }
    }
}
