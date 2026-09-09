import Foundation
import Testing

@testable import Lyrebird

/// The menu's Start and Stop shell out to the CLI, while its readings come over HTTP from the URL
/// in Settings. Nothing but the environment ties the two together, so these pin that knot.
struct ControlTests {

    @Test
    func aPortInTheControlURLReachesTheChildAsTheControlPort() {
        #expect(
            Control.controlEnvironment(for: URL(string: "http://127.0.0.1:9000")!) == ["LYREBIRD_CONTROL_PORT": "9000"])
    }

    @Test(
        arguments: [("http://127.0.0.1", "80"), ("https://127.0.0.1", "443")])
    func aurlWithNoPortResolvesToItsSchemeDefault(address: String, port: String) throws {
        let url = try #require(URL(string: address))
        #expect(Control.controlEnvironment(for: url) == ["LYREBIRD_CONTROL_PORT": port])
    }

    @Test
    func relaunchGoesThroughTheCLISoItTargetsTheDeviceUpRecorded() {
        // Relaunch used to shell out to `xcrun simctl launch booted <bundleid>`, which lets simctl
        // choose when two simulators are booted — so the button could relaunch the app on a device
        // that never received Lyrebird's CA. The device is the engine's to decide, and `booted`
        // must not appear anywhere in what the app runs.
        let argv = Control.relaunchCommand(bundleId: "com.example.Store")

        #expect(argv == ["relaunch", "com.example.Store"])
    }

    @Test func aConfiguredProfileIsPassedAheadOfTheCommand() {
        // A Finder-launched app inherits no shell environment, so the profile has to travel on the
        // command line — and ahead of the subcommand, which is where Click reads a group option.
        #expect(
            Control.arguments(
                Control.relaunchCommand(bundleId: "com.example.Store"),
                profile: "/tmp/lyrebird-profile") == [
                    "--profile", "/tmp/lyrebird-profile", "relaunch", "com.example.Store",
                ])
        #expect(Control.arguments(["up"], profile: "") == ["up"], "an unset profile must not become an empty --profile")
    }

    @Test
    func theChildKeepsTheInheritedEnvironmentAlongsideWhatWeSet() async throws {
        // Replacing the environment instead of merging would strip PATH, and the CLI — which
        // resolves its own tools through it — would fail for a reason unrelated to the port.
        let result = await Control.shell(
            "/bin/sh", ["-c", "env"],
            environment: ["LYREBIRD_CONTROL_PORT": "9999"])

        #expect(result.succeeded, Comment(rawValue: result.output))
        #expect(result.output.contains("LYREBIRD_CONTROL_PORT=9999"), Comment(rawValue: result.output))
        #expect(result.output.contains("PATH="), "the inherited environment was dropped")
    }
}
