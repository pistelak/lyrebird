import AppKit

extension BrowserContent {
    @MainActor
    init(model: AppModel) {
        self.init(
            status: model.status, rulesRead: Self.rules(of: model),
            recentRead: model.recentRead, recentPlaceholder: model.recentPlaceholder,
            scenarios: model.scenarios, busy: model.busy, lastError: model.lastError,
            previewRead: model.previewRead)
    }

    /// The rules to show: the live read, or — while previewing — the browsed scenario's rows out
    /// of the preview payload, which carries every scenario at once.
    ///
    /// A browsed name the payload lacks is reported with the payload's top-level problems: a
    /// scenario whose file stopped loading keeps its selection, and without those lines the reader
    /// saw "no scenario orders" while the parser's reason sat in a note this branch never renders —
    /// see `NativeContentTests`.
    @MainActor
    private static func rules(of model: AppModel) -> MockClient.RulesRead? {
        guard let previewRead = model.previewRead else { return model.rulesRead }
        switch previewRead {
        case .unavailable(let reason):
            return .unavailable(reason)
        case .ok(let preview):
            // The same first scenario the window browses on a cold open, so the first render after
            // the preview arrives already has rules to show.
            guard let name = model.browsedScenario ?? preview.scenarios.first?.name else { return nil }
            if let snapshot = preview.rules[name] { return .ok(snapshot) }
            return .unavailable(
                (["no scenario \(name) in the profile's files"] + preview.problems).joined(separator: "\n"))
        }
    }
}

extension StatusItemController.Content {
    @MainActor
    init(model: AppModel) {
        self.init(
            status: model.status, statusLine: model.statusLine, stopsRatherThanStarts: model.stopsRatherThanStarts,
            busy: model.busy, simBundleId: model.simBundleId, lastError: model.lastError,
            scenarios: model.scenarios, scenariosPlaceholder: model.scenariosPlaceholder,
            recent: model.recent, recentPlaceholder: model.recentPlaceholder)
    }
}

extension StatusItemController {
    convenience init(model: AppModel) {
        self.init(content: Content(model: model))
        onToggle = { Task { await model.toggle() } }
        onRelaunch = { Task { await model.relaunchApp() } }
        onActivate = { name in Task { await model.activate(name) } }
        onClear = { Task { await model.clearRecent() } }
        menuContent = { Content(model: model) }
        observation.start { [weak self] in self?.update(Content(model: model)) }
    }
}
