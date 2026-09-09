import SwiftUI

/// Reopening from the Dock raises the browsing window.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        WindowLauncher.show()
        return false
    }
}

@main
struct LyrebirdApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    // The unit tests are hosted by this app, so an autostarted model would poll the real control
    // port and shell out to the real CLI for the length of every `xcodebuild test` run — see
    // `LaunchEnvironmentTests`.
    @State private var model = AppModel(autoStart: !LyrebirdApp.isHostingTests())
    @State private var searchFocus = SearchFocus()

    /// Decided by whether XCTest is loaded, not by the environment: XCTest variables inherited by
    /// a shell used to stop the app it launched from ever starting its model — see
    /// `LaunchEnvironmentTests`.
    static func isHostingTests(xctestCase: AnyClass? = NSClassFromString("XCTestCase")) -> Bool {
        xctestCase != nil
    }

    var body: some Scene {
        MenuBarExtra {
            MenuContentView(model: model)
        } label: {
            StatusLabel(status: model.status)
        }
        .menuBarExtraStyle(.window)

        // WindowGroup supports an independent full-screen space; WindowLauncher keeps it single-instance.
        WindowGroup("Lyrebird", id: RulesWindowView.sceneId) {
            RulesWindowView(model: model, searchFocus: searchFocus)
        }
        .defaultSize(width: 1120, height: 640)
        .windowResizability(.contentMinSize)
        .commands {
            // All panes share one browsing model, so disable additional windows.
            CommandGroup(replacing: .newItem) {}
            CommandGroup(after: .textEditing) {
                Button("Find") { searchFocus.request() }
                    .keyboardShortcut("f", modifiers: .command)
            }
        }
    }
}

/// Installs scene and Dock integration at launch, before the menu is opened.
private struct StatusLabel: View {
    let status: AppModel.Status
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        StatusGlyph(status: status)
            .task {
                WindowLauncher.openScene = { openWindow(id: RulesWindowView.sceneId) }
                DockPresence.settingChanged()
            }
    }
}
