import Foundation
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct ClearRecentTests {
        @Test
        func trafficSelectionSurvivesReorderingWithoutRetargetingAReusedEventID() async throws {
            try await withAppTestEnvironment {
                let first = RecentEntry(
                    id: "evt-1", time: "2026-01-01T12:00:00Z", method: "GET", path: "/api/orders", status: 200)
                let newer = RecentEntry(
                    id: "evt-2", time: "2026-01-01T12:00:01Z", method: "GET", path: "/api/orders", status: 500)
                let restarted = RecentEntry(
                    id: "evt-1", time: "2026-01-01T12:01:00Z", method: "GET", path: "/api/orders", status: 404)
                #expect([newer, first].first { $0.selectionKey == first.selectionKey }?.status == 200)
                #expect(first.selectionKey != restarted.selectionKey)
                #expect(RecentEntry(method: "GET", path: "/api/orders", status: 200).selectionKey == nil)
            }
        }

        @Test
        func trafficRowIdentitySurvivesNewRequestsAndKeepsLegacyRowsDistinct() async throws {
            try await withAppTestEnvironment {
                let first = RecentEntry(
                    id: "evt-1", time: "2026-01-01T12:00:00Z", method: "GET", path: "/api/orders", status: 200)
                let newer = RecentEntry(
                    id: "evt-2", time: "2026-01-01T12:00:01Z", method: "GET", path: "/api/orders", status: 500)
                let legacy = RecentEntry(method: "GET", path: "/api/orders", status: 200)
                let before = RecentTrafficItem.items([first])
                let after = RecentTrafficItem.items([newer, first, legacy, legacy])
                #expect(before[0].id == after[1].id)
                #expect(Set(after.map(\.id)).count == after.count)
            }
        }

        @Test
        func scenarioReadRejectsAnErrorEvenWhenTheBodyLooksLikeAList() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    let (_, body) = Stub.read(request)
                    return (Stub.response(request, 500), body)
                }
                let client = MockClient(base: Stub.base, session: StubURLProtocol.session())
                let list = await client.scenarios()
                #expect(list == nil)
            }
        }

        @Test
        func clearUsesScopedDeleteAndRefreshesTraffic() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in Stub.read(request) }
                let model = AppModel(
                    client: MockClient(base: Stub.base, session: StubURLProtocol.session()),
                    autoStart: false, expectedFingerprint: "abc123")
                await model.clearRecent()
                let request = StubURLProtocol.requests.first
                #expect(request?.httpMethod == "DELETE")
                #expect(request?.url?.path == "/__mock__/recent")
                #expect(request?.value(forHTTPHeaderField: MockClient.profileHeader) == "abc123")
                #expect(model.lastError == nil)
                #expect(!model.busy)
                #expect(
                    StubURLProtocol.requests.contains {
                        $0.httpMethod == "GET" && $0.url?.path == "/__mock__/recent"
                    })
            }
        }

        @Test
        func refusedClearShowsError() async throws {
            try await withAppTestEnvironment {
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
                #expect(model.lastError?.contains("clear recent traffic") == true)
                #expect(model.lastError?.contains("profile_mismatch") == true)
                #expect(!model.busy)
            }
        }

        @Test
        func clearWithoutProfileDoesNotSendRequest() async throws {
            try await withAppTestEnvironment {
                let model = AppModel(
                    client: MockClient(base: Stub.base, session: StubURLProtocol.session()), autoStart: false)
                await model.clearRecent()
                #expect(model.lastError != nil)
                #expect(StubURLProtocol.requests.isEmpty)
            }
        }
    }
}
