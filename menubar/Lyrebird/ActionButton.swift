import AppKit

@MainActor
final class ActionButton: NSButton {
    var invoke: () -> Void

    init(_ title: String, action: @escaping () -> Void) {
        invoke = action
        super.init(frame: .zero)
        self.title = title
        bezelStyle = .rounded
        target = self
        self.action = #selector(performAction)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    @objc private func performAction() {
        invoke()
    }
}
