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
    enum HealthRead: Sendable {
        case up(Health)
        case down
        case unreadable(String)
    }

    /// What a rules read found. Three claims, kept apart for the same reason `HealthRead`'s are:
    /// `.unsupported` says this engine has no rules route at all and needs updating, `.unavailable`
    /// says the read failed and carries what the server or the loader said, and only `.ok` is a
    /// snapshot. There is deliberately no fourth case that means "no rules" — an empty list is a
    /// scenario with no rules, and a refusal must never arrive spelled that way.
    enum RulesRead: Sendable {
        case ok(RulesSnapshot)
        case unsupported
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

    private func get<T: Decodable>(_ path: String, as type: T.Type) async -> T? {
        guard let request = request(for: path) else { return nil }
        do {
            let (data, _) = try await session.data(for: request)
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            return nil
        }
    }

    // The secondary reads stay silent on failure on purpose: they run on a timer, the status line
    // already says what is wrong with the proxy, and raising every poll failure would make the
    // menu shout twice a second while the proxy is simply not running. Health is not among them —
    // it is the reading every other one is judged by, so it reports how it failed.

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

    func scenarios() async -> ScenarioList? { await get("/__mock__/scenarios", as: ScenarioList.self) }

    func recent() async -> [RecentEntry] { await get("/__mock__/recent", as: [RecentEntry].self) ?? [] }

    /// The rules of the active scenario. Not built on `get`, deliberately: that helper collapses
    /// every failure into nil, and the window's caller would render a 409 from another profile's
    /// proxy as a scenario with no rules — the rules window's version of the bug this whole file is
    /// about. See testAScopingRefusalIsUnavailableRatherThanAnEmptyRuleList.
    func rules() async -> RulesRead {
        guard let request = request(for: "/__mock__/rules") else {
            return .unavailable("could not build a control-API URL from '\(base)'")
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            return .unavailable(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            return .unavailable("the control API answered with something other than HTTP")
        }
        // The one status this route cannot mean anything else by: it never reports a missing
        // scenario, so a 404 is the route itself not being there — an engine older than this view.
        guard http.statusCode != 404 else { return .unsupported }
        guard (200..<300).contains(http.statusCode) else {
            return .unavailable(Self.message(status: http.statusCode, body: data))
        }
        do {
            return .ok(try JSONDecoder().decode(RulesSnapshot.self, from: data))
        } catch {
            return .unavailable("the control API's rules snapshot could not be read: \(error.localizedDescription)")
        }
    }

    func activate(_ name: String) async throws {
        guard var request = request(for: "/__mock__/scenarios/active", method: "PUT") else {
            throw ClientError.transport("could not build a control-API URL from '\(base)'")
        }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["name": name])
        try await send(request)
    }

    /// Start a fresh run: rewind every sequence cursor and clear every answer count. No body, which
    /// the control API reads as "all rules".
    func reset() async throws {
        guard var request = request(for: "/__mock__/reset", method: "POST") else {
            throw ClientError.transport("could not build a control-API URL from '\(base)'")
        }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        try await send(request)
    }

    /// The one place a write's answer is judged, so a second write cannot be added that discards
    /// it — which is exactly what `activate` used to do.
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
