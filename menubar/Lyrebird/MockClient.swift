import Foundation

/// Thin read/activate client over the engine's control API. Never reimplements proxy logic.
struct MockClient: Sendable {
    var base: URL

    private func get<T: Decodable>(_ path: String, as type: T.Type) async -> T? {
        guard let url = URL(string: path, relativeTo: base) else { return nil }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            return nil
        }
    }

    func health() async -> Health? { await get("/__mock__/health", as: Health.self) }

    func sessions() async -> SessionList? { await get("/__mock__/sessions", as: SessionList.self) }

    func recent() async -> [RecentEntry] { await get("/__mock__/recent", as: [RecentEntry].self) ?? [] }

    func activate(_ name: String) async {
        guard let url = URL(string: "/__mock__/sessions/active", relativeTo: base) else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["name": name])
        _ = try? await URLSession.shared.data(for: request)
    }
}
