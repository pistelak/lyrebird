import AppKit
import Foundation
import Testing

@testable import Lyrebird

extension AppTests {
    /// The scenario browser while the proxy is stopped: scenarios and rules read from files through
    /// `lyrebird scenario show`, labelled as a preview, and carrying none of a run's affordances.
    /// Serialized: these install a launcher and the control URL in the shared preferences.
    @MainActor
    struct FilePreviewTests {
        /// What `scenario show` prints for a profile holding the two fixture scenarios.
        static let payload =
            #"{"problems": [], "scenarios": ["#
            + #"{"name": "orders-outage", "overrideCount": 3, "verified": true, "#
            + #""notes": "what the app shows during the outage"}, "#
            + #"{"name": "checkout", "overrideCount": 2, "verified": false, "notes": ""}], "#
            + #""rules": {"orders-outage": \#(RulesFixture.snapshot), "checkout": \#(RulesFixture.browsed)}}"#

        /// The same profile with a third file that loaded nothing: its line is a top-level problem,
        /// because a file with no scenario has no `rules` entry to carry it.
        static let payloadWithABrokenFile = payload.replacingOccurrences(
            of: #""problems": []"#, with: #""problems": ["skipped broken.json: Expecting value: line 1 column 1"]"#)

        /// A profile whose `checkout.json` stopped loading between two polls.
        static let payloadMissingCheckout =
            #"{"problems": ["skipped checkout.json: Expecting value: line 1 column 1"], "#
            + #""scenarios": [{"name": "orders-outage", "overrideCount": 3, "verified": true, "notes": ""}], "#
            + #""rules": {"orders-outage": \#(RulesFixture.snapshot)}}"#

        static let unreadable =
            #"{"problems": ["cannot read /path/to/profile/scenarios: Permission denied"], "scenarios": [], "rules": {}}"#

        private func waitFor(_ description: String, _ condition: () -> Bool) async throws {
            let deadline = ContinuousClock.now.advanced(by: .seconds(5))
            while ContinuousClock.now < deadline {
                if condition() { return }
                try await Task.sleep(for: .milliseconds(20))
            }
            try #require(condition(), Comment(rawValue: description))
        }

        private func badge(_ controller: RulesWindowController) -> String? {
            (controller.window?.toolbar?.items.first { $0.itemIdentifier.rawValue == "status" }?.view
                as? StatusBadgeView)?.label.stringValue
        }

        /// The accessibility labels of the sidebar's scenario rows; an active row's ends in ", active".
        private func sidebarLabels(_ controller: RulesWindowController) -> [String] {
            let outline = controller.sidebar.outline
            return (0..<outline.numberOfRows).flatMap { row -> [String] in
                guard let view = outline.view(atColumn: 0, row: row, makeIfNecessary: true) else { return [] }
                return fields(view).compactMap { $0.accessibilityLabel() }
            }
        }

        private func buttons(_ view: NSView) -> [NSButton] {
            (view as? NSButton).map { [$0] } ?? view.subviews.flatMap(buttons)
        }

        private func rowTexts(_ controller: RulesWindowController) -> [String] {
            controller.requests.rows.map(\.text.string)
        }

        // MARK: - What the launcher printed means

        @Test
        func aPayloadWithNoScenariosIsAFailureCarryingItsReasonsNeverAnEmptyProfile() throws {
            // The command exits 1 whenever it could not show everything and prints the JSON either
            // way; the payload's own sentence is the one to show, not "exit status 1".
            #expect(
                AppModel.decodePreview(Self.unreadable)
                    == .unavailable("cannot read /path/to/profile/scenarios: Permission denied"))
            #expect(AppModel.decodePreview("") == .unavailable("`lyrebird scenario show` printed nothing"))
            #expect(
                AppModel.decodePreview("Traceback (most recent call last): boom\n")
                    == .unavailable("Traceback (most recent call last): boom"))

            // A warning around the JSON is normal: the launcher merges stderr into stdout.
            guard case .ok(let preview) = AppModel.decodePreview("warning: slow disk\n" + Self.payload + "\n") else {
                Issue.record("a well-formed payload read as something other than ok")
                return
            }
            #expect(preview.scenarios.map(\.name) == ["orders-outage", "checkout"])
            #expect(preview.rules.keys.sorted() == ["checkout", "orders-outage"])
            #expect(try #require(preview.rules["orders-outage"]).rules.count == 3)
        }

        // MARK: - When the model reads the files at all

        @Test
        func aStoppedProxyWithAWindowOpenReadsTheFilesThroughTheCLI() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                Config.defaults.set("/tmp/lyrebird-profile", forKey: Config.profilePathKey)
                let launcher = try spyLauncher(exiting: 0, printing: Self.payload)
                let model = makeModel(expecting: RulesFixture.ours)

                await model.windowAppeared()

                #expect(model.status == .down)
                // The whole line: the profile ahead of the subcommand, and the port the CLI needs.
                #expect(recordedCalls(launcher.calls) == "--profile /tmp/lyrebird-profile scenario show port=8088\n")
                guard case .ok(let preview) = model.previewRead else {
                    Issue.record(
                        "the window opened over a stopped proxy and previewed nothing: \(String(describing: model.previewRead))"
                    )
                    return
                }
                #expect(preview.scenarios.map(\.name) == ["orders-outage", "checkout"])
                #expect(model.scenarios == nil, "the preview never enters the live list")
            }
        }

        @Test
        func aClosedWindowNeverRunsThePreview() async throws {
            try await withAppTestEnvironment {
                // A spawn every poll for a window nobody is looking at is the same waste as polling a
                // closed window for its rules.
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                let launcher = try spyLauncher(exiting: 0, printing: Self.payload)
                let model = makeModel(expecting: RulesFixture.ours)

                await model.refresh()
                await model.refresh()

                #expect(model.status == .down)
                #expect(recordedCalls(launcher.calls) == "")
                #expect(model.previewRead == nil)
            }
        }

        @Test
        func aPreviewThatFailedCarriesTheEnginesReasonNotAnEmptyList() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                _ = try spyLauncher(exiting: 1, printing: Self.unreadable)
                let model = makeModel(expecting: RulesFixture.ours)

                await model.windowAppeared()

                #expect(model.previewRead == .unavailable("cannot read /path/to/profile/scenarios: Permission denied"))
            }
        }

        @Test
        func aPreviewThatFinishesAfterTheLastWindowClosedIsDropped() async throws {
            try await withAppTestEnvironment {
                // Closed mid-read: the payload arrives for nobody. Committed anyway, it was what the
                // next window rendered before its own read began — a stale preview under a fresh badge.
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                _ = try spyLauncher(exiting: 0, printing: Self.payload, delay: 0.5)
                let model = makeModel(expecting: RulesFixture.ours)
                model.windowOpened()
                let refresh = Task { await model.refresh() }
                try await Task.sleep(for: .milliseconds(100))  // the launcher is still sleeping

                model.windowClosed()
                await refresh.value

                #expect(model.status == .down, "the health reading is committed regardless")
                #expect(model.previewRead == nil, "a preview nobody asked to see was committed")
            }
        }

        @Test
        func theFirstLiveReadingReplacesThePreviewInTheSameCommit() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                _ = try spyLauncher(exiting: 0, printing: Self.payload)
                let model = makeModel(expecting: RulesFixture.ours)
                await model.windowAppeared()
                #expect(model.previewRead != nil)

                StubURLProtocol.install { request in RulesFixture.serve(request) }
                await model.refresh()

                #expect(model.status == .intercepting)
                #expect(
                    model.previewRead == nil, "a preview surviving into the live state would label live rules as files")
                #expect(model.scenarios?.active == "orders-outage")
            }
        }

        // MARK: - What the window shows

        @Test
        func aColdOpenedPreviewIsLabelledBrowsesItsFirstScenarioAndOffersNoActivation() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                _ = try spyLauncher(exiting: 1, printing: Self.payloadWithABrokenFile)
                let model = makeModel(expecting: RulesFixture.ours)
                let controller = RulesWindowController(model: model, restore: false)
                controller.showWindow(nil)
                defer { controller.close() }
                try await waitFor("the preview never arrived") { model.previewRead != nil }
                controller.render()

                // The label that does not scroll away with the list.
                #expect(badge(controller) == "stopped · file preview")
                // No active scenario to start from, so the first previewed one is browsed — and with
                // it the note, which is where a file that loaded nothing is named.
                #expect(controller.state.scenario == "orders-outage")
                try await waitFor("the browsed scenario's rules never rendered") {
                    rowTexts(controller).contains { $0.contains("/api/v1/orders") }
                }
                #expect(
                    rowTexts(controller).contains {
                        $0.contains("Preview from files") && $0.contains("skipped broken.json")
                    })
                // Both scenarios listed, neither marked active.
                let labels = sidebarLabels(controller)
                #expect(
                    labels.contains { $0.hasPrefix("orders-outage") } && labels.contains { $0.hasPrefix("checkout") })
                #expect(!labels.contains { $0.contains(", active") }, "\(labels)")
                // Activation is a live action: every way in is disabled on a preview row.
                #expect(!controller.canActivateSelection)
                let activate = try #require(buttons(controller.requests.view).first { $0.title == "Activate" })
                #expect(!activate.isEnabled)
                // Reload asks a running proxy to re-read its files; there is none. Start stays enabled:
                // it is how the reader leaves the preview.
                let toolbar = try #require(controller.window?.toolbar)
                let reload = try #require(toolbar.items.first { $0.itemIdentifier.rawValue == "reload" })
                let interception = try #require(toolbar.items.first { $0.itemIdentifier.rawValue == "interception" })
                reload.validate()
                interception.validate()
                #expect(!reload.isEnabled)
                #expect(interception.isEnabled)
                // The scenario's notes come from the previewed list, as they come from the live one.
                #expect(rowTexts(controller).contains { $0.contains("what the app shows during the outage") })
                // The detail pane shows the selected rule's configured response — the sequence rule
                // is first in the fixture — not the stopped-proxy vacancy.
                #expect(controller.detail.textView.string.contains("/api/v1/items"))
                #expect(!controller.detail.textView.string.contains("Proxy is not running."))

                // Recent is run state and keeps saying so.
                controller.select(.recent)
                #expect(rowTexts(controller).contains { $0.contains("Proxy is not running.") })
                #expect(controller.detail.textView.string.contains("Proxy is not running."))
            }
        }

        @Test
        func aFailedPreviewAfterALiveBrowseShowsTheReasonAndNoRowMarkedActive() async throws {
            try await withAppTestEnvironment {
                // Live first, so the model remembers a list — the greyed one a stopped proxy leaves
                // behind, active marker and all. Under "file preview" that marker would name a run
                // the files do not have, so a failed preview shows the failure and nothing else.
                StubURLProtocol.install { request in RulesFixture.serve(request) }
                // Installed first: the launcher path is part of the settings the model was discovered
                // under, and a refresh under changed settings is dropped until rediscovery.
                _ = try spyLauncher(exiting: 1, printing: Self.unreadable)
                let model = makeModel(expecting: RulesFixture.ours)
                let controller = RulesWindowController(model: model, restore: false)
                controller.showWindow(nil)
                defer { controller.close() }
                try await waitFor("the live rules never arrived") { model.rulesRead != nil }
                controller.render()
                #expect(
                    sidebarLabels(controller).contains { $0.contains(", active") }, "live, the active row is marked")

                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                await model.refresh()
                controller.render()

                #expect(model.lastScenarios != nil, "the fallback exists, and is not what is shown")
                #expect(badge(controller) == "stopped · file preview")
                let labels = sidebarLabels(controller)
                #expect(!labels.contains { $0.contains(", active") }, "\(labels)")
                #expect(!labels.contains { $0.hasPrefix("orders-outage") }, "\(labels)")
                #expect(
                    rowTexts(controller).contains {
                        $0.contains("The scenario files could not be read.") && $0.contains("Permission denied")
                    }, "\(rowTexts(controller))")
                #expect(controller.detail.textView.string.contains("Permission denied"))
                #expect(!controller.detail.textView.string.contains("Proxy is not running."))
            }
        }

        @Test
        func aBrowsedScenarioWhoseFileStoppedLoadingNamesTheParsersLine() async throws {
            try await withAppTestEnvironment {
                // The selection is kept, as it is live; the payload no longer has the scenario. An
                // unavailable read takes the vacancy branch, which never shows the preview note, so
                // the parser's line has to travel in the reason itself.
                StubURLProtocol.install { _ in throw URLError(.cannotConnectToHost) }
                let model = makeModel(expecting: RulesFixture.ours)
                await model.browse("checkout")
                guard case .ok(let preview) = AppModel.decodePreview(Self.payloadMissingCheckout) else {
                    Issue.record("the fixture did not decode")
                    return
                }
                model.previewRead = .ok(preview)

                let content = BrowserContent(model: model)

                #expect(
                    content.rulesRead
                        == .unavailable(
                            "no scenario checkout in the profile's files\n"
                                + "skipped checkout.json: Expecting value: line 1 column 1"))
                let state = BrowserState()
                state.select(.scenario("checkout"))
                let list = RequestListController()
                let detail = RuleDetailController()
                list.update(content, state: state)
                detail.update(content, state: state)
                #expect(list.rows.contains { $0.text.string.contains("skipped checkout.json") })
                #expect(detail.textView.string.contains("skipped checkout.json"))
                #expect(!detail.textView.string.contains("Proxy is not running."))
            }
        }
    }
}
