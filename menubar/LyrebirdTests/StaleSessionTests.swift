import Foundation
import Testing

@testable import Lyrebird

/// A proxy that died with its PAC installed — a crash, a `kill -9`, a bad sleep — leaves the Mac
/// routed at a port nothing serves until `lyrebird down` runs. The health poll reads that as a
/// refused connection, and the menu used to render it as "Stopped" beside a Start that `up`
/// refuses over the session's journal: the one state that needed Stop had no Stop. The engine's
/// `status --json` can tell the two apart, and these pin when the app asks it and what it shows.
/// Serialized: they install a launcher and URL protocol stubs in the shared preferences.
extension AppTests {
    @MainActor
    struct StaleSessionTests {
        /// What `status --json` prints for a dead session whose PAC is still installed, as the
        /// engine prints it: exit 1, the config-derived fingerprint, and the PAC object.
        static let deadSession = """
            {
              "proxyUp": false,
              "intercepting": false,
              "profileMismatch": false,
              "profileFingerprint": "\(Fixture.ours)",
              "runningProfileFingerprint": null,
              "pacError": null,
              "journalError": null,
              "service": "Wi-Fi",
              "simulator": null,
              "pac": {"url": "http://127.0.0.1:8088/proxy.pac", "enabled": true, "ours": true}
            }
            """

        /// Counts the probes a model makes and answers each with whatever the test set last.
        actor Probes {
            private(set) var count = 0
            var reading: Control.StatusReading

            init(_ reading: Control.StatusReading) { self.reading = reading }

            func set(_ reading: Control.StatusReading) { self.reading = reading }

            func probe() -> Control.StatusReading {
                count += 1
                return reading
            }
        }

        static let clean = Control.StatusReading(
            fingerprint: Fixture.ours, proxyUp: false, pacOurs: false, pacEnabled: false)
        static let stale = Control.StatusReading(
            fingerprint: Fixture.ours, proxyUp: false, pacOurs: true, pacEnabled: true)

        private func refuseConnections() {
            StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
        }

        private func answerAsOurs() {
            StubURLProtocol.install { request in RulesFixture.serve(request) }
        }

        // MARK: - What the state says

        @Test
        func aDeadSessionShowsStopNotStopped() async throws {
            try await withAppTestEnvironment {
                // The real path: the launcher prints what the engine prints and `Control` decodes
                // it, so the parsing is covered by the behaviour it exists for.
                refuseConnections()
                Config.defaults.set("/tmp/lyrebird-profile", forKey: Config.profilePathKey)
                let launcher = try spyLauncher(exiting: 1, printing: Self.deadSession)
                let model = makeModel(expecting: nil)

                await model.discoverProfile()
                await model.refresh()

                #expect(model.expectedFingerprint == Fixture.ours, "the reading still names the profile")
                #expect(model.status == .stale)
                #expect(model.stopsRatherThanStarts, "the button has to be Stop: `up` is refused over the journal")
                #expect(model.statusLine.contains("press Stop"), Comment(rawValue: model.statusLine))
                #expect(model.simBundleId == nil, "no proxy of ours to relaunch against")
                #expect(model.scenariosPlaceholder == "proxy not running")
                #expect(model.lastError == nil, "a probe is an observation, not a failed command")
                #expect(
                    recordedCalls(launcher.calls).split(separator: "\n").allSatisfy {
                        $0 == "--profile /tmp/lyrebird-profile status --json port=8088"
                    }, Comment(rawValue: recordedCalls(launcher.calls)))
            }
        }

        @Test
        func aReadingWithoutAPACIsNotStaleAndStillNamesTheProfile() throws {
            // The engine prints `pac: null` with a `pacError` when `networksetup` could not be
            // asked. A reading that required the PAC fields would turn a valid fingerprint into
            // "profile unknown"; defaulting them to false would claim a clean stop unseen.
            let printed =
                #"{"profileFingerprint":"\#(Fixture.ours)","proxyUp":false,"pac":null,"pacError":"timed out"}"#
            let reading = try Control.statusReading(from: Control.Result(output: printed, status: 1))

            #expect(reading.fingerprint == Fixture.ours)
            #expect(reading.proxyUp == false)
            #expect(reading.pacOurs == nil && reading.pacEnabled == nil, "missing is nil, never false")
            #expect(!reading.routesToADeadProxy)
        }

        // MARK: - When the probe runs

        @Test
        func theProbeRunsOnTheTransitionAndWhileStaleOnly() async throws {
            try await withAppTestEnvironment {
                let probes = Probes(Self.clean)
                refuseConnections()
                let model = makeModel(expecting: nil, discover: { await probes.probe() })

                await model.discoverProfile()
                #expect(await probes.count == 1)

                // The first poll after discovery probes: a proxy can die between the two.
                await model.refresh()
                #expect(await probes.count == 2)
                #expect(model.status == .down)

                // A poll that stays stopped does not: this is the state an idle machine sits in
                // all day, and a Python spawn every two seconds is the cost the schedule avoids.
                await model.refresh()
                await model.refresh()
                #expect(await probes.count == 2)

                // The proxy comes up, then dies with its PAC installed: the transition probes.
                answerAsOurs()
                await model.refresh()
                #expect(model.status == .intercepting)
                #expect(await probes.count == 2)
                await probes.set(Self.stale)
                refuseConnections()
                await model.refresh()
                #expect(await probes.count == 3)
                #expect(model.status == .stale)

                // While stale, every poll probes, so a `down` run from a terminal clears the
                // banner within a poll.
                await model.refresh()
                #expect(await probes.count == 4)
                #expect(model.status == .stale)
                await probes.set(Self.clean)
                await model.refresh()
                #expect(await probes.count == 5)
                #expect(model.status == .down)
                await model.refresh()
                #expect(await probes.count == 5)
            }
        }

        @Test
        func aProbeThatFailedFallsBackToStoppedWithoutAnError() async throws {
            try await withAppTestEnvironment {
                refuseConnections()
                let launcher = try spyLauncher(exiting: 1, printing: Self.deadSession)
                let model = makeModel(expecting: nil)
                await model.discoverProfile()
                await model.refresh()
                try #require(model.status == .stale)

                // The launcher goes away between two polls: the next probe cannot read anything.
                try FileManager.default.removeItem(atPath: launcher.path)
                await model.refresh()

                #expect(model.status == .down, "a failed probe is not a reading, and not a guess at stale")
                #expect(model.lastError == nil, "a background observation is not a command the user ran")
            }
        }

        // MARK: - Stop from the stale state

        @Test
        func stopFromTheStaleStateRunsDownAndKeepsItsFailure() async throws {
            try await withAppTestEnvironment {
                refuseConnections()
                let probes = Probes(Self.stale)
                let launcher = try spyLauncher(exiting: 1, printing: "✗ the PAC on 'Wi-Fi' could not be read")
                let model = makeModel(expecting: nil, discover: { await probes.probe() })
                await model.discoverProfile()
                await model.refresh()
                try #require(model.status == .stale)

                await model.toggle()

                #expect(recordedCalls(launcher.calls) == "down port=8088\n")
                #expect(
                    model.lastError?.contains("could not be read") == true, Comment(rawValue: model.lastError ?? "nil"))
                #expect(model.status == .stale, "the reading after a failed `down` still says so")
            }
        }
    }
}

private enum Fixture {
    static let ours = "a1b2c3"
}
