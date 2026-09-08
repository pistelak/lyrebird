import XCTest

@testable import Lyrebird

/// The one line a rules row gives you before you click it. Every fact in it comes from `rewrite` —
/// the engine's own description — so these check the sentence, not the semantics: that a forced
/// status appears only when the rule forces one, that an exhausted sequence says so rather than
/// naming a step it will never serve, and that a missing method reads as the constraint it is not.
final class RuleFormattingTests: XCTestCase {

    // MARK: - What a rule matches

    func testARuleWithNoMethodMatchesAnyOfThemAndSaysSo() {
        // A blank there reads as a value that failed to render, when it is a rule that answers
        // every method on the path.
        XCTAssertEqual(RuleFormatting.matchLine(RuleMatch(path: "/api/v1/items")), "ANY /api/v1/items")
        XCTAssertEqual(RuleFormatting.matchLine(nil), "ANY *")
        XCTAssertEqual(
            RuleFormatting.matchLine(RuleMatch(method: "get", path: "/api/v1/orders")),
            "GET /api/v1/orders")
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

    func testAnInactiveRuleSaysSoWhereItsAnswerCountWouldBe() {
        // Zero answers and switched off are different facts; a rule that is off has not merely
        // answered nothing yet, it will not answer.
        XCTAssertEqual(
            RuleFormatting.answerCaption(AnswerState(active: true, count: 3, runId: "r7")),
            "3 answers · run r7")
        XCTAssertEqual(
            RuleFormatting.answerCaption(AnswerState(active: false, count: 0)), "inactive · no run")
        XCTAssertEqual(
            RuleFormatting.answerCaption(AnswerState(active: true, count: 1)),
            "1 answer · no run",
            "a count with no run is evidence bound to no boundary, and must not read as one")
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
}
