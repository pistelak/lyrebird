import AppKit

extension BrowserContent {
    @MainActor
    init(model: AppModel) {
        self.init(
            status: model.status, controlPort: model.ownHealth?.proxyPort, rulesRead: model.rulesRead,
            recentRead: model.recentRead, recentPlaceholder: model.recentPlaceholder,
            scenarios: model.scenarios, busy: model.busy, lastError: model.lastError)
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
