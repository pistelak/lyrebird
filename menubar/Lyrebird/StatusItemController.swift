import AppKit

@MainActor
final class StatusItemController: NSObject, NSMenuDelegate {
    private let model: AppModel
    private let item: NSStatusItem
    // Not private: the folder-scoping tests drive `menuWillOpen` and read the items it built, which is
    // the only way to catch a menu that stops following the active folder — see
    // menuOpeningScopesScenariosToTheActiveFolder.
    let menu = NSMenu()
    private let observation = ModelObservation()
    private let dotView = StatusDotView()
    var onBrowse: () -> Void = {}
    var onSettings: () -> Void = {}

    init(model: AppModel) {
        self.model = model
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        if let button = item.button {
            dotView.translatesAutoresizingMaskIntoConstraints = false
            button.addSubview(dotView)
            NSLayoutConstraint.activate([
                dotView.widthAnchor.constraint(equalToConstant: 6),
                dotView.heightAnchor.constraint(equalToConstant: 6),
                dotView.centerXAnchor.constraint(equalTo: button.centerXAnchor, constant: 8),
                dotView.centerYAnchor.constraint(equalTo: button.centerYAnchor, constant: 5),
            ])
        }
        observation.start { [weak self] in self?.update() }
    }

    private func update() {
        let status = model.status
        item.button?.image = Self.templateImage(for: status, description: model.statusLine)
        dotView.color = status.dotColor
        dotView.isHidden = status.dotColor == nil
        item.button?.toolTip = model.statusLine
        item.button?.setAccessibilityLabel("Lyrebird: " + model.statusLine)
        rebuildMenu()
    }

    static func templateImage(for status: AppModel.Status, description: String) -> NSImage? {
        let symbol = NSImage(systemSymbolName: status.symbolName, accessibilityDescription: description)
        guard status.dotColor != nil else { return symbol }
        // Keep the colored indicator outside the template so AppKit can tint the bird independently.
        let image = NSImage(size: NSSize(width: 24, height: 18), flipped: false) { _ in
            symbol?.draw(in: NSRect(x: 0, y: 1, width: 18, height: 16))
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = description
        return image
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
            let folder = list.shownFolder()
            if let caption = folder.caption { _ = command(caption, nil) }
            for scenario in folder.scenarios {
                let entry = command(
                    scenario.leaf + "  (\(scenario.overrideCount))" + (scenario.verified ? "  ✓" : ""),
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

@MainActor
private final class StatusDotView: NSView {
    var color: NSColor? { didSet { needsDisplay = true } }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func draw(_ dirtyRect: NSRect) {
        color?.setFill()
        NSBezierPath(ovalIn: bounds).fill()
    }
}
