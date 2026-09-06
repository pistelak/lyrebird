import XCTest

@testable import Lyrebird

/// The menu's Start and Stop shell out to the CLI, while its readings come over HTTP from the URL
/// in Settings. Nothing but the environment ties the two together, so these pin that knot.
final class ControlTests: XCTestCase {

    func testAPortInTheControlURLReachesTheChildAsTheControlPort() {
        XCTAssertEqual(Control.controlEnvironment(for: URL(string: "http://127.0.0.1:9000")!),
                       ["LYREBIRD_CONTROL_PORT": "9000"])
    }

    func testAURLWithNoPortResolvesToItsSchemeDefaultRatherThanTheEnginesDefault() {
        // Guessing 8088 here would send Start to a proxy the menu never reads from — the same
        // split this change exists to close.
        XCTAssertEqual(Control.controlEnvironment(for: URL(string: "http://127.0.0.1")!),
                       ["LYREBIRD_CONTROL_PORT": "80"])
        XCTAssertEqual(Control.controlEnvironment(for: URL(string: "https://127.0.0.1")!),
                       ["LYREBIRD_CONTROL_PORT": "443"])
    }

    func testTheChildKeepsTheInheritedEnvironmentAlongsideWhatWeSet() async throws {
        // Replacing the environment instead of merging would strip PATH, and the CLI — which
        // resolves its own tools through it — would fail for a reason unrelated to the port.
        let result = await Control.shell("/bin/sh", ["-c", "env"],
                                         environment: ["LYREBIRD_CONTROL_PORT": "9999"])

        XCTAssertTrue(result.succeeded, result.output)
        XCTAssertTrue(result.output.contains("LYREBIRD_CONTROL_PORT=9999"), result.output)
        XCTAssertTrue(result.output.contains("PATH="), "the inherited environment was dropped")
    }
}
