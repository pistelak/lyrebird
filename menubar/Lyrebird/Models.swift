import Foundation

/// Only the fields the menu actually renders. Unknown JSON keys are ignored by the decoder.
struct Health: Codable, Sendable {
    var activeScenario: String?
    var overrideCount: Int?
    var proxyUp: Bool?
    var intercepting: Bool?
    /// Comes from the active profile, so the app never carries a default app identifier of its own.
    var simBundleId: String?
    /// Which profile the answering proxy is running. The app never computes this — it is
    /// `sha256(profile dir)[:12]` in the engine's `config`, and re-deriving it here would be a
    /// second implementation of a rule only the engine owns. Optional because an older engine
    /// does not send it, which the CLI accepts too.
    var profileFingerprint: String?
}

struct ScenarioSummary: Codable, Sendable, Identifiable {
    var name: String
    var overrideCount: Int
    var verified: Bool
    var notes: String?
    var id: String { name }
}

struct ScenarioList: Codable, Sendable {
    var active: String
    var scenarios: [ScenarioSummary]
}

struct RecentEntry: Codable, Sendable {
    var method: String
    var path: String
    var status: Int
    var matched: String?
}
