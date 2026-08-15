import SwiftUI

struct SettingsView: View {
    @AppStorage(Config.controlURLKey) private var controlURL = Config.defaultControlURL
    @AppStorage(Config.lyrebirdPathKey) private var lyrebirdPath = ""
    @AppStorage(Config.profilePathKey) private var profilePath = ""
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Settings").font(.headline)
            field("Control URL", text: $controlURL, placeholder: Config.defaultControlURL)
            field("lyrebird launcher path", text: $lyrebirdPath, placeholder: Config.lyrebirdPath)
            field("Profile directory", text: $profilePath,
                  placeholder: "~/lyrebird-profiles/my-app")
            Text("The profile holds the hosts to intercept, your saved sessions, and the "
                 + "simulator bundle id. Leave blank to use the engine's default profile.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            HStack { Spacer(); Button("Done") { dismiss() }.keyboardShortcut(.defaultAction) }
        }
        .padding(16)
        .frame(width: 420)
    }

    private func field(_ label: String, text: Binding<String>, placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            TextField(placeholder, text: text).textFieldStyle(.roundedBorder)
        }
    }
}
