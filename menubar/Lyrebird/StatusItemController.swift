import AppKit

@MainActor
final class StatusItemController: NSObject, NSMenuDelegate {
    private let model: AppModel
    private let item: NSStatusItem
    private let menu = NSMenu()
    private let observation = ModelObservation()
    var onBrowse: () -> Void = {}
    var onSettings: () -> Void = {}

    init(model: AppModel) {
        self.model = model
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        observation.start { [weak self] in self?.update() }
    }

    private func update() {
        let status = model.status
        let symbol = NSImage(systemSymbolName: status.symbolName, accessibilityDescription: model.statusLine)
        if let dot = status.dotColor {
            let image = NSImage(size: NSSize(width: 24, height: 18), flipped: false) { rect in
                symbol?.draw(in: NSRect(x: 0, y: 1, width: 18, height: 16))
                dot.setFill()
                NSBezierPath(ovalIn: NSRect(x: 17, y: 0, width: 6, height: 6)).fill()
                return true
            }
            item.button?.image = image
        } else {
            item.button?.image = symbol
        }
        item.button?.toolTip = model.statusLine
        item.button?.setAccessibilityLabel("Lyrebird: " + model.statusLine)
        rebuildMenu()
    }

    func menuWillOpen(_ menu: NSMenu) { rebuildMenu() }

    private func rebuildMenu() {
        menu.removeAllItems()
        func command(_ title: String, _ action: Selector?, enabled: Bool = true, object: Any? = nil) -> NSMenuItem {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
            entry.target = self
            entry.isEnabled = enabled && action != nil
            entry.representedObject = object
            menu.addItem(entry)
            return entry
        }
        _ = command(model.statusLine, nil)
        _ = command(model.stopsRatherThanStarts ? "Stop" : "Start", #selector(toggle), enabled: !model.busy)
        _ = command("Relaunch app", #selector(relaunch), enabled: !model.busy && !(model.simBundleId ?? "").isEmpty)
        if let error = model.lastError, !error.isEmpty {
            let entry = command("Error: " + error, nil)
            entry.toolTip = error
        }
        menu.addItem(.separator())
        _ = command("Scenarios…", #selector(browse))
        if let list = model.scenarios {
            for scenario in list.scenarios {
                let entry = command(
                    scenario.name + "  (\(scenario.overrideCount))" + (scenario.verified ? "  ✓" : ""),
                    #selector(activate(_:)), enabled: !model.busy, object: scenario.name)
                entry.state = scenario.name == list.active ? .on : .off
            }
        } else {
            _ = command(model.scenariosPlaceholder, nil)
        }
        menu.addItem(.separator())
        _ = command("Recent traffic", nil)
        if model.recent.isEmpty {
            _ = command(model.recentPlaceholder, nil)
        } else {
            for entry in model.recent.prefix(8) {
                let row = command(
                    entry.method + "  " + RuleFormatting.statusText(entry.status) + "  " + entry.path, #selector(browse)
                )
                row.toolTip = row.title
            }
        }
        _ = command("Clear recent traffic", #selector(clear), enabled: !model.busy && !model.recent.isEmpty)
        menu.addItem(.separator())
        _ = command("Settings…", #selector(settings))
        let quit = command("Quit Lyrebird", #selector(terminate))
        quit.keyEquivalent = "q"
    }
    @objc private func toggle() { Task { await model.toggle() } }
    @objc private func relaunch() { Task { await model.relaunchApp() } }
    @objc private func activate(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        Task { await model.activate(name) }
    }
    @objc private func clear() { Task { await model.clearRecent() } }
    @objc private func browse() { onBrowse() }
    @objc private func settings() { onSettings() }
    @objc private func terminate() { NSApp.terminate(nil) }
}
