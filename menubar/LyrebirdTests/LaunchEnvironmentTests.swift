import XCTest

@testable import Lyrebird

/// The model is left unstarted when the app is hosting `xcodebuild test`. These pin how the app
/// tells a test run from an ordinary launch.
final class LaunchEnvironmentTests: XCTestCase {

    /// This process is a test host, so the shipped detection must say so — otherwise the model
    /// would start here and every test run would poll the real control port. It shows XCTest is
    /// loaded by the time a test runs; that it is already loaded when the App struct initializes
    /// was checked by a probe in the host on Xcode 26.6.
    func testAHostedTestRunIsDetectedByTheLoadedXCTest() {
        XCTAssertTrue(LyrebirdApp.isHostingTests())
    }

    /// The decision used to be `XCTestConfigurationFilePath == nil`, an environment variable.
    /// `open` hands the caller's environment to the app it launches, and a shell that had
    /// inherited that variable from an earlier test run launched an app that read itself as a
    /// test host and never started its model: no profile discovery, no polling, and a menu that
    /// said "asking the CLI which profile this is" for as long as the app ran, with no error to
    /// show because nothing had been asked. Requiring a non-empty value would not have helped:
    /// Xcode 26.6 sets that variable empty in a real test host, so the process running this test
    /// carries the same stale-looking values the polluted shell had. What separates the two is
    /// whether XCTest is loaded, which no inherited variable changes.
    func testAnAppWithoutXCTestLoadedIsNotATestRun() {
        XCTAssertFalse(LyrebirdApp.isHostingTests(xctestCase: nil))
    }
}
