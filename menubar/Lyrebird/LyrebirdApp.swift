import SwiftUI

@main
struct LyrebirdApp: App {
    // The unit tests are hosted by this app, so an autostarted model would poll the real control
    // port and shell out to the real CLI for the length of every `xcodebuild test` run.
    @State private var model = AppModel(
        autoStart: ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil)

    var body: some Scene {
        MenuBarExtra {
            MenuContentView(model: model)
        } label: {
            StatusGlyph(status: model.status)
        }
        .menuBarExtraStyle(.window)
    }
}
