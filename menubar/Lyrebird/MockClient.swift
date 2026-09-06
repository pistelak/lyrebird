import Foundation

/// Thin read/activate client over the engine's control API. Never reimplements proxy logic.
struct MockClient: Sendable {
    var base: URL
    /// Injectable so a test can drive the client through a stub `URLProtocol`. The app never sets
    /// it; `.shared` is what ships.
    var session: URLSession = .shared

    /// Why a write did not happen. Reads stay best-effort (see below), but a write that returns
    /// normally after a 404 tells the menu the session was activated when it was not.
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

    private func get<T: Decodable>(_ path: String, as type: T.Type) async -> T? {
        guard let url = URL(string: path, relativeTo: base) else { return nil }
        do {
            let (data, _) = try await session.data(from: url)
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            return nil
        }
    }

    // The reads stay silent on failure on purpose: they run on a timer, the model already renders
    // a missing health as "Stopped", and raising every poll failure would make the menu shout
    // twice a second while the proxy is simply not running.

    func health() async -> Health? { await get("/__mock__/health", as: Health.self) }

    func sessions() async -> SessionList? { await get("/__mock__/sessions", as: SessionList.self) }

    func recent() async -> [RecentEntry] { await get("/__mock__/recent", as: [RecentEntry].self) ?? [] }

    func activate(_ name: String) async throws {
        guard let url = URL(string: "/__mock__/sessions/active", relativeTo: base) else {
            throw ClientError.transport("could not build a control-API URL from '\(base)'")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["name": name])

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
            throw ClientError.http(status: http.statusCode,
                                   message: Self.message(status: http.statusCode, body: data))
        }
    }

    /// `detail` first, then the `error` slug, then the status — the precedence `cli._control` uses
    /// and for the same reason: the slug names the problem, the detail says what to do about it.
    /// Empty strings are skipped so a server that sends `"detail": ""` does not produce a message
    /// that explains nothing, which is exactly the silence this whole change is about.
    private static func message(status: Int, body: Data) -> String {
        let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
        for key in ["detail", "error"] {
            if let text = json?[key] as? String, !text.isEmpty { return text }
        }
        let reason = HTTPURLResponse.localizedString(forStatusCode: status)
        return reason.isEmpty ? "HTTP \(status)" : "HTTP \(status) \(reason)"
    }
}
