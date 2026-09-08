import XCTest

@testable import Lyrebird

/// The rules read is the one that most wants to be written as `get(...) ?? []`, and that is exactly
/// the shape this file refuses: an empty rule list is a scenario with no rules, and a refusal, a
/// timeout or an engine with no such route must never arrive spelled that way. The model half pins
/// the other cost — a snapshot of every rule and every saved body, polled twice a second for a
/// window nobody has opened — and the browsing half pins that looking is not switching.
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

    private var rulesQueries: [String?] {
        StubURLProtocol.requests
            .filter { $0.url?.path == "/__mock__/rules" }
            .map { request in
                URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                    .queryItems?.first { $0.name == "scenario" }?.value
            }
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

    func testNamingAScenarioAsksForThatOneAndNothingElseChanges() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }

        guard case .ok(let snapshot) = await makeClient().rules(scenario: "checkout") else {
            return XCTFail("browsing a loaded scenario read as something other than ok")
        }

        XCTAssertEqual(rulesQueries, ["checkout"])
        XCTAssertEqual(snapshot.scenario, "checkout")
    }

    func testAScenarioNameGoesOutEncodedRatherThanPastedIntoTheUrl() async {
        // A scenario name is a path component in the engine, not a query-safe token. `a&b` written
        // straight into the URL is two parameters, and the read would come back describing whatever
        // `a` happens to be — a different scenario, presented under the name that was asked for.
        StubURLProtocol.install { request in RulesFixture.serve(request) }

        _ = await makeClient().rules(scenario: "orders&outage=1")

        XCTAssertEqual(rulesQueries, ["orders&outage=1"])
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
        // aiohttp's own 404 for a route that is not there: no JSON, no slug. "Update the engine" is
        // a different thing to do from "the read failed".
        StubURLProtocol.install { request in (Stub.response(request, 404), Data("404: Not Found".utf8)) }

        guard case .unsupported = await makeClient().rules() else {
            return XCTFail("an engine that predates this view was reported as something else")
        }
    }

    func testAScenarioThatWentAwayIsNotReportedAsAnEngineTooOld() async {
        // Naming a scenario gave this route a second 404: the engine's own `unknown_scenario`, for a
        // name it does not hold. Reading that as "your engine predates the rules view" sends the
        // reader to update software over a scenario somebody deleted between two polls.
        StubURLProtocol.install { request in RulesFixture.serve(request) }

        guard case .unavailable(let reason) = await makeClient().rules(scenario: "gone") else {
            return XCTFail("a scenario that is not there was reported as a missing route")
        }
        XCTAssertTrue(reason.contains("gone"), reason)
    }

    func testAProxyThatIsNotListeningIsUnavailableRatherThanAScenarioWithNoRules() async {
        StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }

        guard case .unavailable(let reason) = await makeClient().rules() else {
            return XCTFail("a refused connection says nothing about how many rules a scenario has")
        }
        XCTAssertFalse(reason.isEmpty, "an unavailable read with no reason explains nothing")
    }

    func testASnapshotMissingAFieldTheEngineAlwaysSendsIsUnavailableRatherThanDefaulted() async {
        // `rewrite.active` and `rewrite.bodyKind` arrive with every rule, so a row without one is a
        // summary this app cannot read. Defaulting either would be the drift the whole snapshot
        // exists to prevent: a rule filed as active because the field was missing, or one described
        // as answering with no body when it answers with 40 KB of JSON.
        let missing = [
            (#"{"active":true,"mode":"replace","status":200,"sequence":null}"#, "rewrite.bodyKind"),
            (#"{"mode":"replace","status":200,"bodyKind":"json","sequence":null}"#, "rewrite.active"),
        ]
        for (rewrite, field) in missing {
            let payload =
                #"{"scenario":"orders-outage","active":true,"notWhole":[],"rules":[{"#
                + #""id":"ovr_orders","rewrite":\#(rewrite),"answer":null,"sequenceState":null}]}"#
            StubURLProtocol.install { request in (Stub.response(request, 200), Data(payload.utf8)) }

            guard case .unavailable(let reason) = await makeClient().rules() else {
                return XCTFail("'\(rewrite)' was accepted as a rule this app can describe")
            }
            XCTAssertTrue(
                reason.contains("`rules[0].\(field)`"), "the reason does not name the missing field: \(reason)")
        }
    }

    func testAMissingFieldIsNamedSoAnOlderEngineIsRecognised() async {
        // Foundation's "The data couldn't be read because it is missing" names nothing, and sent a
        // person looking at the network when the proxy was simply older than the app.
        let payload =
            #"{"scenario":"orders-outage","active":true,"notWhole":[],"rules":[{"id":"ovr_orders","#
            + #""rewrite":{"mode":"replace","status":200,"bodyKind":"json","sequence":null},"answer":null,"sequenceState":null}]}"#
        StubURLProtocol.install { request in (Stub.response(request, 200), Data(payload.utf8)) }

        guard case .unavailable(let reason) = await makeClient().rules() else { return XCTFail("accepted") }
        XCTAssertTrue(reason.contains("`rules[0].rewrite.active` is missing"), reason)
        XCTAssertTrue(reason.contains("older than this app"), reason)
    }

    func testABodyThatIsNotASnapshotIsUnavailableAndSaysThatIsWhatWentWrong() async {
        for body in ["{}", "[]", "not json at all", #"{"scenario":"baseline"}"#] {
            StubURLProtocol.install { request in (Stub.response(request, 200), Data(body.utf8)) }

            guard case .unavailable = await makeClient().rules() else {
                return XCTFail("'\(body)' was accepted as a rules snapshot")
            }
        }
    }

    // MARK: - The one write the window makes

    func testActivateRefusesWhileTheProfileInSettingsHasMovedOn() async {
        // The Settings sheet is open and the profile path has been typed into, so nothing has
        // re-discovered yet and the fingerprint on hand names the profile configured a moment ago.
        // `refresh` has refused to read under it since profile scoping went in; the write only
        // checked for nil, so it landed on the old profile's proxy and reported success.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()
        Config.defaults.set("/tmp/another-profile", forKey: Config.profilePathKey)

        await model.activate("orders-outage")

        XCTAssertTrue(StubURLProtocol.requests.isEmpty, "the PUT switched the previous profile's scenario")
        XCTAssertTrue(model.lastError?.contains("orders-outage") == true, model.lastError ?? "nil")
        XCTAssertTrue(model.lastError?.contains("Settings") == true, model.lastError ?? "nil")
        XCTAssertFalse(model.busy)
    }

    func testActivateRefusesRatherThanSwitchingWhicheverProfileHoldsThePort() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = AppModel(
            client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
            autoStart: false, expectedFingerprint: nil)

        await model.activate("orders-outage")

        XCTAssertTrue(StubURLProtocol.requests.isEmpty, "the PUT went out unscoped")
        XCTAssertNotNil(model.lastError)
    }

    // MARK: - Whose reading the window may describe

    func testAForeignProxysScenarioIsNotShownAsThoughItWereOurs() async {
        // A header built from `health` — whatever answered — printed a foreign proxy's scenario and
        // "intercepting" directly above the line saying that proxy is not this one's.
        StubURLProtocol.install { request in RulesFixture.serve(request, fingerprint: RulesFixture.theirs) }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.status, .foreignProfile(running: RulesFixture.theirs))
        XCTAssertNotNil(model.health, "the reading itself is kept — that is how the proxy is named")
        XCTAssertNil(model.ownHealth, "but none of its contents are this profile's to display")
    }

    func testOurOwnProxysReadingIsTheOneTheToolbarMayDescribe() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.ownHealth?.proxyPort, 8080)
        XCTAssertEqual(model.ownHealth?.activeScenario, "orders-outage")
        XCTAssertEqual(model.ownHealth?.scenariosNotWhole?["orders-outage"]?.count, 1)
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

        await model.windowAppeared()

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

        await model.windowAppeared()

        XCTAssertEqual(model.status, .foreignProfile(running: RulesFixture.theirs))
        XCTAssertFalse(requestedPaths.contains("/__mock__/rules"), "\(requestedPaths)")
        XCTAssertNil(model.rulesRead, "nil, so the window renders from the status rather than a failure")
    }

    func testClosingTheWindowStopsTheReadAndForgetsTheSnapshot() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()

        await model.windowAppeared()
        model.windowClosed()
        await model.refresh()

        XCTAssertFalse(model.rulesWindowOpen)
        XCTAssertNil(model.rulesRead, "the window renders from the status again, not from a stale snapshot")
        XCTAssertEqual(rulesQueries.count, 1, "a shut window was polled again")
    }

    func testASecondWindowClosingIsWhatStopsTheRead() async {
        // One model backs every window in the group, and `windowClosed` used to clear a flag: an
        // `onDisappear` from one of two windows blanked the pane of the one still on screen, and
        // stopped polling underneath it.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()
        await model.windowAppeared()
        model.windowOpened()

        model.windowClosed()

        XCTAssertTrue(model.rulesWindowOpen, "the window still on screen stopped being read")
        XCTAssertNotNil(model.rulesRead, "and its pane was blanked")

        model.windowClosed()

        XCTAssertFalse(model.rulesWindowOpen)
        XCTAssertNil(model.rulesRead)
    }

    func testACloseWithNoWindowOpenCannotDriveTheCountBelowZero() async {
        // `onDisappear` can arrive for a window that never counted, and a count allowed to go
        // negative would need two opens before the next window was read at all.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()

        model.windowClosed()
        await model.windowAppeared()

        XCTAssertTrue(model.rulesWindowOpen)
        XCTAssertEqual(model.openWindows, 1)
    }

    // MARK: - Recent traffic

    func testARecentReadThatFailedIsNotAnEmptyList() async {
        // Four ways for it to fail, and `?? []` rendered every one of them as a proxy that had seen
        // no traffic — which is a claim about the proxy, made from no answer at all.
        let cases: [(String, StubURLProtocol.Handler)] = [
            (
                "a scoping refusal",
                { request in (Stub.response(request, 409), Data(#"{"error":"profile_mismatch"}"#.utf8)) }
            ),
            ("no such route", { request in (Stub.response(request, 404), Data("404: Not Found".utf8)) }),
            ("a timeout", { _ in throw URLError(.timedOut) }),
            ("a body that is not a list", { request in (Stub.response(request, 200), Data(#"{"recent":[]}"#.utf8)) }),
        ]

        for (what, handler) in cases {
            StubURLProtocol.install(handler)

            guard case .unavailable(let reason) = await makeClient().recent() else {
                return XCTFail("\(what) was accepted as a proxy that has seen nothing")
            }
            XCTAssertFalse(reason.isEmpty, "\(what): an unavailable read with no reason explains nothing")
        }
    }

    func testASuccessfulRecentReadCarriesTheRowsAndTheirEngineGivenNames() async {
        StubURLProtocol.install { request in RulesFixture.serve(request) }

        guard case .ok(let entries) = await makeClient().recent() else {
            return XCTFail("a well-formed list read as something other than ok")
        }
        XCTAssertEqual(entries.map(\.id), ["evt-2", "evt-1"])
    }

    func testTheMenuIsToldWhyTheTrafficIsMissingRatherThanThatThereIsNone() async {
        // Health answers, so the proxy is ours; only the recent read fails. That is exactly the case
        // that rendered as "no traffic yet" — a claim about the proxy made from no answer at all.
        StubURLProtocol.install { request in
            guard request.url?.path == "/__mock__/recent" else { return RulesFixture.serve(request) }
            return (Stub.response(request, 409), Data(#"{"error":"profile_mismatch"}"#.utf8))
        }
        let model = makeModel()

        await model.refresh()

        XCTAssertEqual(model.status, .intercepting)
        guard case .unavailable = model.recentRead else {
            return XCTFail("a refused recent read was kept as an answer about the traffic")
        }
        XCTAssertTrue(model.recent.isEmpty)
        XCTAssertTrue(
            model.recentPlaceholder.contains("could not be read"), model.recentPlaceholder)
    }

    // MARK: - The sidebar's last good list

    func testTheSidebarKeepsTheNewestListItWasSentAndNotTheFirst() async {
        // The cache was written only when the *active* scenario changed, so a scenario added since
        // the window opened was missing from the sidebar for as long as the reads kept failing —
        // and the reader looked at a list that had been wrong for minutes.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()
        await model.refresh()
        XCTAssertEqual(model.lastScenarios?.scenarios.count, 2)

        StubURLProtocol.install { request in
            guard request.url?.path == "/__mock__/scenarios" else { return RulesFixture.serve(request) }
            let grown = RulesFixture.scenarios.replacingOccurrences(
                of: #""name": "checkout", "overrideCount": 2, "verified": false, "notes": ""}"#,
                with: #""name": "checkout", "overrideCount": 2, "verified": false, "notes": ""},"#
                    + #"{"name": "payments", "overrideCount": 1, "verified": false, "notes": ""}"#)
            return (Stub.response(request, 200), Data(grown.utf8))
        }
        await model.refresh()

        StubURLProtocol.install { _ in throw URLError(.timedOut) }
        await model.refresh()

        XCTAssertNil(model.scenarios, "this poll brought nothing, which is what makes the list stale")
        XCTAssertEqual(
            model.lastScenarios?.scenarios.map(\.name), ["orders-outage", "checkout", "payments"],
            "the sidebar fell back to the list from two polls ago")
    }

    func testAnotherProfilesSidebarListIsNotKeptForThisOne() async {
        // A list is one profile's scenarios. Handing back the previous profile's while a read fails
        // is the same mistake as showing its health, one settings edit later. Discovery is injected
        // so the fingerprint moves without shelling out to the CLI.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = AppModel(
            client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
            autoStart: false, expectedFingerprint: RulesFixture.ours,
            discover: { RulesFixture.theirs })
        await model.refresh()
        XCTAssertNotNil(model.lastScenarios)

        await model.discoverProfile()

        XCTAssertEqual(model.expectedFingerprint, RulesFixture.theirs)
        XCTAssertNil(model.lastScenarios, "the previous profile's scenarios were still on offer")
    }

    // MARK: - Browsing

    func testBrowsingAnotherScenarioReadsItAndSwitchesNothing() async {
        // The whole point of `?scenario=`: a sidebar selection is a read. A window that activated
        // what it was asked to show would repoint the running proxy at every scenario a user
        // glanced at.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()
        await model.windowAppeared()

        await model.browse("checkout")

        guard case .ok(let snapshot) = model.rulesRead else { return XCTFail("browsing read nothing") }
        XCTAssertEqual(snapshot.scenario, "checkout")
        XCTAssertEqual(rulesQueries, [nil, "checkout"])
        XCTAssertFalse(
            StubURLProtocol.requests.contains { $0.httpMethod == "PUT" },
            "browsing activated the scenario it was only asked to show")
    }

    func testSettlingOnTheScenarioAlreadyOnScreenDoesNotBlankIt() async throws {
        // The window opens with no sidebar selection, so the first read is the parameterless one;
        // the sidebar then settles on the scenario that read named. Dropping the snapshot there put
        // "Reading the rules…" on screen for a poll every time the window was opened — which is why
        // this is asserted while the next read is still in flight, the only moment it shows.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()
        await model.windowAppeared()
        let gate = DispatchSemaphore(value: 0)
        StubURLProtocol.install { request in
            if request.url?.path == "/__mock__/rules" { gate.wait() }
            return RulesFixture.serve(request)
        }

        let browse = Task { await model.browse("orders-outage") }
        for _ in 0..<200 where !StubURLProtocol.requests.contains(where: { $0.url?.path == "/__mock__/rules" }) {
            try await Task.sleep(for: .milliseconds(5))
        }

        guard case .ok(let snapshot) = model.rulesRead else {
            gate.signal()
            await browse.value
            return XCTFail("the snapshot of the very scenario being browsed was thrown away")
        }
        XCTAssertEqual(snapshot.scenario, "orders-outage")
        gate.signal()
        await browse.value
    }

    func testMovingToAnotherScenarioBlanksTheOneOnScreenWhileTheReadIsInFlight() async throws {
        // The other half of the same decision, and the one that must not be lost to it: a snapshot
        // of a different scenario left up during the read is that scenario's rules under this
        // scenario's header.
        StubURLProtocol.install { request in RulesFixture.serve(request) }
        let model = makeModel()
        await model.windowAppeared()
        let gate = DispatchSemaphore(value: 0)
        StubURLProtocol.install { request in
            if request.url?.path == "/__mock__/rules" { gate.wait() }
            return RulesFixture.serve(request)
        }

        let browse = Task { await model.browse("checkout") }
        for _ in 0..<200 where !StubURLProtocol.requests.contains(where: { $0.url?.path == "/__mock__/rules" }) {
            try await Task.sleep(for: .milliseconds(5))
        }
        let duringTheRead = model.rulesRead

        gate.signal()
        await browse.value

        XCTAssertNil(duringTheRead, "orders-outage's rules were on screen under checkout's name")
    }

    func testASnapshotThatArrivesAfterTheWindowMovesOnIsDropped() async throws {
        // A `client.rules()` already in flight when the sidebar moves commits afterwards, and
        // committing it puts the scenario that was being read under the name of the one that is.
        // Neither of the other generation counters catches this: nothing about the poll or the
        // profile changed, only what the window is looking at.
        let gate = DispatchSemaphore(value: 0)
        StubURLProtocol.install { request in
            if request.url?.path == "/__mock__/rules" { gate.wait() }
            return RulesFixture.serve(request)
        }
        let model = makeModel()
        model.windowOpened()

        let refresh = Task { await model.refresh() }
        for _ in 0..<200 where !StubURLProtocol.requests.contains(where: { $0.url?.path == "/__mock__/rules" }) {
            try await Task.sleep(for: .milliseconds(5))
        }
        model.windowClosed()
        gate.signal()
        await refresh.value

        XCTAssertNil(model.rulesRead, "a snapshot for a window nobody has open was committed")
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
        model.windowOpened()

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
