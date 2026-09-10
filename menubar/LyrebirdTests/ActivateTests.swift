import Foundation
import Testing

@testable import Lyrebird

/// Activating a scenario is the one menu action that used to return the same way whether the
/// engine accepted it or refused it: the client discarded the result of the PUT, so a scenario
/// deleted between the last refresh and the click moved nothing and explained nothing. These pin
/// each way the write can fail to a message the menu can show.
///
/// The models below are given a profile fingerprint because a menu that does not know which
/// profile it is configured for sends no control requests at all — that is `ProfileScopingTests`'
/// subject, not this file's.
extension AppTests {
    @MainActor
    struct ActivateTests {
        /// Fails the test unless `activate` threw an HTTP error, and hands back its message.
        private func messageFromFailedActivate(
            _ client: MockClient,
            _ name: String = "orders-outage",
            expecting status: Int,
            sourceLocation: SourceLocation = SourceLocation(
                fileID: #fileID, filePath: #filePath, line: #line, column: #column)
        ) async throws -> String? {
            let error = try #require(
                await #expect(throws: MockClient.ClientError.self, sourceLocation: sourceLocation) {
                    try await client.activate(name)
                }, sourceLocation: sourceLocation)
            guard case .http(let actualStatus, let message) = error else {
                Issue.record("expected an HTTP error, got \(error)", sourceLocation: sourceLocation)
                return nil
            }
            #expect(actualStatus == status, sourceLocation: sourceLocation)
            return message
        }

        // MARK: - Client

        @Test
        func anAcceptedActivateSendsThePUTTheControlAPIExpectsAndDoesNotThrow() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    (
                        Stub.response(request, 200),
                        Data(#"{"active":"orders-outage","previous":{"name":"baseline"}}"#.utf8)
                    )
                }

                try await Stub.makeClient().activate("orders-outage")

                let request = try #require(StubURLProtocol.requests.first)
                #expect(request.httpMethod == "PUT")
                #expect(request.url?.path == "/__mock__/scenarios/active")
                #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
                let body =
                    try JSONSerialization.jsonObject(with: StubURLProtocol.body(of: request)) as? [String: String]
                #expect(body == ["name": "orders-outage"])
            }
        }

        @Test
        func aScenarioThatNoLongerExistsSurfacesTheServersSlugInsteadOfReturningQuietly()
            async throws
        {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    (Stub.response(request, 404), Data(#"{"error":"unknown_scenario","name":"orders-outage"}"#.utf8))
                }

                let message = try await messageFromFailedActivate(Stub.makeClient(), expecting: 404)

                #expect(message == "unknown_scenario")
            }
        }

        @Test
        func aDetailBeatsTheErrorSlugSoTheMessageSaysWhatToDoAboutIt() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    (
                        Stub.response(request, 400),
                        Data(#"{"error":"invalid_payload","detail":"body must be a JSON object"}"#.utf8)
                    )
                }

                let message = try await messageFromFailedActivate(Stub.makeClient(), expecting: 400)

                #expect(message == "body must be a JSON object")
            }
        }

        @Test
        func anEmptyDetailFallsThroughToTheSlugRatherThanReportingNothingAtAll() async throws {
            try await withAppTestEnvironment {
                // Python's `detail or error` skips "" as well as a missing key; matching it here keeps the
                // menu from showing "activate 'orders-outage': ".
                StubURLProtocol.install { request in
                    (Stub.response(request, 409), Data(#"{"error":"profile_mismatch","detail":""}"#.utf8))
                }

                let message = try await messageFromFailedActivate(Stub.makeClient(), expecting: 409)

                #expect(message == "profile_mismatch")
            }
        }

        @Test
        func aBodyWhoseFieldsAreAllEmptyStillReportsTheStatus() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    (Stub.response(request, 421), Data(#"{"error":"","detail":""}"#.utf8))
                }

                let message = try await messageFromFailedActivate(Stub.makeClient(), expecting: 421)

                #expect(message != nil)
                #expect(message?.hasPrefix("HTTP 421") == true, "got \(message ?? "nil")")
            }
        }

        @Test
        func aNonJSONErrorBodyReportsTheStatusRatherThanFailingToDecode() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    (Stub.response(request, 500), Data("<html>gateway barfed</html>".utf8))
                }

                let message = try await messageFromFailedActivate(Stub.makeClient(), expecting: 500)

                #expect(message != nil)
                #expect(message?.hasPrefix("HTTP 500") == true, "got \(message ?? "nil")")
            }
        }

        @Test
        func aProxyThatIsNotListeningIsReportedAsTransportRatherThanSwallowed() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }

                let error = try #require(
                    await #expect(throws: MockClient.ClientError.self) {
                        try await Stub.makeClient().activate("orders-outage")
                    })
                guard case .transport(let message) = error else {
                    Issue.record("expected a transport error, got \(error)")
                    return
                }
                #expect(!message.isEmpty, "a transport failure with no message explains nothing")
            }
        }

        // MARK: - Model

        @Test
        func aFailedActivateNamesTheScenarioAndTheServersExplanationInLastError() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    guard request.httpMethod == "PUT" else { return Stub.read(request) }
                    return (
                        Stub.response(request, 404),
                        Data(
                            (#"{"error":"unknown_scenario","detail":"no scenario named 'orders-outage'"#
                                + #" — it was deleted"}"#).utf8)
                    )
                }
                let model = makeModel(expecting: "a1b2c3")

                await model.activate("orders-outage")

                let lastError = model.lastError ?? ""
                #expect(
                    lastError.contains("orders-outage"),
                    "the menu lists several scenarios; the message must say which one: \(lastError)")
                // The detail, not the slug: the slug says what kind of thing went wrong, the detail says
                // what to do about it, and the model must pass the more useful half through.
                #expect(lastError.contains("it was deleted"), "\(lastError)")
            }
        }

        @Test
        func aSucceedingActivateClearsTheErrorLeftByTheOneBefore() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    guard request.httpMethod == "PUT" else { return Stub.read(request) }
                    return (Stub.response(request, 404), Data(#"{"error":"unknown_scenario"}"#.utf8))
                }
                let model = makeModel(expecting: "a1b2c3")
                await model.activate("orders-outage")
                #expect(model.lastError != nil)

                StubURLProtocol.install { request in
                    guard request.httpMethod == "PUT" else { return Stub.read(request) }
                    return (Stub.response(request, 200), Data(#"{"active":"baseline"}"#.utf8))
                }
                await model.activate("baseline")

                #expect(model.lastError == nil, "a stale error outlives the failure it describes")
            }
        }

        @Test
        func aFailedActivateStillReleasesBusyAndRefreshesTheStaleList() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    guard request.httpMethod == "PUT" else { return Stub.read(request) }
                    return (Stub.response(request, 404), Data(#"{"error":"unknown_scenario"}"#.utf8))
                }
                let model = makeModel(expecting: "a1b2c3")

                await model.activate("orders-outage")

                #expect(!model.busy, "a failure that leaves busy set locks every other menu action")
                let paths = StubURLProtocol.requests.map { ($0.httpMethod ?? "") + " " + ($0.url?.path ?? "") }
                let put = paths.firstIndex(of: "PUT /__mock__/scenarios/active")
                let health = paths.firstIndex(of: "GET /__mock__/health")
                #expect(put != nil)
                #expect(health != nil, "the list that produced the 404 was never re-read: \(paths)")
                if let put, let health {
                    #expect(health > put, "the refresh must follow the failed write: \(paths)")
                }
            }
        }
    }
}
