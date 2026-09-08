import Foundation

/// Thin read/activate client over the engine's control API. Never reimplements proxy logic.
struct MockClient: Sendable {
    var base: URL
    /// The profile this client speaks for, as the engine's own fingerprint — learned from
    /// `lyrebird status --json`, never computed here. Nil means "say nothing", which the API reads
    /// as an unscoped caller and answers for whatever profile it is running; the model refuses to
    /// make such a call rather than trusting the answer.
    var profile: String?
    /// Injectable so a test can drive the client through a stub `URLProtocol`. The app never sets
    /// it; `.shared` is what ships.
    var session: URLSession = .shared

    /// The scoping header `control._guard` compares against the running profile's fingerprint.
    static let profileHeader = "X-Lyrebird-Profile"

    /// What a health read found. The three cases are three different claims: `.down` says nothing
    /// is listening, `.unreadable` says something answered and could not be understood, and only
    /// `.up` carries a reading. Collapsing the middle one into `.down` is how a proxy that is
    /// plainly there came to be rendered as "Stopped".
    enum HealthRead: Sendable, Equatable {
        case up(Health)
        case down
        case unreadable(String)
    }

    /// What a rules read found. `.unsupported` is an engine with no such route, which needs
    /// updating; `.unavailable` is a read that failed, carrying what said so. An empty snapshot is a
    /// scenario with no rules, so no failure may arrive spelled that way — see
    /// testAScopingRefusalIsUnavailableCarryingBothProfilesRatherThanAnEmptyRuleList.
    enum RulesRead: Sendable, Equatable {
        case ok(RulesSnapshot)
        case unsupported
        case unavailable(String)
    }

    /// What a recent-traffic read found, on the same rule: an empty list is a proxy that has seen no
    /// traffic — see testARecentReadThatFailedIsNotAnEmptyList.
    enum RecentRead: Sendable, Equatable {
        case ok([RecentEntry])
        case unavailable(String)
    }

    /// Why a write did not happen. Reads stay best-effort (see below), but a write that returns
    /// normally after a 404 tells the menu the scenario was activated when it was not.
    enum ClientError: LocalizedError {
        /// The request never got an HTTP answer — proxy down, wrong port, connection refused.
        case transport(String)
        case http(status: Int, message: String)

        var errorDescription: String? {
            switch self {
            case .transport(let message): return message
            case .http(_, let message): return message
            }
        }
    }

    /// The one place a request is built, so the scoping header cannot be on some calls and off
    /// others — what it guards against is silent, and a second construction site is all it would
    /// take to bring that back.
    private func request(for path: String, method: String = "GET") -> URLRequest? {
        guard let url = URL(string: path, relativeTo: base) else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let profile { request.setValue(profile, forHTTPHeaderField: Self.profileHeader) }
        return request
    }

    func health() async -> HealthRead {
        guard let request = request(for: "/__mock__/health") else {
            return .unreadable("could not build a control-API URL from '\(base)'")
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError where error.code == .cannotConnectToHost {
            // The one failure that means "nothing is listening there". Every other transport
            // error — a timeout, a connection dropped mid-answer, a URL the loader rejected —
            // leaves open whether a proxy is running, and "Stopped" would be a claim this client
            // cannot support.
            return .down
        } catch {
            return .unreadable(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            return .unreadable("the control API answered with something other than HTTP")
        }
        guard (200..<300).contains(http.statusCode) else {
            return .unreadable(Self.message(status: http.statusCode, body: data))
        }
        guard let health = try? JSONDecoder().decode(Health.self, from: data) else {
            return .unreadable("the control API's health was not the JSON object it should be")
        }
        // Every field of `Health` is optional, so an unrelated JSON object decodes into an all-nil
        // reading that would render as a proxy up and idle. `proxyUp` is the one field the engine
        // always sends; without it this is not a health response.
        guard health.proxyUp != nil else {
            return .unreadable("the control API's health did not say whether the proxy is up")
        }
        return .up(health)
    }

    func scenarios() async -> ScenarioList? {
        guard case .success(let list) = await read("/__mock__/scenarios", as: ScenarioList.self) else { return nil }
        return list
    }

    /// What a read ran into instead of a value. It reports and does not decide: a 404 means an
    /// unknown scenario on one route and an engine too old on another, so the status and the body
    /// travel back to the caller that knows which.
    private enum ReadFailure: Error {
        case transport(String)
        case http(status: Int, body: Data)
        case undecodable(String)
    }

    /// Scoped reads preserve HTTP and decoding failures for the caller.
    private func read<T: Decodable>(_ path: String, as type: T.Type) async -> Result<T, ReadFailure> {
        guard let request = request(for: path) else {
            return .failure(.transport("could not build a control-API URL from '\(base)'"))
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            return .failure(.transport(error.localizedDescription))
        }
        guard let http = response as? HTTPURLResponse else {
            return .failure(.transport("the control API answered with something other than HTTP"))
        }
        guard (200..<300).contains(http.statusCode) else {
            return .failure(.http(status: http.statusCode, body: data))
        }
        do {
            return .success(try JSONDecoder().decode(T.self, from: data))
        } catch {
            return .failure(.undecodable(Self.describe(decoding: error)))
        }
    }

    /// Names the field a decode tripped on, because Foundation's own sentence does not: "The data
    /// couldn't be read because it is missing" sent the user looking at the network when the answer
    /// was a proxy older than the app — see testAMissingFieldIsNamedSoAnOlderEngineIsRecognised.
    static func describe(decoding error: Error) -> String {
        func path(_ context: DecodingError.Context, plus key: CodingKey? = nil) -> String {
            let keys = (context.codingPath + [key].compactMap { $0 }).map {
                $0.intValue.map { "[\($0)]" } ?? $0.stringValue
            }
            return keys.isEmpty
                ? "the top level" : "`" + keys.joined(separator: ".").replacingOccurrences(of: ".[", with: "[") + "`"
        }
        switch error {
        case DecodingError.keyNotFound(let key, let context):
            return "the field \(path(context, plus: key)) is missing — the engine may be older than this app"
        case DecodingError.valueNotFound(_, let context):
            return "the field \(path(context)) is null where a value was expected"
        case DecodingError.typeMismatch(let type, let context):
            return "the field \(path(context)) is not the \(type) this app expects"
        case DecodingError.dataCorrupted(let context):
            return "the body is not the JSON this app expects at \(path(context))"
        default:
            return error.localizedDescription
        }
    }

    /// The requests the proxy has seen.
    func recent() async -> RecentRead {
        switch await read("/__mock__/recent", as: [RecentEntry].self) {
        case .success(let entries):
            return .ok(entries)
        case .failure(.transport(let reason)):
            return .unavailable(reason)
        case .failure(.http(let status, let body)):
            return .unavailable(Self.message(status: status, body: body))
        case .failure(.undecodable(let reason)):
            return .unavailable("the control API's recent traffic could not be read: \(reason)")
        }
    }

    /// One scenario's rules: the active one, or the one `scenario` names — browsing, which never
    /// switches the proxy.
    func rules(scenario: String? = nil) async -> RulesRead {
        // Through `URLComponents` rather than by concatenation: a scenario name is a path component
        // in the engine, not a query-safe token, and a `+` or a `&` in one pasted straight into the
        // URL would ask after a scenario nobody named.
        var components = URLComponents(string: "/__mock__/rules")
        if let scenario { components?.queryItems = [URLQueryItem(name: "scenario", value: scenario)] }
        guard let path = components?.string else {
            return .unavailable("could not build a control-API URL from '\(base)'")
        }
        switch await read(path, as: RulesSnapshot.self) {
        case .success(let snapshot):
            return .ok(snapshot)
        case .failure(.transport(let reason)):
            return .unavailable(reason)
        case .failure(.http(404, let body)):
            // Two 404s share this route: the engine's `unknown_scenario`, and an engine too old to
            // have the route at all. Reporting the first as the second sends the reader to update
            // software over a scenario somebody deleted — see
            // testAScenarioThatWentAwayIsNotReportedAsAnEngineTooOld.
            let slug = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
            return slug?["error"] == nil ? .unsupported : .unavailable(Self.message(status: 404, body: body))
        case .failure(.http(let status, let body)):
            return .unavailable(Self.message(status: status, body: body))
        case .failure(.undecodable(let reason)):
            return .unavailable("the control API's rules snapshot could not be read: \(reason)")
        }
    }

    func clearRecent() async throws {
        guard let request = request(for: "/__mock__/recent", method: "DELETE") else {
            throw ClientError.transport("could not build a control-API URL from '\(base)'")
        }
        try await send(request)
    }

    func activate(_ name: String) async throws {
        guard var request = request(for: "/__mock__/scenarios/active", method: "PUT") else {
            throw ClientError.transport("could not build a control-API URL from '\(base)'")
        }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["name": name])
        try await send(request)
    }

    /// The one place a write's answer is judged, so a second write cannot be added that discards it
    /// — which is exactly what `activate` used to do.
    private func send(_ request: URLRequest) async throws {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw ClientError.transport(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw ClientError.transport("the control API answered with something other than HTTP")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw ClientError.http(
                status: http.statusCode,
                message: Self.message(status: http.statusCode, body: data))
        }
    }

    /// `detail` first, then the scoping refusal, then the `error` slug, then the status — the
    /// precedence `cli._control` uses and for the same reason: the slug names the problem, the
    /// detail says what to do about it. Empty strings are skipped so a server that sends
    /// `"detail": ""` does not produce a message that explains nothing, which is exactly the
    /// silence this whole change is about.
    ///
    /// `profile_mismatch` gets a sentence of its own because it is the one refusal whose
    /// explanation arrives in fields rather than in `detail`: the two fingerprints say which proxy
    /// answered and which one was asked for, and the bare slug says neither.
    private static func message(status: Int, body: Data) -> String {
        let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
        if let detail = json?["detail"] as? String, !detail.isEmpty { return detail }
        let slug = json?["error"] as? String
        if slug == "profile_mismatch",
            let running = json?["running"] as? String, !running.isEmpty,
            let requested = json?["requested"] as? String, !requested.isEmpty
        {
            return "another profile (running \(running), asked for \(requested))"
        }
        if let slug, !slug.isEmpty { return slug }
        let reason = HTTPURLResponse.localizedString(forStatusCode: status)
        return reason.isEmpty ? "HTTP \(status)" : "HTTP \(status) \(reason)"
    }
}
