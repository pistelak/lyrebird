import SwiftUI
import XCTest

@testable import Lyrebird

/// What the window says when it is not describing a rule: the header, the toolbar, the states where
/// there is nothing to list and why, and the search that narrows what there is.
/// `ScenarioOutlineTests` covers the lines a rule is described with.
final class RuleFormattingTests: XCTestCase {

    // MARK: - What the header and the toolbar say

    func testTheToolbarNamesTheActiveScenarioAndNotTheBrowsedOne() {
        // The toolbar says what is answering requests; the column below says what is being looked
        // at. One line doing both is how a reader comes to believe a browsed scenario is live.
        XCTAssertEqual(
            RuleFormatting.statusItem(status: .intercepting, activeScenario: "orders-outage"),
            "intercepting · orders-outage")
        XCTAssertEqual(
            RuleFormatting.statusItem(status: .pacDisabled, activeScenario: "orders-outage"),
            "not intercepting · orders-outage")
        XCTAssertEqual(
            RuleFormatting.statusItem(status: .down, activeScenario: nil), "stopped",
            "no scenario is named when there is no reading of ours to name one from")
        XCTAssertEqual(
            RuleFormatting.statusItem(status: .foreignProfile(running: RulesFixture.theirs), activeScenario: nil),
            "another profile")
    }

    // MARK: - Numbers

    func testByteSizesReadInTheUnitsTheEngineCountedThemIn() {
        XCTAssertEqual(RuleFormatting.byteSize(0), "0 B")
        XCTAssertEqual(RuleFormatting.byteSize(512), "512 B")
        XCTAssertEqual(RuleFormatting.byteSize(1023), "1023 B")
        XCTAssertEqual(RuleFormatting.byteSize(1229), "1.2 KB")
        XCTAssertEqual(RuleFormatting.byteSize(3 * 1024 * 1024), "3.0 MB")
    }

    func testAStatusIsGreenBelowFourHundredAndRedFromThereUp() {
        XCTAssertEqual(RuleFormatting.statusColor(204), RuleFormatting.success)
        XCTAssertEqual(RuleFormatting.statusColor(399), RuleFormatting.success)
        XCTAssertEqual(RuleFormatting.statusColor(400), RuleFormatting.danger)
        XCTAssertEqual(RuleFormatting.statusColor(503), RuleFormatting.danger)
    }

    func testEveryTintTheWindowUsesIsOneTheAppearanceResolves() {
        // SwiftUI's `.red`, `.green` and `.orange` are fixed sRGB values: they are the same colour
        // in both appearances, and the red one sits near 3:1 on a dark pane. AppKit's system
        // colours are resolved against the appearance the view is drawn in, and follow Increase
        // Contrast with it, which is what makes the dark renders legible.
        XCTAssertEqual(RuleFormatting.danger, Color(nsColor: .systemRed))
        XCTAssertEqual(RuleFormatting.success, Color(nsColor: .systemGreen))
        XCTAssertEqual(RuleFormatting.warning, Color(nsColor: .systemOrange))
        XCTAssertNotEqual(RuleFormatting.danger, .red, "a fixed sRGB red does not adapt to the appearance")
    }

    // MARK: - Nothing to show, and why

    func testEachReasonThereIsNoListSaysWhichOneItIs() {
        // A blank list for all of these is the failure these empty states exist to avoid: "the proxy
        // is not running", "somebody else holds the port" and "this scenario has no rules" are three
        // different things to do next.
        let down = RuleFormatting.rulesVacancy(status: .down, read: nil, controlPort: 8088)
        XCTAssertEqual(down?.message, "Proxy is not running.")

        let foreign = RuleFormatting.rulesVacancy(
            status: .foreignProfile(running: RulesFixture.theirs), read: nil, controlPort: 8088)
        XCTAssertTrue(foreign?.message.contains("8088") == true, foreign?.message ?? "nil")
        XCTAssertTrue(foreign?.message.contains(RulesFixture.theirs) == true, foreign?.message ?? "nil")

        let old = RuleFormatting.rulesVacancy(status: .intercepting, read: .unsupported, controlPort: 8088)
        XCTAssertEqual(old?.message, "This engine predates the rules view.")
        XCTAssertTrue(old?.hint.contains("404") == true, old?.hint ?? "nil")

        let broken = RuleFormatting.rulesVacancy(
            status: .intercepting, read: .unavailable("the connection timed out"), controlPort: 8088)
        XCTAssertEqual(broken?.hint, "the connection timed out")

        let unknown = RuleFormatting.rulesVacancy(
            status: .profileUnknown("lyrebird not found"), read: nil, controlPort: nil)
        XCTAssertTrue(unknown?.hint.contains("lyrebird not found") == true, unknown?.hint ?? "nil")
    }

    func testAForeignProxysRefusalIsNeverReportedAsAnEngineTooOld() throws {
        // Status is read first on purpose: a proxy that is not ours answers 404 to plenty of
        // things, and "update your engine" would send the operator to fix the wrong machine.
        let vacancy = RuleFormatting.rulesVacancy(
            status: .foreignProfile(running: RulesFixture.theirs), read: .unsupported, controlPort: 8088)

        XCTAssertTrue(try XCTUnwrap(vacancy).message.contains(RulesFixture.theirs))
    }

    func testAScenarioWhoseRulesWereAllDroppedStillNamesWhatWentWrong() throws {
        // The worst case of the two being folded together: every rule failed to load, so the list
        // is empty and every problem is recorded — and reading the problems out of the branch that
        // decides the list showed the reassuring "has no rules yet" and nothing else.
        let snapshot = RulesSnapshot(
            scenario: "orders-outage",
            notWhole: ["orders-outage: rule 1 skipped — unknown field 'statsu'"],
            rules: [])
        let read = MockClient.RulesRead.ok(snapshot)

        XCTAssertEqual(RuleFormatting.problems(in: read), snapshot.notWhole)
        XCTAssertEqual(
            RuleFormatting.rulesVacancy(status: .intercepting, read: read, controlPort: 8088)?.message,
            "orders-outage has no rules yet.",
            "the empty state still applies; the banner is what must appear beside it")
    }

    func testThereAreNoProblemsToShowWhenTheReadItselfFailed() {
        // A failed read says nothing about how the scenario loaded, and a banner invented from it
        // would blame the scenario for the proxy's silence.
        XCTAssertTrue(RuleFormatting.problems(in: .unavailable("the connection timed out")).isEmpty)
        XCTAssertTrue(RuleFormatting.problems(in: .unsupported).isEmpty)
        XCTAssertTrue(RuleFormatting.problems(in: nil).isEmpty)
    }

    func testAScenarioWithNoRulesIsNamedRatherThanShownAsAnEmptyList() throws {
        let snapshot = RulesSnapshot(scenario: "baseline", notWhole: [], rules: [])

        let vacancy = try XCTUnwrap(
            RuleFormatting.rulesVacancy(status: .intercepting, read: .ok(snapshot), controlPort: 8088))

        XCTAssertEqual(vacancy.message, "baseline has no rules yet.")
    }

    func testASnapshotWithRulesInItHasNothingToExplainAndShowsTheList() throws {
        let snapshot = try JSONDecoder().decode(RulesSnapshot.self, from: Data(RulesFixture.snapshot.utf8))

        XCTAssertNil(RuleFormatting.rulesVacancy(status: .intercepting, read: .ok(snapshot), controlPort: 8088))
    }

    func testARequestThatNeverGotAResponseIsNotAGreenZero() {
        // `addon._record` files a flow that never got a response as status 0, and the menu's recent
        // list is the one place a status is still coloured — it says what happened to a request,
        // where the window says what a rule is set to. A green 0 claims a request succeeded when
        // what happened is that it produced no response at all.
        XCTAssertEqual(RuleFormatting.statusText(0), "no response")
        XCTAssertEqual(RuleFormatting.statusColor(0), .secondary)
        XCTAssertEqual(RuleFormatting.statusText(200), "200")
        XCTAssertEqual(RuleFormatting.statusColor(204), RuleFormatting.success)
        XCTAssertEqual(RuleFormatting.statusColor(503), RuleFormatting.danger)
    }

    // MARK: - What a refused action leaves behind

    func testAWriteTheProxyRefusedLeavesSomethingTheWindowCanShow() {
        // The window makes three writes and every one can be refused; the message used to be
        // rendered only in the menu, so a refusal while the window had focus produced nothing at
        // all — the button was pressed and the counts did not move.
        XCTAssertEqual(
            RuleFormatting.actionFailure("activate 'checkout': no scenario named 'checkout'"),
            "activate 'checkout': no scenario named 'checkout'")
        XCTAssertNil(RuleFormatting.actionFailure(nil))
        XCTAssertNil(
            RuleFormatting.actionFailure("   \n "),
            "a cleared error must not leave a bar of blank red behind")
    }

    // MARK: - Searching and grouping

    /// Thirty rules, built here rather than decoded, so the counts below are arithmetic a reader can
    /// check: every 7th is switched off, and every 5th carries a note.
    private func manyRules() -> [RuleRow] {
        (1...30).map { index in
            let number = String(format: "%02d", index)
            return RuleRow(
                id: "ovr_r" + number,
                match: RuleMatch(method: index % 3 == 0 ? "POST" : "GET", path: "/api/v1/items/" + number),
                notes: index % 5 == 0 ? "checkout note " + number : nil,
                rewrite: Rewrite(active: index % 7 != 0, mode: "replace", status: 200, bodyKind: "none"))
        }
    }

    private func rule(id: String, method: String? = nil, path: String? = nil, notes: String? = nil) -> RuleRow {
        RuleRow(
            id: id, match: RuleMatch(method: method, path: path), notes: notes,
            rewrite: Rewrite(active: true, mode: "replace", status: 200, bodyKind: "none"))
    }

    func testSearchLooksAtThePathTheMethodTheIdAndTheNotes() {
        // Four fields because four are what someone has to hand: the id they wrote in a test, the
        // path they are debugging, the method, and the note they left themselves.
        let rows = [
            rule(id: "ovr_orders", method: "GET", path: "/api/v1/orders"),
            rule(id: "ovr_flags", method: "POST", path: "/api/v1/features", notes: "checkout toggle"),
        ]

        XCTAssertEqual(RuleFormatting.filter(rows, query: "orders").map(\.id), ["ovr_orders"])
        XCTAssertEqual(RuleFormatting.filter(rows, query: "features").map(\.id), ["ovr_flags"])
        XCTAssertEqual(RuleFormatting.filter(rows, query: "post").map(\.id), ["ovr_flags"])
        XCTAssertEqual(RuleFormatting.filter(rows, query: "checkout").map(\.id), ["ovr_flags"])
        XCTAssertEqual(RuleFormatting.filter(rows, query: "").count, 2, "an empty query hides nothing")
    }

    func testSearchIgnoresCaseAndSurroundingSpace() {
        let rows = [rule(id: "ovr_orders", method: "GET", path: "/api/v1/Orders")]

        for query in ["orders", "ORDERS", "  Orders  "] {
            XCTAssertEqual(RuleFormatting.filter(rows, query: query).count, 1, query)
        }
    }

    func testSearchDoesNotMatchAcrossTwoFields() {
        // The fields are joined for one substring test, and without a separator a query could span
        // the end of the id and the start of the path and report a rule that contains no such text.
        let rows = [rule(id: "ovr_a", method: "GET", path: "/b")]

        XCTAssertTrue(RuleFormatting.filter(rows, query: "ovr_a/b").isEmpty)
    }

    func testSearchIgnoresAccentsTheReaderDidNotType() {
        // Neither case nor accents are a distinction the person typing made on purpose, and a note
        // reading "café" that `cafe` does not find is how someone concludes the rule is not there.
        let rows = [rule(id: "ovr_a", path: "/api/v1/a", notes: "café outage")]

        for query in ["cafe", "café", "CAFE", "CAFÉ"] {
            XCTAssertEqual(RuleFormatting.filter(rows, query: query).count, 1, query)
        }
    }

    func testFilteringKeepsTheOrderTheSnapshotListedThemIn() {
        // The snapshot's order is the order the proxy holds the rules in, which is what an operator
        // looking for a rule by position is counting on.
        let rows = manyRules()

        let shown = RuleFormatting.filter(rows, query: "api")

        XCTAssertEqual(shown.map(\.id), rows.map(\.id))
    }

    func testTheInactiveRulesAreGroupedApartFromTheOnesThatCanAnswer() {
        let rows = manyRules()

        let groups = RuleFormatting.grouped(rows)

        XCTAssertEqual(groups.inactive.map(\.id), ["ovr_r07", "ovr_r14", "ovr_r21", "ovr_r28"])
        XCTAssertEqual(groups.active.count, 26)
        XCTAssertTrue(
            groups.active.allSatisfy(\.isActive), "a rule that cannot answer is not in the top list")
        XCTAssertEqual(
            (groups.active.map(\.id) + groups.inactive.map(\.id)).sorted(), rows.map(\.id).sorted(),
            "every rule is in exactly one of the two, and none in both")
    }

    func testTheDetailPaneResolvesASelectionTheFilterHides() {
        // The pane looks the rule up in every rule the snapshot carries, not in the shown ones:
        // reading it from the filtered list would blank the pane while every filter test passed.
        let rows = manyRules()
        let snapshot = RulesSnapshot(scenario: "orders-outage", notWhole: [], rules: rows)
        let shown = RuleFormatting.filter(rows, query: "items/12")

        XCTAssertEqual(RuleFormatting.detailRule(selection: .rule("ovr_r07"), in: snapshot)?.id, "ovr_r07")
        XCTAssertFalse(shown.contains { $0.id == "ovr_r07" })
        XCTAssertNil(RuleFormatting.detailRule(selection: .rule("ovr_gone"), in: snapshot))
        XCTAssertNil(RuleFormatting.detailRule(selection: nil, in: snapshot))
        XCTAssertNil(RuleFormatting.detailRule(selection: .rule("ovr_r07"), in: nil))
    }

}
