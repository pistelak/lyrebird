import XCTest

@testable import Lyrebird

@MainActor
final class ClearRecentTests: XCTestCase {
    override func setUpWithError() throws {
        try super.setUpWithError()
        try TestDefaults.install()
    }

    override func tearDown() {
        StubURLProtocol.reset()
        TestDefaults.restore()
        super.tearDown()
    }

    func testTrafficSelectionSurvivesReorderingWithoutRetargetingAReusedEventID() {
        let first = RecentEntry(
            id: "evt-1", time: "2026-01-01T12:00:00Z", method: "GET", path: "/api/orders", status: 200)
        let newer = RecentEntry(
            id: "evt-2", time: "2026-01-01T12:00:01Z", method: "GET", path: "/api/orders", status: 500)
        let restarted = RecentEntry(
            id: "evt-1", time: "2026-01-01T12:01:00Z", method: "GET", path: "/api/orders", status: 404)
        XCTAssertEqual([newer, first].first { $0.selectionKey == first.selectionKey }?.status, 200)
        XCTAssertNotEqual(first.selectionKey, restarted.selectionKey)
        XCTAssertNil(RecentEntry(method: "GET", path: "/api/orders", status: 200).selectionKey)
    }

    func testTrafficRowIdentitySurvivesNewRequestsAndKeepsLegacyRowsDistinct() {
        let first = RecentEntry(
            id: "evt-1", time: "2026-01-01T12:00:00Z", method: "GET", path: "/api/orders", status: 200)
        let newer = RecentEntry(
            id: "evt-2", time: "2026-01-01T12:00:01Z", method: "GET", path: "/api/orders", status: 500)
        let legacy = RecentEntry(method: "GET", path: "/api/orders", status: 200)
        let before = RecentTrafficItem.items([first])
        let after = RecentTrafficItem.items([newer, first, legacy, legacy])
        XCTAssertEqual(before[0].id, after[1].id)
        XCTAssertEqual(Set(after.map(\.id)).count, after.count)
    }

    func testScenarioReadRejectsAnErrorEvenWhenTheBodyLooksLikeAList() async {
        StubURLProtocol.install { request in
            let (_, body) = Stub.read(request)
            return (Stub.response(request, 500), body)
        }
        let client = MockClient(base: Stub.base, session: StubURLProtocol.session())
        let list = await client.scenarios()
        XCTAssertNil(list)
    }

    func testClearUsesScopedDeleteAndRefreshesTraffic() async {
        StubURLProtocol.install { request in Stub.read(request) }
        let model = AppModel(
            client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
            autoStart: false, expectedFingerprint: "abc123")
        await model.clearRecent()
        let request = StubURLProtocol.requests.first
        XCTAssertEqual(request?.httpMethod, "DELETE")
        XCTAssertEqual(request?.url?.path, "/__mock__/recent")
        XCTAssertEqual(request?.value(forHTTPHeaderField: MockClient.profileHeader), "abc123")
        XCTAssertNil(model.lastError)
        XCTAssertFalse(model.busy)
        XCTAssertTrue(
            StubURLProtocol.requests.contains {
                $0.httpMethod == "GET" && $0.url?.path == "/__mock__/recent"
            })
    }

    func testRefusedClearShowsError() async {
        StubURLProtocol.install { request in
            if request.httpMethod == "DELETE" {
                return (Stub.response(request, 409), Data(#"{"error":"profile_mismatch"}"#.utf8))
            }
            return Stub.read(request)
        }
        let model = AppModel(
            client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
            autoStart: false, expectedFingerprint: "abc123")
        await model.clearRecent()
        XCTAssertTrue(model.lastError?.contains("clear recent traffic") == true)
        XCTAssertTrue(model.lastError?.contains("profile_mismatch") == true)
        XCTAssertFalse(model.busy)
    }

    func testClearWithoutProfileDoesNotSendRequest() async {
        let model = AppModel(
            client: MockClient(base: Stub.base, session: StubURLProtocol.session()), autoStart: false)
        await model.clearRecent()
        XCTAssertNotNil(model.lastError)
        XCTAssertTrue(StubURLProtocol.requests.isEmpty)
    }
}
