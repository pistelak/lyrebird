import Foundation

struct BrowserContent {
    var status: AppModel.Status
    var controlPort: Int?
    var rulesRead: MockClient.RulesRead?
    var recentRead: MockClient.RecentRead?
    var recentPlaceholder: String
    var scenarios: ScenarioList?
    var busy: Bool
    var lastError: String?

    var recent: [RecentEntry] {
        if case .ok(let entries) = recentRead { return entries }
        return []
    }
}
