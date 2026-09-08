import XCTest

@testable import Lyrebird

/// The one line a rules row gives you before you click it. Every fact in it comes from `rewrite` —
/// the engine's own description — so these check the sentence, not the semantics: that a forced
/// status appears only when the rule forces one, that an exhausted sequence says so rather than
/// naming a step it will never serve, and that a missing method reads as the constraint it is not.
final class RuleFormattingTests: XCTestCase {

    // MARK: - What a rule matches

    func testARuleWithNoMethodMatchesAnyOfThemAndSaysSo() {
        // A blank in either field reads as a value that failed to render, when it is a rule that
        // answers every method, or every path. The row and the detail set the two halves in
        // different weights, so each is read on its own.
        XCTAssertEqual(RuleFormatting.method(of: RuleMatch(path: "/api/v1/items")), "ANY")
        XCTAssertEqual(RuleFormatting.method(of: nil), "ANY")
        XCTAssertEqual(RuleFormatting.method(of: RuleMatch(method: "get")), "GET", "the wire's casing is not the rule")
        XCTAssertEqual(RuleFormatting.path(of: RuleMatch(method: "GET")), "*")
        XCTAssertEqual(RuleFormatting.path(of: nil), "*")
        XCTAssertEqual(RuleFormatting.path(of: RuleMatch(path: "/api/v1/orders")), "/api/v1/orders")
    }

    // MARK: - What it answers with

    func testAReplaceRuleReadsAsItsStatusItsBodyAndItsDelay() {
        let rewrite = Rewrite(
            mode: "replace", status: 200, bodyKind: "json", bodyBytes: 1229, delayMs: 1000)

        XCTAssertEqual(RuleFormatting.howLine(rewrite), "replace → 200 json 1.2 KB · +1000 ms")
    }

    func testARuleThatAnswersWithNoBodySizesNothing() {
        // The engine reports `"none"` for a 204 however much body the rule carries. "none 0 B"
        // would describe a payload no request receives.
        let rewrite = Rewrite(mode: "replace", status: 204, bodyKind: "none")

        XCTAssertEqual(RuleFormatting.howLine(rewrite), "replace → 204")
    }

    func testAPatchNamesAForcedStatusOnlyWhenTheRuleForcesOne() {
        // `rewrite.status` is null for a patch that keeps the real response's status, and printing
        // a number there would be a claim about a response this engine has never seen.
        let forcing = Rewrite(mode: "patch", status: 503, bodyKind: "none", patchKeys: 3)
        let keeping = Rewrite(mode: "patch", bodyKind: "none", patchKeys: 3)

        XCTAssertEqual(
            RuleFormatting.howLine(forcing), "patch → merge 3 keys, force 503 · JSON upstream only")
        XCTAssertEqual(RuleFormatting.howLine(keeping), "patch → merge 3 keys · JSON upstream only")
    }

    func testAPatchStrategyIsNamedBecauseItChangesWhatTheMergeDoes() {
        let rewrite = Rewrite(mode: "patch", bodyKind: "none", patchKeys: 1, patchStrategy: "appendToArray")

        XCTAssertEqual(
            RuleFormatting.howLine(rewrite), "patch → merge 1 key, appendToArray · JSON upstream only")
    }

    func testASequenceReadsAsItsStepCountItsCursorAndTheExhaustionItWillApply() {
        let rewrite = Rewrite(
            mode: "replace",
            sequence: RewriteSequence(
                advanceOn: "self", onExhausted: "repeatLast",
                steps: Array(repeating: StepSummary(status: 200), count: 5)))

        XCTAssertEqual(
            RuleFormatting.howLine(rewrite, state: SequenceState(nextStep: 2)),
            "sequence 5 steps · next 2 · then repeatLast")
    }

    func testAnExhaustedSequenceSaysSoRatherThanNamingAStepItWillNeverServe() {
        // `sequence_states` sends `nextStep: null` once no planned step remains. Falling back to
        // step 1 there would show a rule about to answer with something it has already left behind.
        let rewrite = Rewrite(
            mode: "replace",
            sequence: RewriteSequence(onExhausted: "error", steps: [StepSummary(status: 200)]))

        XCTAssertEqual(
            RuleFormatting.howLine(rewrite, state: SequenceState(nextStep: nil, exhausted: true)),
            "sequence 1 step · exhausted · then error")
    }

    // MARK: - What it has done

    func testTheRowCaptionCountsAnswersAndSaysWhenARuleIsOff() {
        // Zero answers and switched off are different facts; a rule that is off has not merely
        // answered nothing yet, it will not answer. The run id is not here — thirty rows each
        // ending in the same opaque token is noise, and the detail pane is where it answers
        // something.
        XCTAssertEqual(RuleFormatting.answerCaption(AnswerState(active: true, count: 3, runId: "r7")), "3 answers")
        XCTAssertEqual(RuleFormatting.answerCaption(AnswerState(active: true, count: 1, runId: "r7")), "1 answer")
        XCTAssertEqual(RuleFormatting.answerCaption(AnswerState(active: true, count: 0)), "no answers yet")
        XCTAssertEqual(RuleFormatting.answerCaption(AnswerState(active: false, count: 3, runId: "r7")), "inactive")
    }

    func testTheDetailPaneNamesTheRunACountBelongsTo() {
        // A count with no run is evidence bound to no boundary, and must not read as one.
        XCTAssertEqual(RuleFormatting.runCaption(AnswerState(active: true, count: 3, runId: "r7")), "run r7")
        XCTAssertEqual(RuleFormatting.runCaption(AnswerState(active: true, count: 0)), "no run")
    }

    func testTheRowSummaryLeavesTheStatusToItsOwnColumn() {
        // The row shows the status in a fixed column so the codes line up; repeating it in the
        // summary beside it reads as two different facts about the same rule.
        let replace = Rewrite(mode: "replace", status: 503, bodyKind: "json", bodyBytes: 1229)
        let patch = Rewrite(mode: "patch", status: 503, bodyKind: "none", patchKeys: 3)

        XCTAssertEqual(RuleFormatting.howLine(replace, includingStatus: false), "replace → json 1.2 KB")
        XCTAssertEqual(
            RuleFormatting.howLine(patch, includingStatus: false), "patch → merge 3 keys · JSON upstream only")
        XCTAssertEqual(
            RuleFormatting.howLine(replace), "replace → 503 json 1.2 KB",
            "the detail and the tooltip still want the whole sentence")
    }

    func testAModeWithNothingAfterItDropsTheArrow() {
        // Found by looking at the rendered rows: a bodyless 204, with the status moved to its own
        // column, left the summary reading "replace →" — a sentence cut off mid-way.
        let bodyless = Rewrite(mode: "replace", status: 204, bodyKind: "none")

        XCTAssertEqual(RuleFormatting.howLine(bodyless, includingStatus: false), "replace")
        XCTAssertEqual(
            RuleFormatting.howLine(bodyless), "replace → 204",
            "the arrow earns its place as soon as something follows it")
    }

    func testOnlyTheDestructiveMethodCarriesAWarningTint() {
        // Colouring every method spends the reader's attention on a field they can already read.
        XCTAssertEqual(RuleFormatting.methodTint("DELETE"), .orange)
        XCTAssertEqual(RuleFormatting.methodTint("delete"), .orange, "the wire's casing is not the rule")
        XCTAssertNil(RuleFormatting.methodTint("GET"))
        XCTAssertNil(RuleFormatting.methodTint("POST"))
        XCTAssertNil(RuleFormatting.methodTint("ANY"))
    }

    // MARK: - The chips in the detail pane

    func testAMatchersPinsBecomeChipsAndABareMatcherHasNone() {
        // The row is only worth its vertical space when something is in it, and a query pin is
        // exactly the constraint the request line above cannot show.
        let pinned = RuleMatch(
            method: "GET", path: "/api/v1/orders",
            query: ["kind": .string("marketing"), "page": .int(2)])

        XCTAssertEqual(
            RuleFormatting.matchChips(for: pinned).map(\.text), ["kind = marketing", "page = 2"],
            "sorted, and a numeric pin shown as the engine compares it")
        XCTAssertTrue(RuleFormatting.matchChips(for: RuleMatch(path: "/api/v1/orders")).isEmpty)
        XCTAssertTrue(RuleFormatting.matchChips(for: nil).isEmpty)
    }

    func testALongBodyContainsIsTruncatedInTheMiddleSoBothEndsStayVisible() {
        // Keeping only the prefix makes two pins that differ at the end look like the same one.
        let long = String(repeating: "ab", count: 40)
        let chips = RuleFormatting.matchChips(for: RuleMatch(bodyContains: long))

        XCTAssertEqual(chips.count, 1)
        XCTAssertTrue(chips[0].text.hasPrefix("body ∋ \"ab"), chips[0].text)
        XCTAssertTrue(chips[0].text.hasSuffix("ab\""), chips[0].text)
        XCTAssertTrue(chips[0].text.contains("…"), chips[0].text)
        XCTAssertLessThan(chips[0].text.count, long.count)
    }

    func testAShortBodyContainsIsShownWhole() {
        XCTAssertEqual(
            RuleFormatting.matchChips(for: RuleMatch(bodyContains: "orderId")).map(\.text),
            [#"body ∋ "orderId""#])
    }

    func testTheAnswerChipsNameTheModeStatusDelayAndBody() {
        let rewrite = Rewrite(
            mode: "replace", status: 503, bodyKind: "json", bodyBytes: 251, delayMs: 1000)

        XCTAssertEqual(
            RuleFormatting.answerChips(for: rewrite).map(\.text),
            ["replace", "503", "+1000 ms", "json · 251 B"])
        XCTAssertEqual(
            RuleFormatting.answerChips(for: rewrite)[1].tint, RuleFormatting.statusColor(503),
            "the status chip carries its own colour so the number and the colour cannot disagree")
    }

    func testAModeTheEngineDidNotSendIsNotInvented() {
        // Every validated rule carries a mode, so a missing one is a snapshot this app does not
        // understand — and a chip reading "replace" over what might be a patch is a claim about the
        // rule, not a gap in the row.
        XCTAssertEqual(RuleFormatting.answerChips(for: Rewrite(status: 200)).map(\.text), ["200"])
    }

    func testAHowLineDoesNotInventAModeEither() {
        // The row's line and the detail's chip read the same field, so they refuse the same way:
        // the mode word is dropped rather than guessed, and what the rule answers with still shows.
        XCTAssertEqual(
            RuleFormatting.howLine(Rewrite(status: 200, bodyKind: "json", bodyBytes: 1229)),
            "→ 200 json 1.2 KB")
    }

    func testARuleThatAnswersWithNoBodyGetsNoBodyChip() {
        // "none · 0 B" would be a pill describing a payload no request receives.
        XCTAssertEqual(
            RuleFormatting.answerChips(for: Rewrite(mode: "replace", status: 204, bodyKind: "none"))
                .map(\.text),
            ["replace", "204"])
    }

    func testAPatchsChipsCountItsKeysAndNameItsStrategy() {
        let rewrite = Rewrite(
            mode: "patch", bodyKind: "none", patchKeys: 3, patchStrategy: "appendToArray")

        XCTAssertEqual(
            RuleFormatting.answerChips(for: rewrite).map(\.text),
            ["patch", "3 keys", "appendToArray"],
            "no status chip: a patch forcing none keeps the real response's")
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
        XCTAssertEqual(RuleFormatting.statusColor(204), .green)
        XCTAssertEqual(RuleFormatting.statusColor(399), .green)
        XCTAssertEqual(RuleFormatting.statusColor(400), .red)
        XCTAssertEqual(RuleFormatting.statusColor(503), .red)
    }

    // MARK: - Nothing to show, and why

    func testEachReasonThereIsNoTableSaysWhichOneItIs() {
        // A blank table for all of these is the failure this window's empty states exist to avoid:
        // "the proxy is not running", "somebody else holds the port" and "this scenario has no
        // rules" are three different things to do next.
        let down = RuleFormatting.vacancy(status: .down, read: nil, controlPort: 8088)
        XCTAssertEqual(down?.message, "Proxy is not running.")

        let foreign = RuleFormatting.vacancy(
            status: .foreignProfile(running: RulesFixture.theirs), read: nil, controlPort: 8088)
        XCTAssertTrue(foreign?.message.contains("8088") == true, foreign?.message ?? "nil")
        XCTAssertTrue(foreign?.message.contains(RulesFixture.theirs) == true, foreign?.message ?? "nil")

        let old = RuleFormatting.vacancy(status: .intercepting, read: .unsupported, controlPort: 8088)
        XCTAssertEqual(old?.message, "This engine predates the rules view.")
        XCTAssertTrue(old?.hint.contains("404") == true, old?.hint ?? "nil")

        let broken = RuleFormatting.vacancy(
            status: .intercepting, read: .unavailable("the connection timed out"), controlPort: 8088)
        XCTAssertEqual(broken?.hint, "the connection timed out")
    }

    func testAForeignProxysRefusalIsNeverReportedAsAnEngineTooOld() throws {
        // Status is read first on purpose: a proxy that is not ours answers 404 to plenty of
        // things, and "update your engine" would send the operator to fix the wrong machine.
        let vacancy = RuleFormatting.vacancy(
            status: .foreignProfile(running: RulesFixture.theirs), read: .unsupported, controlPort: 8088)

        XCTAssertTrue(try XCTUnwrap(vacancy).message.contains(RulesFixture.theirs))
    }

    func testAScenarioWhoseRulesWereAllDroppedStillNamesWhatWentWrong() throws {
        // The worst case of the two being folded together: every rule failed to load, so the list
        // is empty and every problem is recorded — and reading the problems out of the branch that
        // decides the table showed the reassuring "has no rules yet" and nothing else.
        let snapshot = RulesSnapshot(
            scenario: "orders-outage",
            notWhole: ["orders-outage: rule 1 skipped — unknown field 'statsu'"],
            rules: [])
        let read = MockClient.RulesRead.ok(snapshot)

        XCTAssertEqual(RuleFormatting.problems(in: read), snapshot.notWhole)
        XCTAssertEqual(
            RuleFormatting.vacancy(status: .intercepting, read: read, controlPort: 8088)?.message,
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
            RuleFormatting.vacancy(status: .intercepting, read: .ok(snapshot), controlPort: 8088))

        XCTAssertEqual(vacancy.message, "baseline has no rules yet.")
    }

    func testASnapshotWithRulesInItHasNothingToExplainAndShowsTheTable() throws {
        let snapshot = try JSONDecoder().decode(RulesSnapshot.self, from: Data(RulesFixture.snapshot.utf8))

        XCTAssertNil(RuleFormatting.vacancy(status: .intercepting, read: .ok(snapshot), controlPort: 8088))
    }

    // MARK: - Searching, filtering and grouping

    /// Thirty rules, built here rather than decoded, so the counts below are arithmetic a reader can
    /// check: every 7th is switched off, every 6th is a sequence, and every 4th has answered nothing
    /// in a run it does not have.
    private func manyRules(_ count: Int = 30) -> [RuleRow] {
        (1...count).map { index in
            let number = String(format: "%02d", index)
            return RuleRow(
                id: "ovr_r" + number,
                match: RuleMatch(method: index % 3 == 0 ? "POST" : "GET", path: "/api/v1/items/" + number),
                notes: index % 5 == 0 ? "checkout note " + number : nil,
                rewrite: Rewrite(mode: "replace", status: 200),
                answer: AnswerState(
                    active: index % 7 != 0, count: index % 4, runId: index % 4 == 0 ? nil : "r1"),
                sequenceState: index % 6 == 0 ? SequenceState(runId: "r1", nextStep: 1) : nil)
        }
    }

    private func rule(
        id: String, method: String? = nil, path: String? = nil, notes: String? = nil,
        answer: AnswerState = AnswerState(active: true, count: 0)
    ) -> RuleRow {
        RuleRow(
            id: id, match: RuleMatch(method: method, path: path), notes: notes,
            rewrite: Rewrite(mode: "replace", status: 200), answer: answer)
    }

    func testSearchLooksAtThePathTheMethodTheIdAndTheNotes() {
        // Four fields because four are what someone has to hand: the id they wrote in a test, the
        // path they are debugging, the method, and the note they left themselves.
        let rows = [
            rule(id: "ovr_orders", method: "GET", path: "/api/v1/orders"),
            rule(id: "ovr_flags", method: "POST", path: "/api/v1/features", notes: "checkout toggle"),
        ]

        XCTAssertEqual(RuleFormatting.filter(rows, query: "orders", segment: .all).map(\.id), ["ovr_orders"])
        XCTAssertEqual(RuleFormatting.filter(rows, query: "features", segment: .all).map(\.id), ["ovr_flags"])
        XCTAssertEqual(RuleFormatting.filter(rows, query: "post", segment: .all).map(\.id), ["ovr_flags"])
        XCTAssertEqual(RuleFormatting.filter(rows, query: "checkout", segment: .all).map(\.id), ["ovr_flags"])
        XCTAssertEqual(RuleFormatting.filter(rows, query: "", segment: .all).count, 2, "an empty query hides nothing")
    }

    func testSearchIgnoresCaseAndSurroundingSpace() {
        let rows = [rule(id: "ovr_orders", method: "GET", path: "/api/v1/Orders")]

        for query in ["orders", "ORDERS", "  Orders  "] {
            XCTAssertEqual(RuleFormatting.filter(rows, query: query, segment: .all).count, 1, query)
        }
    }

    func testSearchDoesNotMatchAcrossTwoFields() {
        // The fields are joined for one substring test, and without a separator a query could span
        // the end of the id and the start of the path and report a rule that contains no such text.
        let rows = [rule(id: "ovr_a", method: "GET", path: "/b")]

        XCTAssertTrue(RuleFormatting.filter(rows, query: "ovr_a/b", segment: .all).isEmpty)
    }

    func testSearchIgnoresAccentsTheReaderDidNotType() {
        // Neither case nor accents are a distinction the person typing made on purpose, and a note
        // reading "café" that `cafe` does not find is how someone concludes the rule is not there.
        let rows = [rule(id: "ovr_a", path: "/api/v1/a", notes: "café outage")]

        for query in ["cafe", "café", "CAFE", "CAFÉ"] {
            XCTAssertEqual(RuleFormatting.filter(rows, query: query, segment: .all).count, 1, query)
        }
    }

    func testACountWithNoRunIsNotAnsweredThisRun() {
        // A count with no run is a count from no boundary anyone drew: the slot it was counted in is
        // gone, so it says nothing about the run the window is showing.
        let orphan = rule(id: "ovr_orphan", path: "/api/v1/orphan", answer: AnswerState(active: true, count: 2))
        let current = rule(
            id: "ovr_now", path: "/api/v1/now", answer: AnswerState(active: true, count: 2, runId: "r1"))

        XCTAssertEqual(
            RuleFormatting.filter([orphan, current], query: "", segment: .answered).map(\.id), ["ovr_now"])
    }

    func testEachSegmentAdmitsWhatItsNameSays() {
        let rows = manyRules()

        XCTAssertEqual(RuleFormatting.filter(rows, query: "", segment: .all).count, 30)
        XCTAssertEqual(
            RuleFormatting.filter(rows, query: "", segment: .sequences).map(\.id),
            ["ovr_r06", "ovr_r12", "ovr_r18", "ovr_r24", "ovr_r30"])
        // Answered this run: a count above zero under a run id. The seven rules whose index divides
        // by four have neither, and a rule with a count but no run has answered in no run at all.
        let answered = RuleFormatting.filter(rows, query: "", segment: .answered)
        XCTAssertEqual(answered.count, 23)
        XCTAssertTrue(answered.allSatisfy { $0.answer.count > 0 && $0.answer.runId != nil })
    }

    func testASearchAndASegmentApplyTogether() {
        let rows = manyRules()

        XCTAssertEqual(
            RuleFormatting.filter(rows, query: "items/12", segment: .sequences).map(\.id), ["ovr_r12"])
        XCTAssertTrue(RuleFormatting.filter(rows, query: "items/13", segment: .sequences).isEmpty)
    }

    func testFilteringKeepsTheOrderTheSnapshotListedThemIn() {
        // The snapshot's order is the order the proxy holds the rules in, which is what an operator
        // looking for a rule by position is counting on.
        let rows = manyRules()

        let shown = RuleFormatting.filter(rows, query: "api", segment: .all)

        XCTAssertEqual(shown.map(\.id), rows.map(\.id))
    }

    func testTheInactiveRulesAreGroupedApartFromTheOnesThatCanAnswer() {
        let rows = manyRules()

        let groups = RuleFormatting.grouped(rows)

        XCTAssertEqual(groups.inactive.map(\.id), ["ovr_r07", "ovr_r14", "ovr_r21", "ovr_r28"])
        XCTAssertEqual(groups.active.count, 26)
        XCTAssertTrue(groups.active.allSatisfy(\.isActive), "a rule that cannot answer is not in the top list")
        XCTAssertEqual(
            (groups.active.map(\.id) + groups.inactive.map(\.id)).sorted(), rows.map(\.id).sorted(),
            "every rule is in exactly one of the two, and none in both")
    }

    func testASelectionTheFilterHidesIsReportedRatherThanDropped() {
        // The detail pane keeps showing it — narrowing a search must not throw away what you were
        // reading — so the list has to say why the highlighted row is not in it.
        let rows = manyRules()
        let shown = RuleFormatting.filter(rows, query: "items/12", segment: .all)

        XCTAssertTrue(RuleFormatting.selectionIsHidden("ovr_r13", shown: shown, all: rows))
        XCTAssertFalse(RuleFormatting.selectionIsHidden("ovr_r12", shown: shown, all: rows))
        XCTAssertFalse(RuleFormatting.selectionIsHidden(nil, shown: shown, all: rows))
        XCTAssertFalse(
            RuleFormatting.selectionIsHidden("ovr_gone", shown: shown, all: rows),
            "a rule the scenario no longer has is not one a filter is hiding")
    }

    func testTheDetailPaneResolvesASelectionTheFilterHides() {
        // The pane looks the rule up in every rule the snapshot carries, not in the shown ones:
        // reading it from the filtered list would blank the pane while every filter test passed.
        let rows = manyRules()
        let snapshot = RulesSnapshot(scenario: "orders-outage", notWhole: [], rules: rows)
        let shown = RuleFormatting.filter(rows, query: "items/12", segment: .all)

        XCTAssertEqual(RuleFormatting.detailRule(selection: "ovr_r07", in: snapshot)?.id, "ovr_r07")
        XCTAssertTrue(
            RuleFormatting.selectionIsHidden("ovr_r07", shown: shown, all: rows),
            "and the list says why the highlighted row is not in it")
        XCTAssertNil(RuleFormatting.detailRule(selection: "ovr_gone", in: snapshot))
        XCTAssertNil(RuleFormatting.detailRule(selection: nil, in: snapshot))
        XCTAssertNil(RuleFormatting.detailRule(selection: "ovr_r07", in: nil))
    }

    func testTheInactiveGroupOpensForASearchAndClosesWhenItEnds() {
        XCTAssertFalse(
            RuleFormatting.inactiveGroupExpanded(userExpanded: false, query: "", inactiveMatches: false))
        XCTAssertFalse(
            RuleFormatting.inactiveGroupExpanded(userExpanded: false, query: "  ", inactiveMatches: true),
            "an empty field is not a search")
        XCTAssertTrue(
            RuleFormatting.inactiveGroupExpanded(userExpanded: false, query: "checkout", inactiveMatches: true))
        XCTAssertFalse(
            RuleFormatting.inactiveGroupExpanded(userExpanded: false, query: "checkout", inactiveMatches: false),
            "a search that found nothing in there leaves it shut")
        XCTAssertTrue(
            RuleFormatting.inactiveGroupExpanded(userExpanded: true, query: "checkout", inactiveMatches: false),
            "what the reader opened stays open whatever the search does")
    }

    func testTheRuleCountNamesTheTotalOnceAFilterIsHidingSome() {
        // Without the total, a filtered list reads as a scenario that has lost most of its rules.
        XCTAssertEqual(RuleFormatting.ruleCount(shown: 12, total: 12), "12 rules")
        XCTAssertEqual(RuleFormatting.ruleCount(shown: 3, total: 12), "3 of 12 rules")
        XCTAssertEqual(RuleFormatting.ruleCount(shown: 1, total: 1), "1 rule")
        XCTAssertEqual(RuleFormatting.ruleCount(shown: 0, total: 12), "0 of 12 rules")
    }
}
