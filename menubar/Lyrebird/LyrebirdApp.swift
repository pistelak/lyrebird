import SwiftUI

@main
struct LyrebirdApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        MenuBarExtra {
            MenuContentView(model: model)
        } label: {
            StatusGlyph(status: model.status)
        }
        .menuBarExtraStyle(.window)
    }
}
