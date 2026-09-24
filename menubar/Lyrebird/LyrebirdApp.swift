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
    /// Answers a termination request once its decision is made. A seam: a test replaces it, since
    /// a real `reply(true)` would terminate the test host.
    var replyToTermination: (Bool) -> Void = { NSApp.reply(toApplicationShouldTerminate: $0) }
    /// Raises the termination request the alternate Quit makes. The same seam, for the same
    /// reason: a real `terminate` in a test host ends the test run.
    var requestTermination: () -> Void = { NSApp.terminate(nil) }
    /// A termination request whose answer is still being decided. One at a time: AppKit is owed
    /// exactly one reply per request, and a second Quit during the first's `down` used to start a
    /// second decision that could answer after the first — see `QuitTests`.
    private var terminationPending = false
    /// "Quit, leave the proxy running" was chosen for the termination request about to arrive.
    /// Captured and cleared by every request, accepted or not, so a choice made while another
    /// quit was still deciding cannot be consumed by a later plain ⌘Q — see `QuitTests`.
    private var leaveProxyRequested = false
    private var browser: RulesWindowController?
    private var settings: SettingsWindowController?
    private var status: StatusItemController?

    /// The model is handed in by a test; the app builds its own at launch.
    init(model: AppModel? = nil) {
        self.model = model
        super.init()
    }

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
        status?.onLeaveProxy = { [weak self] in self?.quitLeavingProxy() }
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

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    /// Quit stops this profile's running proxy first, or stays open saying why it could not —
    /// `AppModel.prepareToQuit` decides. Not under `--preview`: that model claims an intercepting
    /// proxy over a launcher path that does not exist, and the UI tests run and terminate the app
    /// that way.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let leaveProxy = leaveProxyRequested
        leaveProxyRequested = false
        guard let model else { return .terminateNow }
        #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("--preview") { return .terminateNow }
        #endif
        // The pending request will answer; this one is declined so it cannot answer twice.
        guard !terminationPending else { return .terminateCancel }
        terminationPending = true
        Task { @MainActor in
            let quit = await model.prepareToQuit(leaveProxy: leaveProxy)
            terminationPending = false
            replyToTermination(quit)
        }
        return .terminateLater
    }

    /// The status menu's alternate Quit: the choice is recorded for the request `terminate`
    /// raises synchronously on this same turn of the run loop, and for no other.
    func quitLeavingProxy() {
        leaveProxyRequested = true
        requestTermination()
    }

    private func installMenus() {
        NSApp.mainMenu = makeMainMenu()
    }

    /// Built apart from installing it so a test can read what the menus carry — see
    /// theFileMenuCarriesNoInterceptionItem.
    func makeMainMenu() -> NSMenu {
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
        add(file, "Activate Scenario", #selector(RulesWindowController.activateSelectedScenario(_:)), "\r")
        // No Start/Stop here: with no browser window key the responder chain ends at the delegate,
        // and the item sat greyed out under a stale title. The status menu and the toolbar carry it.
        add(file, "Close", #selector(NSWindow.performClose(_:)), "w")
        let edit = submenu("Edit")
        add(edit, "Undo", Selector(("undo:")), "z")
        add(edit, "Redo", Selector(("redo:")), "Z")
        edit.addItem(.separator())
        add(edit, "Cut", #selector(NSText.cut(_:)), "x")
        add(edit, "Copy", #selector(NSText.copy(_:)), "c")
        add(edit, "Paste", #selector(NSText.paste(_:)), "v")
        add(edit, "Select All", #selector(NSText.selectAll(_:)), "a")
        let view = submenu("View")
        add(view, "Hide Sidebar", #selector(RulesWindowController.toggleSidebar(_:)), "s")
        view.items.last?.keyEquivalentModifierMask = [.command, .control]
        add(view, "Refresh", #selector(RulesWindowController.refresh(_:)), "r")
        let window = submenu("Window")
        add(window, "Minimize", #selector(NSWindow.performMiniaturize(_:)), "m")
        add(window, "Zoom", #selector(NSWindow.performZoom(_:)))
        add(window, "Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f")
        window.items.last?.keyEquivalentModifierMask = [.command, .control]
        NSApp.windowsMenu = window
        return main
    }
}
