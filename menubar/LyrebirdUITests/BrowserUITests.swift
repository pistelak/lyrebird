import XCTest

final class BrowserUITests: XCTestCase {
    func testNativeBrowserNavigationAndWindowReopening() {
        let app = XCUIApplication()
        app.launchArguments = ["--preview"]
        app.launch()
        defer { app.terminate() }
        let window = app.windows["Lyrebird"]
        XCTAssertTrue(window.waitForExistence(timeout: 10))
        let pending = app.staticTexts["scenario:orders-pending"]
        XCTAssertTrue(pending.waitForExistence(timeout: 10))
        let detail = app.textViews["response-detail"]
        XCTAssertTrue(detail.waitForExistence(timeout: 5))
        let populated = NSPredicate(format: "value CONTAINS %@", "Example order")
        expectation(for: populated, evaluatedWith: detail)
        waitForExpectations(timeout: 10)
        XCTAssertGreaterThan(detail.frame.minY, window.frame.minY + 40)
        let before = window.frame
        app.staticTexts["scenario:empty"].click()
        let empty = NSPredicate(format: "value CONTAINS %@", "has no rules")
        expectation(for: empty, evaluatedWith: detail)
        waitForExpectations(timeout: 5)
        XCTAssertEqual(window.frame, before)
        pending.click()
        expectation(for: populated, evaluatedWith: detail)
        waitForExpectations(timeout: 5)
        XCTAssertEqual(app.searchFields.count, 0)
        let attachment = XCTAttachment(screenshot: window.screenshot())
        attachment.name = "AppKit browser light"
        attachment.lifetime = .keepAlways
        add(attachment)
        app.typeKey("w", modifierFlags: .command)
        XCTAssertFalse(window.exists)
        app.menuBars.menuBarItems["File"].click()
        app.menuItems["Scenarios"].click()
        XCTAssertTrue(window.waitForExistence(timeout: 5))
        app.typeKey(",", modifierFlags: .command)
        let settings = app.windows["Lyrebird Settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: 5))
        settings.buttons["Cancel"].click()
        XCTAssertFalse(settings.exists)
    }

    func testDarkAppearanceAndFullscreenKeepContentVisible() {
        let app = XCUIApplication()
        app.launchArguments = ["--preview", "--dark"]
        app.launch()
        defer { app.terminate() }
        let window = app.windows["Lyrebird"]
        XCTAssertTrue(window.waitForExistence(timeout: 10))
        let detail = app.textViews["response-detail"]
        expectation(for: NSPredicate(format: "value CONTAINS %@", "Example order"), evaluatedWith: detail)
        waitForExpectations(timeout: 10)
        let attachment = XCTAttachment(screenshot: window.screenshot())
        attachment.name = "AppKit browser dark"
        attachment.lifetime = .keepAlways
        add(attachment)
        let original = window.frame
        app.typeKey("f", modifierFlags: [.command, .control])
        expectation(for: NSPredicate { _, _ in window.frame.height > original.height }, evaluatedWith: window)
        waitForExpectations(timeout: 10)
        XCTAssertGreaterThanOrEqual(detail.frame.minY, window.frame.minY)
        XCTAssertLessThanOrEqual(detail.frame.maxY, window.frame.maxY + 1)
        // A full-screen menu bar is hidden; use the native keyboard equivalent to exit.
        app.typeKey("f", modifierFlags: [.command, .control])
        expectation(for: NSPredicate { _, _ in abs(window.frame.height - original.height) < 2 }, evaluatedWith: window)
        waitForExpectations(timeout: 10)
    }
    func testOriginalScenarioDesignAndCopyPlacement() {
        let app = XCUIApplication()
        app.launchArguments = ["--preview", "--dark", "--design-preview"]
        app.launch()
        defer { app.terminate() }
        let window = app.windows["Lyrebird"]
        XCTAssertTrue(window.waitForExistence(timeout: 10))
        let detail = app.textViews["response-detail"]
        expectation(for: NSPredicate(format: "value CONTAINS %@", "Notebook"), evaluatedWith: detail)
        waitForExpectations(timeout: 10)
        let copy = app.buttons["Copy body"]
        XCTAssertTrue(copy.exists)
        XCTAssertGreaterThan(copy.frame.minY, detail.frame.minY + 100)
        XCTAssertLessThan(copy.frame.maxY, detail.frame.maxY)
        let toggle = app.toolbars.buttons["Sidebar"]
        XCTAssertTrue(toggle.isHittable)
        let scenario = app.staticTexts["scenario:remove-an-item"]
        let expandedX = toggle.frame.midX
        XCTAssertLessThanOrEqual(toggle.frame.maxX, app.tables["request-list"].frame.minX + 1)
        toggle.click()
        expectation(for: NSPredicate { _, _ in !scenario.isHittable }, evaluatedWith: window)
        waitForExpectations(timeout: 5)
        XCTAssertTrue(toggle.isHittable)
        XCTAssertLessThan(toggle.frame.midX, expandedX)
        toggle.click()
        expectation(for: NSPredicate { _, _ in scenario.isHittable }, evaluatedWith: window)
        waitForExpectations(timeout: 5)
        let screenshot = XCTAttachment(screenshot: window.screenshot())
        screenshot.name = "Original scenario — AppKit design"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }
}
