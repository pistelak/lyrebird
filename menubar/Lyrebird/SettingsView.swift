import SwiftUI

struct SettingsView: View {
    // `store:` on every one of them: `@AppStorage` defaults to `.standard`, and the sheet writing
    // there while `Config` read somewhere else would leave a typed-in path that nothing acts on.
    @AppStorage(Config.controlURLKey, store: Config.defaults) private var controlURL = Config.defaultControlURL
    @AppStorage(Config.lyrebirdPathKey, store: Config.defaults) private var lyrebirdPath = ""
    @AppStorage(Config.profilePathKey, store: Config.defaults) private var profilePath = ""
    @AppStorage(Config.dockOnlyWhileWindowOpenKey, store: Config.defaults)
    private var dockOnlyWhileWindowOpen = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.section) {
            Text("Settings").font(.headline)
            field("Control URL", text: $controlURL, placeholder: Config.defaultControlURL)
            field("lyrebird launcher path", text: $lyrebirdPath, placeholder: Config.lyrebirdPath)
            field(
                "Profile directory", text: $profilePath,
                placeholder: "~/lyrebird-profiles/my-app")
            Text(
                "The profile holds the hosts to intercept, your saved scenarios, and the "
                    + "simulator bundle id. Leave blank to use the engine's default profile."
            )
            .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Toggle("Show in Dock only while a window is open", isOn: $dockOnlyWhileWindowOpen)
            Text(
                "Off: Lyrebird stays in the Dock while it runs. On: it lives in the menu bar and "
                    + "appears in the Dock only while its window is open."
            )
            .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(RuleFormatting.Space.section)
        .frame(width: 420)
    }

    private func field(_ label: String, text: Binding<String>, placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            TextField(placeholder, text: text).textFieldStyle(.roundedBorder)
        }
    }
}
