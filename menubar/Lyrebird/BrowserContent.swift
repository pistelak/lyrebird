import Foundation

struct BrowserContent {
    var status: AppModel.Status
    var rulesRead: MockClient.RulesRead?
    var recentRead: MockClient.RecentRead?
    var recentPlaceholder: String
    /// The live list only. The preview never enters it, so every control that reads it to offer
    /// activation — the request pane's button, the sidebar's double-click and menu — stays disabled
    /// on a preview row without a predicate of its own. See `BrowserControllerTests`.
    var scenarios: ScenarioList?
    var busy: Bool
    var lastError: String?
    /// The file preview, present only while the proxy is stopped. A failed preview is present too:
    /// its reason is what the rules panes show, not "Proxy is not running."
    var previewRead: AppModel.PreviewRead? = nil

    var recent: [RecentEntry] {
        if case .ok(let entries) = recentRead { return entries }
        return []
    }

    /// True for a preview that failed as well as one that succeeded — see `previewRead`.
    var previewing: Bool { previewRead != nil }

    var preview: ProfilePreview? {
        if case .ok(let preview) = previewRead { return preview }
        return nil
    }

    /// What the sidebar draws from the preview; nil when there is none, or it failed.
    var previewList: ScenarioList? { preview?.list }
}
