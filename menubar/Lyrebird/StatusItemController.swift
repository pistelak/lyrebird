import AppKit

@MainActor
final class StatusItemController: NSObject {
    struct Content {
        var status: AppModel.Status
        var statusLine: String
        var stopsRatherThanStarts: Bool
        var busy: Bool
        var simBundleId: String?
        var lastError: String?
        var scenarios: ScenarioList?
        var scenariosPlaceholder: String
        var recent: [RecentEntry]
        var recentPlaceholder: String
    }

    private var content: Content
    private let item: NSStatusItem
    // Expose the menu to catch stale folder scoping; see menuOpeningScopesScenariosToTheActiveFolder.
    let menu = NSMenu()
    let observation = ModelObservation()
    var menuContent: (() -> Content)?
    private let dotView = StatusDotView()
    var onBrowse: () -> Void = {}
    var onSettings: () -> Void = {}

    var onToggle: () -> Void = {}
    var onRelaunch: () -> Void = {}
    var onActivate: (String) -> Void = { _ in }
    var onClear: () -> Void = {}

    init(content: Content) {
        self.content = content
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
        update(content)
    }

    func update(_ content: Content) {
        self.content = content
        let status = content.status
        item.button?.image = Self.templateImage(for: status, description: content.statusLine)
        dotView.color = status.dotColor
        dotView.isHidden = status.dotColor == nil
        item.button?.toolTip = content.statusLine
        item.button?.setAccessibilityLabel("Lyrebird: " + content.statusLine)
        rebuildMenu()
    }

    static func templateImage(for status: AppModel.Status, description: String) -> NSImage? {
        let symbol = NSImage(systemSymbolName: status.symbolName, accessibilityDescription: description)
        guard status.dotColor != nil else { return symbol }
        // A separate indicator keeps the bird tintable by AppKit; see menuBarBirdRemainsATemplateInEveryStatus.
        let image = NSImage(size: NSSize(width: 24, height: 18), flipped: false) { _ in
            symbol?.draw(in: NSRect(x: 0, y: 1, width: 18, height: 16))
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = description
        return image
    }

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
        _ = command(content.statusLine, nil)
        _ = command(content.stopsRatherThanStarts ? "Stop" : "Start", #selector(toggle), enabled: !content.busy)
        _ = command("Relaunch app", #selector(relaunch), enabled: !content.busy && !(content.simBundleId ?? "").isEmpty)
        if let error = content.lastError, !error.isEmpty {
            let entry = command("Error: " + error, nil)
            entry.toolTip = error
        }
        menu.addItem(.separator())
        _ = command("Scenarios…", #selector(browse))
        if let list = content.scenarios {
            let folder = list.shownFolder()
            if let caption = folder.caption { _ = command(caption, nil) }
            for scenario in folder.scenarios {
                let entry = command(
                    scenario.leaf + "  (\(scenario.overrideCount))" + (scenario.verified ? "  ✓" : ""),
                    #selector(activate(_:)), enabled: !content.busy, object: scenario.name)
                entry.state = scenario.name == list.active ? .on : .off
            }
        } else {
            _ = command(content.scenariosPlaceholder, nil)
        }
        menu.addItem(.separator())
        _ = command("Recent traffic", nil)
        if content.recent.isEmpty {
            _ = command(content.recentPlaceholder, nil)
        } else {
            for entry in content.recent.prefix(8) {
                let row = command(
                    entry.method + "  " + RuleFormatting.statusText(entry.status) + "  " + entry.path, #selector(browse)
                )
                row.toolTip = row.title
            }
        }
        _ = command("Clear recent traffic", #selector(clear), enabled: !content.busy && !content.recent.isEmpty)
        menu.addItem(.separator())
        _ = command("Settings…", #selector(settings))
        let quit = command("Quit Lyrebird", #selector(terminate))
        quit.keyEquivalent = "q"
    }

    @objc private func toggle() {
        onToggle()
    }

    @objc private func relaunch() {
        onRelaunch()
    }

    @objc private func activate(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        onActivate(name)
    }

    @objc private func clear() {
        onClear()
    }

    @objc private func browse() {
        onBrowse()
    }

    @objc private func settings() {
        onSettings()
    }

    @objc private func terminate() {
        NSApp.terminate(nil)
    }
}

@MainActor
private final class StatusDotView: NSView {
    var color: NSColor? { didSet { needsDisplay = true } }

    override func hitTest(_ point: NSPoint) -> NSView? {
        nil
    }

    override func draw(_ dirtyRect: NSRect) {
        color?.setFill()
        NSBezierPath(ovalIn: bounds).fill()
    }
}

extension StatusItemController: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        if let menuContent { content = menuContent() }
        rebuildMenu()
    }
}
