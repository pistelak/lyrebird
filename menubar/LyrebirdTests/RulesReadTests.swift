import XCTest

@testable import Lyrebird

/// The rules read is the one that most wants to be written as `get(...) ?? []`, and that is exactly
/// the shape this file refuses: an empty rule list is a scenario with no rules, and a refusal, a
/// timeout or an engine with no such route must never arrive spelled that way. The model half pins
/// the other cost — a snapshot of every rule and every saved body, polled twice a second for a
/// window nobody has opened.
///
/// One class, like `ActivateTests` and `ProfileScopingTests`: `StubURLProtocol`'s handler is
/// process-wide.
@MainActor
final class RulesReadTests: XCTestCase {

    override func setUpWithError() throws {
        try super.setUpWithError()
        // Never `UserDefaults.standard`: these tests run hosted inside Lyrebird.app, so that is the
        // user's own Settings — see TestDefaults.
        try TestDefaults.install()
    }

    override func tearDown() {
        StubURLProtocol.reset()
        TestDefaults.restore()
        super.tearDown()
    }

    private func makeClient() -> MockClient {
        MockClient(base: Stub.base, profile: RulesFixture.ours, session: StubURLProtocol.session())
    }

    private func makeModel() -> AppModel {
        AppModel(
            client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
            autoStart: false, expectedFingerprint: RulesFixture.ours)
    }

    private var requestedPaths: [String] {
        StubURLProtocol.requests.compactMap { $0.url?.path }
    }

    // MARK: - The client

    func testASuccessfulReadDecodesTheSnapshotAndNamesTheProfileItAskedAs() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }

        guard case .ok(let snapshot) = await makeClient().rules() else {
            return XCTFail("a well-formed snapshot read as something other than ok")
        }

        XCTAssertEqual(snapshot.scenario, "orders-outage")
        XCTAssertEqual(snapshot.rules.count, 3)
        XCTAssertEqual(
            StubURLProtocol.requests.first?.value(forHTTPHeaderField: MockClient.profileHeader),
            RulesFixture.ours,
            "an unscoped read is answered by whichever profile holds the port")
    }

    func testAScopingRefusalIsUnavailableCarryingBothProfilesRatherThanAnEmptyRuleList() async {
        // The failure this whole file guards: a 409 collapsed into nil and then into `[]` renders
        // as "this scenario has no rules", which is a claim about someone else's proxy.
        StubURLProtocol.install { request in
            (
                Stub.response(request, 409),
                Data(
                    (#"{"error":"profile_mismatch","running":"\#(RulesFixture.theirs)","#
                        + #""requested":"\#(RulesFixture.ours)"}"#).utf8)
            )
        }

        guard case .unavailable(let reason) = await makeClient().rules() else {
            return XCTFail("a refusal was accepted as an answer about this profile's rules")
        }
        XCTAssertTrue(reason.contains(RulesFixture.theirs), "which proxy answered: \(reason)")
        XCTAssertTrue(reason.contains(RulesFixture.ours), "and which profile asked: \(reason)")
    }

    func testAnEngineWithNoRulesRouteIsUnsupportedRatherThanUnavailable() async {
        // The route never reports a missing scenario, so its 404 can only be the route itself not
        // being there — and "update the engine" is a different thing to do from "the read failed".
        StubURLProtocol.install { request in (Stub.response(request, 404), Data("404: Not Found".utf8)) }

        guard case .unsupported = await makeClient().rules() else {
            return XCTFail("an engine that predates this view was reported as something else")
        }
    }

    func testAProxyThatIsNotListeningIsUnavailableRatherThanAScenarioWithNoRules() async {
        StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }

        guard case .unavailable(let reason) = await makeClient().rules() else {
            return XCTFail("a refused connection says nothing about how many rules a scenario has")
        }
        XCTAssertFalse(reason.isEmpty, "an unavailable read with no reason explains nothing")
    }

    func testABodyThatIsNotASnapshotIsUnavailableAndSaysThatIsWhatWentWrong() async {
        for body in ["{}", "[]", "not json at all", #"{"scenario":"baseline"}"#] {
            StubURLProtocol.install { request in (Stub.response(request, 200), Data(body.utf8)) }

            guard case .unavailable = await makeClient().rules() else {
                return XCTFail("'\(body)' was accepted as a rules snapshot")
            }
        }
    }

    // MARK: - Reset run

    func testResetSendsThePostTheControlApiExpectsScopedToThisProfile() async throws {
        StubURLProtocol.install { request in
            (Stub.response(request, 200), Data(#"{"scenario":"orders-outage","reset":{"ovr_orders":"r8"}}"#.utf8))
        }

        try await makeClient().reset()

        let request = try XCTUnwrap(StubURLProtocol.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.path, "/__mock__/reset")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(request.value(forHTTPHeaderField: MockClient.profileHeader), RulesFixture.ours)
    }

    func testAResetTheProxyRefusedIsReportedInsteadOfLookingLikeAFreshRun() async {
        // A "Reset run" that returned quietly after a refusal leaves every count and cursor on
        // screen belonging to the run the operator believes they ended.
        StubURLProtocol.install { request in
            guard request.httpMethod == "POST" else { return RulesFixture.serve(request) }
            return (Stub.response(request, 409), Data(#"{"error":"profile_mismatch"}"#.utf8))
        }
        let model = makeModel()

        await model.resetRun()

        XCTAssertTrue(model.lastError?.contains("profile_mismatch") == true, model.lastError ?? "nil")
        XCTAssertFalse(model.busy, "a failure that leaves busy set locks every other action")
    }

    func testAWriteRefusesWhileTheProfileInSettingsHasMovedOn() async {
        // The Settings sheet is open and the profile path has been typed into, so nothing has
        // re-discovered yet and the fingerprint on hand names the profile configured a moment ago.
        // `refresh` has refused to read under it since profile scoping went in; the writes only
        // checked for nil, so Reset run posted to the old profile's proxy and reported success.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()
        Config.defaults.set("/tmp/another-profile", forKey: Config.profilePathKey)

        await model.resetRun()

        XCTAssertTrue(StubURLProtocol.requests.isEmpty, "the reset went out under the previous profile")
        XCTAssertTrue(model.lastError?.contains("reset run") == true, model.lastError ?? "nil")
        XCTAssertTrue(model.lastError?.contains("Settings") == true, model.lastError ?? "nil")
        XCTAssertFalse(model.busy)
    }

    func testActivateRefusesUnderAStaleFingerprintTheSameWayResetDoes() async {
        // The same gap, in the write that was there first: both are scoped by the same header and
        // both used to check only that it existed.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()
        Config.defaults.set("/tmp/another-profile", forKey: Config.profilePathKey)

        await model.activate("orders-outage")

        XCTAssertTrue(StubURLProtocol.requests.isEmpty, "the PUT switched the previous profile's scenario")
        XCTAssertTrue(model.lastError?.contains("orders-outage") == true, model.lastError ?? "nil")
    }

    // MARK: - Whose reading the window may describe

    func testAForeignProxysScenarioAndPortAreNotShownAsThoughTheyWereOurs() async {
        // The header used to read `model.health`, which is whatever answered — so a foreign proxy's
        // scenario, port and "intercepting" were printed directly above the line saying that proxy
        // is not this one's.
        StubURLProtocol.install { request in RulesFixture.serve(request, fingerprint: RulesFixture.theirs) }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.status, .foreignProfile(running: RulesFixture.theirs))
        XCTAssertNotNil(model.health, "the reading itself is kept — that is how the proxy is named")
        XCTAssertNil(model.ownHealth, "but none of its contents are this profile's to display")
    }

    func testOurOwnProxysReadingIsTheOneTheHeaderMayDescribe() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.ownHealth?.proxyPort, 8080)
        XCTAssertEqual(model.ownHealth?.activeScenario, "orders-outage")
    }

    func testAStoppedProxyDescribesNothingEvenThoughItAnswered() async {
        // `proxyUp: false` is a live control API reporting a proxy that is not running; a port and
        // a scenario read out of it would describe traffic nothing is intercepting.
        StubURLProtocol.install { request in
            request.url?.path == "/__mock__/health"
                ? (
                    Stub.response(request, 200),
                    Data(#"{"proxyUp":false,"intercepting":false,"profileFingerprint":"\#(RulesFixture.ours)"}"#.utf8)
                )
                : RulesFixture.serve(request)
        }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.status, .down)
        XCTAssertNil(model.ownHealth)
    }

    func testAResetRefusesRatherThanRewindingWhicheverProfileHoldsThePort() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = AppModel(
            client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
            autoStart: false, expectedFingerprint: nil)

        await model.resetRun()

        XCTAssertTrue(StubURLProtocol.requests.isEmpty, "the POST went out unscoped")
        XCTAssertNotNil(model.lastError)
    }

    // MARK: - When the model asks for rules at all

    func testAClosedWindowIsNeverPolledForRules() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()

        await model.refresh()

        XCTAssertFalse(requestedPaths.contains("/__mock__/rules"), "\(requestedPaths)")
        XCTAssertNil(model.rulesRead)
    }

    func testAnOpenWindowIsPolledForRulesAndGetsASnapshot() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()

        await model.rulesWindowAppeared()

        XCTAssertTrue(requestedPaths.contains("/__mock__/rules"), "\(requestedPaths)")
        guard case .ok(let snapshot) = model.rulesRead else {
            return XCTFail("the window opened and read nothing")
        }
        XCTAssertEqual(snapshot.rules.count, 3)
    }

    func testAnotherProfilesProxyIsNotAskedForItsRulesEvenWithTheWindowOpen() async {
        // The same gate the scenario list and the recent traffic are behind: another profile's
        // rules under this profile's name is the same mistake as its health.
        StubURLProtocol.install { request in RulesFixture.serve(request, fingerprint: RulesFixture.theirs) }
        let model = makeModel()

        await model.rulesWindowAppeared()

        XCTAssertEqual(model.status, .foreignProfile(running: RulesFixture.theirs))
        XCTAssertFalse(requestedPaths.contains("/__mock__/rules"), "\(requestedPaths)")
        XCTAssertNil(model.rulesRead, "nil, so the window renders from the status rather than a failure")
    }

    func testARulesReadThatBeganUnderTheOldProfileIsNotCommittedUnderTheNewOne() async throws {
        // The same race `testAReadThatBeganUnderTheOldProfileIsNotCommittedUnderTheNewOne` pins for
        // health: `@AppStorage` writes on every keystroke, so the profile changes long before the
        // Settings sheet is dismissed and nothing has bumped a generation counter yet.
        let gate = DispatchSemaphore(value: 0)
        StubURLProtocol.install { request in
            if request.url?.path == "/__mock__/rules" { gate.wait() }
            return RulesFixture.serve(request)
        }
        let model = makeModel()
        model.rulesWindowOpen = true

        let refresh = Task { await model.refresh() }
        for _ in 0..<200 where !StubURLProtocol.requests.contains(where: { $0.url?.path == "/__mock__/rules" }) {
            try await Task.sleep(for: .milliseconds(5))
        }
        Config.defaults.set("/tmp/another-profile", forKey: Config.profilePathKey)
        gate.signal()
        await refresh.value

        XCTAssertNil(model.rulesRead, "a snapshot read under the previous profile was kept")
        XCTAssertNil(model.healthRead)
    }
}
