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

        // A separate scene rather than a sheet on the menu: the menu closes the moment focus moves,
        // and a rules table is something you keep open beside the app you are driving.
        Window("Rules", id: RulesWindowView.sceneId) {
            RulesWindowView(model: model)
        }
        .defaultSize(width: 900, height: 560)
    }
}
