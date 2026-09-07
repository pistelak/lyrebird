import SwiftUI

@main
struct LyrebirdApp: App {
    // The unit tests are hosted by this app, so an autostarted model would poll the real control
    // port and shell out to the real CLI for the length of every `xcodebuild test` run — see
    // testAHostedTestRunIsDetectedByTheLoadedXCTest.
    @State private var model = AppModel(autoStart: !LyrebirdApp.isHostingTests())

    /// Decided by whether XCTest is loaded, not by the environment: XCTest variables inherited by
    /// a shell used to stop the app it launched from ever starting its model — see
    /// testAnAppWithoutXCTestLoadedIsNotATestRun.
    static func isHostingTests(xctestCase: AnyClass? = NSClassFromString("XCTestCase")) -> Bool {
        xctestCase != nil
    }

    var body: some Scene {
        MenuBarExtra {
            MenuContentView(model: model)
        } label: {
            StatusGlyph(status: model.status)
        }
        .menuBarExtraStyle(.window)
    }
}
