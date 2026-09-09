import Testing

@testable import Lyrebird

struct LaunchEnvironmentTests {
    // The hosted runner must be detected even when every test uses Swift Testing, so the app
    // does not start polling the real control port during tests.
    @Test func aHostedSwiftTestingRunIsDetected() {
        #expect(LyrebirdApp.isHostingTests())
    }

    // An inherited XCTestConfigurationFilePath cannot make an ordinary app launch a test host.
    @Test
    func anAppWithoutTheTestHarnessLoadedIsNotATestRun() {
        #expect(!LyrebirdApp.isHostingTests(xctestCase: nil))
    }
}
