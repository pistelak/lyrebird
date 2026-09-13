import AppKit

@MainActor
final class SettingsWindowController: NSWindowController {

    private let model: AppModel

    let controlURL = NSTextField()

    let launcher = NSTextField()

    let profile = NSTextField()

    let dock = NSButton(checkboxWithTitle: "Show in Dock only while a window is open", target: nil, action: nil)

    private let error = NSTextField(wrappingLabelWithString: "")

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
        error.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
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

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

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
        do {
            _ = try Config.validateControlURL(text)
        } catch {
            self.error.stringValue = error.localizedDescription
            return
        }
        // The model writes the settings, because a changed control URL has to stop the session the
        // old one addresses first — and a `down` that fails keeps the window open with the reason.
        let launcher = self.launcher.stringValue
        let profile = self.profile.stringValue
        let dockOnly = dock.state == .on
        Task {
            if let refusal = await model.commitSettings(
                controlURL: text, launcher: launcher, profile: profile, dockOnlyWhileWindowOpen: dockOnly)
            {
                self.error.stringValue = refusal
                return
            }
            close()
        }
    }

}

extension SettingsWindowController: NSWindowDelegate {
    func windowWillClose(_ notification: Notification) {
        guard registered else { return }
        registered = false
        DockPresence.windowClosed()
    }
}
