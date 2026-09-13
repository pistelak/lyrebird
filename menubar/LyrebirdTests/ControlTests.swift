import Foundation
import Testing

@testable import Lyrebird

/// The menu's Start and Stop shell out to the CLI, while its readings come over HTTP from the URL
/// in Settings. Nothing but the environment ties the two together, so these pin that knot.
struct ControlTests {

    /// A launcher can exit nonzero and say nothing — a wrong path, a killed child, output that is
    /// not UTF-8. That used to leave `lastError` an empty string, and both places that show an
    /// action failure drop an empty message, so Start or Relaunch failed and the app showed nothing
    /// at all. The exit status is the one thing always available to say instead.
    @Test
    func aCommandThatFailedSilentlyStillSaysSomethingTheDisplayKeeps() {
        for output in ["", "   ", " \n\t "] {
            let result = Control.Result(output: output, status: 3)
            let failure = result.failure
            #expect(failure?.isEmpty == false, "a failure with output \(output.debugDescription) said nothing")
            #expect(failure?.contains("3") == true, "the exit status is the only detail there is")
            // The displays are what dropped it before, so the message has to survive them too.
            #expect(RuleFormatting.actionFailure(failure) != nil)
        }
        #expect(
            Control.Result(output: "could not find the launcher", status: 1).failure == "could not find the launcher")
        #expect(Control.Result(output: "", status: 0).failure == nil, "a command that worked has no failure")
    }

    @Test(arguments: [
        "http://127.0.0.1", "https://127.0.0.1:8088", "http://example.com:8088",
    ])
    func controlEnvironmentRejectsEndpointsTheCLICannotControl(address: String) throws {
        let url = try #require(URL(string: address))
        #expect(throws: Config.ControlURLProblem.self) {
            try Control.controlEnvironment(for: url)
        }
    }

    @Test func anUnsetProfileMustNotBecomeAnEmptyProfileOption() {
        #expect(Control.arguments(["up"], profile: "") == ["up"])
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

/// The wiring itself, not the helpers it is built from: each of these runs the real `Control` call
/// against a launcher that records its argv and environment, so the production path could not be
/// deleted with the test staying green. They share preferences, hence the serialized suite.
extension AppTests {
    @MainActor
    struct ControlWiringTests {
        @Test
        func aPortInTheControlURLReachesTheChildAsTheControlPort() async throws {
            try await withAppTestEnvironment {
                Config.defaults.set("http://127.0.0.1:9000", forKey: Config.controlURLKey)
                let launcher = try spyLauncher(exiting: 0)

                let result = await Control.up()

                #expect(result.failure == nil)
                // The whole line: `contains` would also pass a subcommand this is not.
                #expect(recordedCalls(launcher.calls) == "up port=9000\n")
            }
        }

        @Test
        func relaunchGoesThroughTheCLISoItTargetsTheDeviceUpRecorded() async throws {
            try await withAppTestEnvironment {
                // Relaunch used to shell out to `xcrun simctl launch booted <bundleid>`, which lets
                // simctl choose when two simulators are booted — so the button could relaunch the app
                // on a device that never received Lyrebird's CA. The device is the engine's to decide,
                // and `booted` must not appear anywhere in what the app runs.
                Config.defaults.set("http://127.0.0.1:9001", forKey: Config.controlURLKey)
                let launcher = try spyLauncher(exiting: 0)

                _ = await Control.relaunch(bundleId: "com.example.Store")

                // Exactly this, port included: no `booted`, no `simctl`, and the port the CLI needs
                // to find the device `up` recorded.
                #expect(recordedCalls(launcher.calls) == "relaunch com.example.Store port=9001\n")
            }
        }

        @Test
        func aConfiguredProfileIsPassedAheadOfTheCommand() async throws {
            try await withAppTestEnvironment {
                // A Finder-launched app inherits no shell environment, so the profile has to travel on
                // the command line — and ahead of the subcommand, which is where Click reads a group
                // option.
                Config.defaults.set("/tmp/lyrebird-profile", forKey: Config.profilePathKey)
                let launcher = try spyLauncher(exiting: 0)

                _ = await Control.up()

                #expect(recordedCalls(launcher.calls) == "--profile /tmp/lyrebird-profile up port=8088\n")
            }
        }
    }
}
