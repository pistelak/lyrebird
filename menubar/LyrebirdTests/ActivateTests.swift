import XCTest

@testable import Lyrebird

/// Activating a session is the one menu action that used to return the same way whether the
/// engine accepted it or refused it: the client discarded the result of the PUT, so a session
/// deleted between the last refresh and the click moved nothing and explained nothing. These pin
/// each way the write can fail to a message the menu can show.
///
/// One class on purpose: `StubURLProtocol`'s handler is process-wide, and XCTest runs classes,
/// not methods, in parallel.
///
/// The models below are given a profile fingerprint because a menu that does not know which
/// profile it is configured for sends no control requests at all — that is `ProfileScopingTests`'
/// subject, not this file's.
@MainActor
final class ActivateTests: XCTestCase {

    override func tearDown() {
        StubURLProtocol.reset()
        super.tearDown()
    }

    private func makeClient() -> MockClient {
        MockClient(base: Stub.base, session: StubURLProtocol.session())
    }

    /// Fails the test unless `activate` threw an HTTP error, and hands back its message.
    private func messageFromFailedActivate(_ client: MockClient,
                                           _ name: String = "orders-outage",
                                           expecting status: Int,
                                           file: StaticString = #filePath,
                                           line: UInt = #line) async -> String? {
        do {
            try await client.activate(name)
            XCTFail("activate returned normally on HTTP \(status)", file: file, line: line)
            return nil
        } catch let error as MockClient.ClientError {
            guard case .http(let actualStatus, let message) = error else {
                XCTFail("expected an HTTP error, got \(error)", file: file, line: line)
                return nil
            }
            XCTAssertEqual(actualStatus, status, file: file, line: line)
            return message
        } catch {
            XCTFail("expected a ClientError, got \(error)", file: file, line: line)
            return nil
        }
    }

    // MARK: - Client

    func testAnAcceptedActivateSendsThePutTheControlApiExpectsAndDoesNotThrow() async throws {
        StubURLProtocol.install { request in
            (Stub.response(request, 200),
             Data(#"{"active":"orders-outage","previous":{"name":"baseline"}}"#.utf8))
        }

        try await makeClient().activate("orders-outage")

        let request = try XCTUnwrap(StubURLProtocol.requests.first)
        XCTAssertEqual(request.httpMethod, "PUT")
        XCTAssertEqual(request.url?.path, "/__mock__/sessions/active")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let body = try JSONSerialization.jsonObject(with: StubURLProtocol.body(of: request)) as? [String: String]
        XCTAssertEqual(body, ["name": "orders-outage"])
    }

    func testASessionThatNoLongerExistsSurfacesTheServersSlugInsteadOfReturningQuietly() async {
        StubURLProtocol.install { request in
            (Stub.response(request, 404), Data(#"{"error":"unknown_session","name":"orders-outage"}"#.utf8))
        }

        let message = await messageFromFailedActivate(makeClient(), expecting: 404)

        XCTAssertEqual(message, "unknown_session")
    }

    func testADetailBeatsTheErrorSlugSoTheMessageSaysWhatToDoAboutIt() async {
        StubURLProtocol.install { request in
            (Stub.response(request, 400),
             Data(#"{"error":"invalid_payload","detail":"body must be a JSON object"}"#.utf8))
        }

        let message = await messageFromFailedActivate(makeClient(), expecting: 400)

        XCTAssertEqual(message, "body must be a JSON object")
    }

    func testAnEmptyDetailFallsThroughToTheSlugRatherThanReportingNothingAtAll() async {
        // Python's `detail or error` skips "" as well as a missing key; matching it here keeps the
        // menu from showing "activate 'orders-outage': ".
        StubURLProtocol.install { request in
            (Stub.response(request, 409), Data(#"{"error":"profile_mismatch","detail":""}"#.utf8))
        }

        let message = await messageFromFailedActivate(makeClient(), expecting: 409)

        XCTAssertEqual(message, "profile_mismatch")
    }

    func testABodyWhoseFieldsAreAllEmptyStillReportsTheStatus() async {
        StubURLProtocol.install { request in
            (Stub.response(request, 421), Data(#"{"error":"","detail":""}"#.utf8))
        }

        let message = await messageFromFailedActivate(makeClient(), expecting: 421)

        XCTAssertNotNil(message)
        XCTAssertTrue(message?.hasPrefix("HTTP 421") == true, "got \(message ?? "nil")")
    }

    func testANonJsonErrorBodyReportsTheStatusRatherThanFailingToDecode() async {
        StubURLProtocol.install { request in
            (Stub.response(request, 500), Data("<html>gateway barfed</html>".utf8))
        }

        let message = await messageFromFailedActivate(makeClient(), expecting: 500)

        XCTAssertNotNil(message)
        XCTAssertTrue(message?.hasPrefix("HTTP 500") == true, "got \(message ?? "nil")")
    }

    func testAProxyThatIsNotListeningIsReportedAsTransportRatherThanSwallowed() async {
        StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }

        do {
            try await makeClient().activate("orders-outage")
            XCTFail("activate returned normally although the connection failed")
        } catch let error as MockClient.ClientError {
            guard case .transport(let message) = error else {
                return XCTFail("expected a transport error, got \(error)")
            }
            XCTAssertFalse(message.isEmpty, "a transport failure with no message explains nothing")
        } catch {
            XCTFail("expected a ClientError, got \(error)")
        }
    }

    // MARK: - Model

    func testAFailedActivateNamesTheSessionAndTheServersExplanationInLastError() async {
        StubURLProtocol.install { request in
            guard request.httpMethod == "PUT" else { return Stub.read(request) }
            return (Stub.response(request, 404),
                    Data(#"{"error":"unknown_session","detail":"no session named 'orders-outage' — it was deleted"}"#.utf8))
        }
        let model = AppModel(client: makeClient(), autoStart: false, expectedFingerprint: "a1b2c3")

        await model.activate("orders-outage")

        let lastError = model.lastError ?? ""
        XCTAssertTrue(lastError.contains("orders-outage"),
                      "the menu lists several sessions; the message must say which one: \(lastError)")
        // The detail, not the slug: the slug says what kind of thing went wrong, the detail says
        // what to do about it, and the model must pass the more useful half through.
        XCTAssertTrue(lastError.contains("it was deleted"), lastError)
    }

    func testASucceedingActivateClearsTheErrorLeftByTheOneBefore() async {
        StubURLProtocol.install { request in
            guard request.httpMethod == "PUT" else { return Stub.read(request) }
            return (Stub.response(request, 404), Data(#"{"error":"unknown_session"}"#.utf8))
        }
        let model = AppModel(client: makeClient(), autoStart: false, expectedFingerprint: "a1b2c3")
        await model.activate("orders-outage")
        XCTAssertNotNil(model.lastError)

        StubURLProtocol.install { request in
            guard request.httpMethod == "PUT" else { return Stub.read(request) }
            return (Stub.response(request, 200), Data(#"{"active":"baseline"}"#.utf8))
        }
        await model.activate("baseline")

        XCTAssertNil(model.lastError, "a stale error outlives the failure it describes")
    }

    func testAFailedActivateStillReleasesBusyAndRefreshesTheStaleList() async {
        StubURLProtocol.install { request in
            guard request.httpMethod == "PUT" else { return Stub.read(request) }
            return (Stub.response(request, 404), Data(#"{"error":"unknown_session"}"#.utf8))
        }
        let model = AppModel(client: makeClient(), autoStart: false, expectedFingerprint: "a1b2c3")

        await model.activate("orders-outage")

        XCTAssertFalse(model.busy, "a failure that leaves busy set locks every other menu action")
        let paths = StubURLProtocol.requests.map { ($0.httpMethod ?? "") + " " + ($0.url?.path ?? "") }
        let put = paths.firstIndex(of: "PUT /__mock__/sessions/active")
        let health = paths.firstIndex(of: "GET /__mock__/health")
        XCTAssertNotNil(put)
        XCTAssertNotNil(health, "the list that produced the 404 was never re-read: \(paths)")
        if let put, let health {
            XCTAssertGreaterThan(health, put, "the refresh must follow the failed write: \(paths)")
        }
    }
}
