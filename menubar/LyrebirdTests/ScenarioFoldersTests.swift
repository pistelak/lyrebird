import Foundation
import Testing

@testable import Lyrebird

/// Scenarios may live one folder down, and the folder is part of the name. Two places read that
/// differently on purpose: the window shows the whole tree, the menu shows only the folder the
/// active scenario is in. These pin the splitting itself, the cases where "the active scenario's
/// folder" has no obvious answer, and the reload the window offers for a profile edited outside it.
extension AppTests {
    @MainActor
    struct ScenarioFoldersTests {
        private func summary(_ name: String, overrides: Int = 0, verified: Bool = false) -> ScenarioSummary {
            ScenarioSummary(name: name, overrideCount: overrides, verified: verified, notes: nil)
        }

        private func list(active: String, _ names: [String]) -> ScenarioList {
            ScenarioList(active: active, scenarios: names.map { summary($0) })
        }

        // MARK: - Splitting a name

        @Test
        func aNameWithNoFolderIsAtTheRootAndKeepsItsWholeName() {
            let scenario = summary("orders-outage")
            #expect(scenario.group.isEmpty)
            #expect(scenario.leaf == "orders-outage")
        }

        @Test
        func exactlyOneFolderIsStrippedFromALeaf() {
            let scenario = summary("checkout/orders-outage")
            #expect(scenario.group == "checkout")
            #expect(scenario.leaf == "orders-outage")
        }

        @Test
        func aPayloadWithoutAGroupFieldStillFolders() throws {
            // The engine sends `group`, but the app derives it: decoding it would add a second
            // source for something the name already says, and an engine older than the field would
            // then flatten the whole tree into one nameless folder.
            let payload =
                #"{"active":"checkout/x","scenarios":[{"name":"checkout/x","overrideCount":1,"verified":false}]}"#
            let decoded = try JSONDecoder().decode(ScenarioList.self, from: Data(payload.utf8))

            #expect(decoded.scenarios.first?.group == "checkout")
        }

        @Test
        func foldersPutsRootScenariosFirstAndSortsTheFolders() {
            let subject = list(active: "default", ["default", "checkout/a", "archive/b", "scratch", "checkout/c"])

            let folders = subject.folders()

            #expect(folders.root.map(\.name) == ["default", "scratch"])
            #expect(folders.groups.map(\.name) == ["archive", "checkout"], "folders are sorted by name")
            #expect(folders.groups.last?.scenarios.map(\.name) == ["checkout/a", "checkout/c"])
        }

        // MARK: - What the menu shows

        @Test
        func theMenuShowsTheActiveScenariosFolderAndNamesIt() {
            let subject = list(active: "checkout/a", ["default", "checkout/a", "checkout/c", "archive/b"])

            let shown = subject.shownFolder()

            #expect(shown.scenarios.map(\.name) == ["checkout/a", "checkout/c"])
            #expect(shown.caption == "checkout/", "a menu showing part of a profile must say which part")
        }

        @Test
        func theMenuShowsTheRootWithNoCaptionWhenTheActiveScenarioIsThere() {
            let subject = list(active: "default", ["default", "scratch", "checkout/a"])

            let shown = subject.shownFolder()

            #expect(shown.scenarios.map(\.name) == ["default", "scratch"])
            #expect(shown.caption == nil)
        }

        @Test
        func aFolderHoldingOnlyTheActiveScenarioStillListsIt() {
            let subject = list(active: "checkout/only", ["default", "checkout/only"])

            let shown = subject.shownFolder()

            #expect(shown.scenarios.map(\.name) == ["checkout/only"])
        }

        @Test
        func anActiveNameAbsentFromTheListFallsBackToTheRootWithoutClaimingAFolder() {
            // The scenario was deleted between two polls. Caption and rows come from one call for
            // exactly this case: derived apart, the caption could read `checkout/` above rows taken
            // from the root — a menu saying it shows one thing while showing another.
            let subject = list(active: "checkout/gone", ["default", "scratch", "archive/b"])

            let shown = subject.shownFolder()

            #expect(shown.scenarios.map(\.name) == ["default", "scratch"])
            #expect(shown.caption?.contains("not in this profile") == true, "\(shown.caption ?? "nil")")
            #expect(shown.caption?.contains("checkout/gone") == true)
        }

        @Test
        func aMissingActiveScenarioWithAnEmptyRootReportsRatherThanRenderingNothing() {
            let subject = list(active: "checkout/gone", ["archive/b"])

            let shown = subject.shownFolder()

            #expect(shown.scenarios.isEmpty)
            #expect(shown.caption != nil, "an empty list with no caption reads as a profile with no scenarios")
        }

        // MARK: - Reload

        @Test
        func reloadPostsToTheReloadRouteWithAJsonBody() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    guard request.httpMethod == "POST" else { return Stub.read(request) }
                    return (Stub.response(request, 200), Data(#"{"active":"default","reloaded":2}"#.utf8))
                }

                try await Stub.makeClient().reloadScenarios()

                let request = try #require(StubURLProtocol.requests.first)
                #expect(request.httpMethod == "POST")
                #expect(request.url?.path == "/__mock__/scenarios/reload")
                #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
            }
        }

        @Test
        func aRefusedReloadCarriesTheEnginesDetailReleasesBusyAndStillRefreshes() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in
                    guard request.httpMethod == "POST" else { return Stub.read(request) }
                    return (
                        Stub.response(request, 409),
                        Data(
                            #"{"error":"reload_refused","detail":"skipped broken.json: Expecting value"}"#.utf8)
                    )
                }
                let model = makeModel(expecting: "a1b2c3")

                await model.reloadScenarios()

                let lastError = model.lastError ?? ""
                // The engine's sentence, not the slug: a refused reload names the file to fix, and
                // "reload failed" on its own sends the reader nowhere.
                #expect(lastError.contains("broken.json"), "\(lastError)")
                #expect(model.busy == false, "a refusal must not leave every other action disabled")
                // Asserting on `lastError` alone would pass with the refresh missing, leaving the
                // window showing the list that produced the refusal.
                #expect(
                    StubURLProtocol.requests.contains { $0.httpMethod == "GET" },
                    "the reload is followed by a refresh, so the window corrects itself in the same tick")
            }
        }

        @Test
        func aReloadThisAppCannotScopeToAProfileSendsNothing() async throws {
            try await withAppTestEnvironment {
                StubURLProtocol.install { request in Stub.read(request) }
                // No expected fingerprint: the app cannot say which profile a proxy on this port is
                // running, and a reload sent anyway would re-read somebody else's.
                let model = makeModel(expecting: nil)

                await model.reloadScenarios()

                #expect(model.lastError?.isEmpty == false)
                #expect(
                    !StubURLProtocol.requests.contains { $0.httpMethod == "POST" },
                    "an unscoped write must not reach the network at all")
            }
        }
    }
}
