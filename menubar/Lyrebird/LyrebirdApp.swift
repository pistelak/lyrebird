import AppKit

@main
enum LyrebirdApp {
    @MainActor
    static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }

    static func isHostingTests(xctestCase: AnyClass? = NSClassFromString("XCTestCase")) -> Bool {
        xctestCase != nil
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var model: AppModel?
    private var browser: RulesWindowController?
    private var settings: SettingsWindowController?
    private var status: StatusItemController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Hosted tests must never poll a real profile or launch the CLI; see LaunchEnvironmentTests.
        guard !LyrebirdApp.isHostingTests() else { return }
        let model: AppModel
        #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("--preview") {
                model = BrowserPreview.makeModel()
                if ProcessInfo.processInfo.arguments.contains("--dark") {
                    NSApp.appearance = NSAppearance(named: .darkAqua)
                } else {
                    NSApp.appearance = NSAppearance(named: .aqua)
                }
            } else {
                model = AppModel()
            }
        #else
            model = AppModel()
        #endif
        self.model = model
        installMenus()
        status = StatusItemController(model: model)
        status?.onBrowse = { [weak self] in self?.showBrowser(nil) }
        status?.onSettings = { [weak self] in self?.showSettings(nil) }
        DockPresence.settingChanged()
        if !Config.dockOnlyWhileWindowOpen { showBrowser(nil) }
    }

    @objc func showBrowser(_ sender: Any?) {
        guard let model else { return }
        if browser == nil {
            browser = RulesWindowController(
                model: model, restore: !ProcessInfo.processInfo.arguments.contains("--preview"))
        }
        browser?.showWindow(sender)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc func showSettings(_ sender: Any?) {
        guard let model else { return }
        if settings == nil { settings = SettingsWindowController(model: model) }
        settings?.showWindow(sender)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showBrowser(nil)
        return false
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    private func installMenus() {
        let main = NSMenu()
        func submenu(_ title: String) -> NSMenu {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let menu = NSMenu(title: title)
            item.submenu = menu
            main.addItem(item)
            return menu
        }
        func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String = "", target: AnyObject? = nil) {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.target = target
            menu.addItem(item)
        }
        let app = submenu("Lyrebird")
        add(app, "About Lyrebird", #selector(NSApplication.orderFrontStandardAboutPanel(_:)))
        add(app, "Settings…", #selector(showSettings), ",", target: self)
        app.addItem(.separator())
        add(app, "Hide Lyrebird", #selector(NSApplication.hide(_:)), "h")
        add(app, "Show All", #selector(NSApplication.unhideAllApplications(_:)))
        app.addItem(.separator())
        add(app, "Quit Lyrebird", #selector(NSApplication.terminate(_:)), "q")
        let file = submenu("File")
        add(file, "Scenarios", #selector(showBrowser), "o", target: self)
        add(file, "Close", #selector(NSWindow.performClose(_:)), "w")
        let edit = submenu("Edit")
        add(edit, "Undo", Selector(("undo:")), "z")
        add(edit, "Redo", Selector(("redo:")), "Z")
        edit.addItem(.separator())
        add(edit, "Cut", #selector(NSText.cut(_:)), "x")
        add(edit, "Copy", #selector(NSText.copy(_:)), "c")
        add(edit, "Paste", #selector(NSText.paste(_:)), "v")
        add(edit, "Select All", #selector(NSText.selectAll(_:)), "a")
        edit.addItem(.separator())
        let view = submenu("View")
        add(view, "Toggle Sidebar", #selector(RulesWindowController.toggleSidebar(_:)))
        add(view, "Refresh", #selector(RulesWindowController.refresh(_:)), "r")
        let window = submenu("Window")
        add(window, "Minimize", #selector(NSWindow.performMiniaturize(_:)), "m")
        add(window, "Zoom", #selector(NSWindow.performZoom(_:)))
        add(window, "Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f")
        window.items.last?.keyEquivalentModifierMask = [.command, .control]
        NSApp.windowsMenu = window
        NSApp.mainMenu = main
    }
}
