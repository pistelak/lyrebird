import AppKit

@MainActor
final class SettingsWindowController: NSWindowController, NSWindowDelegate {
    private let model: AppModel
    let controlURL = NSTextField()
    let launcher = NSTextField()
    let profile = NSTextField()
    let dock = NSButton(checkboxWithTitle: "Show in Dock only while a window is open", target: nil, action: nil)
    private let error = NativeStyle.label("")
    private var registered = false

    init(model: AppModel) {
        self.model = model
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 380), styleMask: [.titled, .closable],
            backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Lyrebird Settings"
        window.isReleasedWhenClosed = false
        window.delegate = self
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10

        func field(_ title: String, _ field: NSTextField, _ placeholder: String) {
            stack.addArrangedSubview(NativeStyle.label(title, size: 12, weight: .medium))
            field.placeholderString = placeholder
            field.setAccessibilityLabel(title)
            stack.addArrangedSubview(field)
            field.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        field("Control URL", controlURL, Config.defaultControlURL)
        field("lyrebird launcher path", launcher, "Path to the lyrebird executable")
        field("Profile directory", profile, "~/lyrebird-profiles/my-app")
        let note = NSTextField(
            wrappingLabelWithString:
                "The profile holds your hosts, scenarios and simulator bundle ID. Leave the profile blank to use the engine’s default. Leave the launcher blank to find it automatically."
        )
        note.font = .systemFont(ofSize: 12)
        note.textColor = .secondaryLabelColor
        stack.addArrangedSubview(note)
        note.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        stack.addArrangedSubview(dock)
        error.textColor = .systemRed
        stack.addArrangedSubview(error)
        let cancel = ActionButton("Cancel") { [weak self] in self?.close() }
        cancel.keyEquivalent = "\u{1b}"
        let save = ActionButton("Save") { [weak self] in self?.save() }
        save.keyEquivalent = "\r"
        let buttons = NSStackView(views: [NSView(), cancel, save])
        stack.addArrangedSubview(buttons)
        buttons.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        NativeStyle.pin(stack, in: window.contentView!, inset: 20)
        window.center()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func showWindow(_ sender: Any?) {
        if !registered {
            registered = true
            DockPresence.windowOpened()
            controlURL.stringValue = Config.defaults.string(forKey: Config.controlURLKey) ?? Config.defaultControlURL
            launcher.stringValue = Config.defaults.string(forKey: Config.lyrebirdPathKey) ?? ""
            profile.stringValue = Config.profilePath
            dock.state = Config.dockOnlyWhileWindowOpen ? .on : .off
            error.stringValue = ""
        }
        super.showWindow(sender)
        window?.makeKeyAndOrderFront(sender)
    }

    func save() {
        let text = controlURL.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: text), ["http", "https"].contains(url.scheme?.lowercased() ?? ""), url.host != nil
        else {
            error.stringValue = "Enter an HTTP or HTTPS control URL."
            return
        }
        Config.defaults.set(text, forKey: Config.controlURLKey)
        Config.defaults.set(launcher.stringValue, forKey: Config.lyrebirdPathKey)
        Config.defaults.set(profile.stringValue, forKey: Config.profilePathKey)
        Config.defaults.set(dock.state == .on, forKey: Config.dockOnlyWhileWindowOpenKey)
        close()
        Task { await model.settingsChanged() }
    }

    func windowWillClose(_ notification: Notification) {
        guard registered else { return }
        registered = false
        DockPresence.windowClosed()
    }
}
