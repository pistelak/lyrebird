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

    /// The preview in the sidebar's shape; nil when there is none, or it failed. `active` is empty
    /// because it matches no name: a file has no run, so no row may be marked active — see
    /// `FilePreviewTests`.
    var previewList: ScenarioList? {
        if case .ok(let preview) = previewRead { return ScenarioList(active: "", scenarios: preview.scenarios) }
        return nil
    }
}
