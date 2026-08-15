import Foundation

/// Only the fields the menu actually renders. Unknown JSON keys are ignored by the decoder.
struct Health: Codable, Sendable {
    var activeSession: String?
    var overrideCount: Int?
    var proxyUp: Bool?
    var intercepting: Bool?
    /// Comes from the active profile, so the app never carries a default app identifier of its own.
    var simBundleId: String?
}

struct SessionSummary: Codable, Sendable, Identifiable {
    var name: String
    var overrideCount: Int
    var verified: Bool
    var notes: String?
    var id: String { name }
}

struct SessionList: Codable, Sendable {
    var active: String
    var sessions: [SessionSummary]
}

struct RecentEntry: Codable, Sendable {
    var method: String
    var path: String
    var status: Int
    var matched: String?
}
