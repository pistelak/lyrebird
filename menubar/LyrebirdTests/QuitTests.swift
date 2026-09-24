import AppKit
import Foundation
import Testing

@testable import Lyrebird

/// The menu-bar icon is the only sign on screen that the Mac's proxy settings are rewritten, so
/// Quit stops this profile's running proxy before it goes — and stays open, with the reason in the
/// menu, when it could not. It never stops what it did not observe as this profile's: on the shared
/// port a journal may be another profile's, and `down` consumes whichever exists. Serialized: these
/// install a launcher and URL protocol stubs in the shared preferences.
extension AppTests {
    @MainActor
    struct QuitTests {
        private func answer(fingerprint: String) {
            StubURLProtocol.install { request in RulesFixture.serve(request, fingerprint: fingerprint) }
        }

        private func waitUntilBusy(_ model: AppModel) async throws {
            for _ in 0..<400 where !model.busy {
                try await Task.sleep(for: .milliseconds(5))
            }
            try #require(model.busy, "the action never started")
        }

        @Test
        func aQuitWhoseDownFailedKeepsTheAppOpen() async throws {
            try await withAppTestEnvironment {
                // A "Stop and Quit" that stopped nothing must not vanish: the proxy would go on
                // intercepting with the one sign of it gone from the screen.
                answer(fingerprint: RulesFixture.ours)
                let launcher = try spyLauncher(exiting: 1, printing: "✗ the PAC on 'Wi-Fi' could not be read")
                let model = makeModel(expecting: RulesFixture.ours)

                let quits = await model.prepareToQuit()

                #expect(!quits)
                #expect(recordedCalls(launcher.calls) == "down port=8088\n")
                #expect(
                    model.lastError?.contains("could not be read") == true, Comment(rawValue: model.lastError ?? "nil"))
                #expect(!model.busy)
            }
        }

        @Test
        func aQuitOverThisProfilesProxyStopsItFirst() async throws {
            try await withAppTestEnvironment {
                answer(fingerprint: RulesFixture.ours)
                let launcher = try spyLauncher(exiting: 0, printing: "stopped")
                let model = makeModel(expecting: RulesFixture.ours)

                #expect(await model.prepareToQuit())
                #expect(recordedCalls(launcher.calls) == "down port=8088\n")
            }
        }

        @Test
        func aQuitStopsNothingItDidNotObserveAsThisProfiles() async throws {
            try await withAppTestEnvironment {
                let launcher = try spyLauncher(exiting: 0)
                let stale = Control.StatusReading(
                    fingerprint: RulesFixture.ours, proxyUp: false, pacOurs: true, pacEnabled: true)

                // Another profile's proxy holds the port.
                answer(fingerprint: RulesFixture.theirs)
                var model = makeModel(expecting: RulesFixture.ours)
                #expect(await model.prepareToQuit())
                try #require(model.status == .foreignProfile(running: RulesFixture.theirs))

                // Something answered and could not be understood.
                StubURLProtocol.install { request in (Stub.response(request, 500), Data("boom".utf8)) }
                model = makeModel(expecting: RulesFixture.ours)
                #expect(await model.prepareToQuit())
                guard case .unreadable = model.status else {
                    Issue.record("expected unreadable, got \(model.status)")
                    return
                }

                // A dead session on the shared port: its journal may be another profile's, so the
                // authorization to run `down` over it stays with an explicit Stop.
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                model = makeModel(expecting: RulesFixture.ours, discover: { stale })
                await model.refresh()
                try #require(model.status == .stale)
                #expect(await model.prepareToQuit())

                // This profile's proxy, and the user chose to leave it running.
                answer(fingerprint: RulesFixture.ours)
                model = makeModel(expecting: RulesFixture.ours)
                #expect(await model.prepareToQuit(leaveProxy: true))

                #expect(
                    !recordedCalls(launcher.calls).contains("down"), Comment(rawValue: recordedCalls(launcher.calls)))
            }
        }

        @Test
        func aSecondQuitWhileTheFirstIsDecidingGetsOneReply() async throws {
            try await withAppTestEnvironment {
                // AppKit is owed exactly one reply per termination request. A second ⌘Q during
                // the first's `down` used to start a second decision, which saw the first's
                // `busy`, answered "no", and left the first to answer again — after an apparent
                // refusal, possibly with "yes".
                answer(fingerprint: RulesFixture.ours)
                _ = try spyLauncher(exiting: 1, printing: "✗ the PAC on 'Wi-Fi' could not be read", delay: 0.5)
                let model = makeModel(expecting: RulesFixture.ours)
                let delegate = AppDelegate(model: model)
                let replies = Replies()
                delegate.replyToTermination = { replies.record($0) }

                #expect(delegate.applicationShouldTerminate(NSApp) == .terminateLater)
                #expect(delegate.applicationShouldTerminate(NSApp) == .terminateCancel)
                for _ in 0..<600 where replies.recorded.isEmpty {
                    try await Task.sleep(for: .milliseconds(10))
                }
                try await Task.sleep(for: .milliseconds(100))  // room for a second reply to show up

                #expect(replies.recorded == [false], "one request, one answer — the failed `down`'s")
                #expect(delegate.applicationShouldTerminate(NSApp) == .terminateLater, "the next request is its own")
                for _ in 0..<600 where replies.recorded.count < 2 {
                    try await Task.sleep(for: .milliseconds(10))
                }
                #expect(replies.recorded.count == 2)
            }
        }

        @Test
        func aLeaveChoiceMadeDuringAPendingQuitIsNotConsumedByTheNextPlainQuit() async throws {
            try await withAppTestEnvironment {
                // Plain Quit starts a `down` that will fail; while it runs the user picks "Quit,
                // leave the proxy running". That request is declined — but its choice, kept as
                // state, used to be spent by the next plain ⌘Q, which then quit without `down`.
                answer(fingerprint: RulesFixture.ours)
                let launcher = try spyLauncher(
                    exiting: 1, printing: "✗ the PAC on 'Wi-Fi' could not be read", delay: 0.3)
                let delegate = AppDelegate(model: makeModel(expecting: RulesFixture.ours))
                let replies = Replies()
                delegate.replyToTermination = { replies.record($0) }
                let declined = Replies()
                delegate.requestTermination = {
                    declined.record(delegate.applicationShouldTerminate(NSApp) == .terminateCancel)
                }

                #expect(delegate.applicationShouldTerminate(NSApp) == .terminateLater)
                delegate.quitLeavingProxy()
                #expect(declined.recorded == [true], "declined: a request is pending")
                for _ in 0..<600 where replies.recorded.isEmpty {
                    try await Task.sleep(for: .milliseconds(10))
                }
                try #require(replies.recorded == [false])

                #expect(delegate.applicationShouldTerminate(NSApp) == .terminateLater)
                for _ in 0..<600 where replies.recorded.count < 2 {
                    try await Task.sleep(for: .milliseconds(10))
                }

                // The second plain Quit ran `down` again: the stale choice did not skip it.
                #expect(recordedCalls(launcher.calls) == "down port=8088\ndown port=8088\n")
                #expect(replies.recorded == [false, false])
            }
        }

        @Test
        func theAlternateQuitRaisesOneRequestThatLeavesTheProxyRunning() async throws {
            try await withAppTestEnvironment {
                // The choice has to reach the request: a delegate that dropped it while still
                // raising termination would run `down` under the item that promised not to, and
                // the leak test above cannot tell — it only proves the choice is not reused.
                answer(fingerprint: RulesFixture.ours)
                let launcher = try spyLauncher(exiting: 0)
                let delegate = AppDelegate(model: makeModel(expecting: RulesFixture.ours))
                let replies = Replies()
                delegate.replyToTermination = { replies.record($0) }
                let requests = Replies()
                delegate.requestTermination = {
                    requests.record(delegate.applicationShouldTerminate(NSApp) == .terminateLater)
                }

                delegate.quitLeavingProxy()
                for _ in 0..<600 where replies.recorded.isEmpty {
                    try await Task.sleep(for: .milliseconds(10))
                }

                #expect(requests.recorded == [true], "exactly one request, and it was accepted")
                #expect(replies.recorded == [true], "the app may quit")
                #expect(
                    !recordedCalls(launcher.calls).contains("down"), Comment(rawValue: recordedCalls(launcher.calls)))
            }
        }

        /// Records replies off whichever thread delivers them.
        final class Replies: @unchecked Sendable {
            private let lock = NSLock()
            private var replies: [Bool] = []
            var recorded: [Bool] {
                lock.lock()
                defer { lock.unlock() }
                return replies
            }
            func record(_ reply: Bool) {
                lock.lock()
                replies.append(reply)
                lock.unlock()
            }
        }

        @Test
        func aQuitDuringAnActionIsRefused() async throws {
            try await withAppTestEnvironment {
                // Start then ⌘Q: the status is still "stopped" while `up` runs, so a decision made
                // now would quit under a proxy about to come up. Refused, not waited for — `up`
                // has no deadline the app controls.
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                let launcher = try spyLauncher(exiting: 0, delay: 1)
                let model = makeModel(expecting: RulesFixture.ours, discover: { .init(fingerprint: RulesFixture.ours) })
                let action = Task { await model.toggle() }
                try await waitUntilBusy(model)

                let quits = await model.prepareToQuit()

                #expect(!quits)
                #expect(model.lastError?.contains("still running") == true, Comment(rawValue: model.lastError ?? "nil"))
                await action.value
                // Read once the action is over: the launcher records its line only after it starts.
                #expect(recordedCalls(launcher.calls) == "up port=8088\n", "nothing was stopped underneath the action")

                // Once the action is over, a quit decides on what it left: nothing running here.
                #expect(await model.prepareToQuit())
                #expect(!recordedCalls(launcher.calls).contains("down"))
            }
        }
    }
}
