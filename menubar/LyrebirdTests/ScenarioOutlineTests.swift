import XCTest

@testable import Lyrebird

final class ScenarioOutlineTests: XCTestCase {

    // MARK: - Fixtures

    private func replaceRule(
        id: String = "ovr_orders", active: Bool = true, method: String? = "GET",
        path: String = "/api/v1/orders", status: Int? = 200, delayMs: Int? = nil,
        delayCapped: Bool? = nil, query: [String: JSONValue]? = nil, bodyContains: String? = nil,
        bodyKind: String = "json", bodyBytes: Int? = 251
    ) -> RuleRow {
        RuleRow(
            id: id, match: RuleMatch(method: method, path: path, query: query, bodyContains: bodyContains),
            rewrite: Rewrite(
                active: active, mode: "replace", status: status, bodyKind: bodyKind, bodyBytes: bodyBytes,
                delayMs: delayMs, delayCapped: delayCapped))
    }

    private func patchRule(
        status: Int? = 503, keys: Int? = 3, strategy: String? = nil, delayMs: Int? = nil
    ) -> RuleRow {
        RuleRow(
            id: "ovr_flags", match: RuleMatch(method: "POST", path: "/api/v1/orders"),
            rewrite: Rewrite(
                active: true, mode: "patch", status: status, bodyKind: "none", patchKeys: keys,
                patchStrategy: strategy, delayMs: delayMs))
    }

    private func sequenceRule(
        id: String = "ovr_order_states", active: Bool = true, steps: Int = 2,
        onExhausted: String? = "error", advanceOn: RuleMatch? = nil,
        query: [String: JSONValue]? = nil, delayMs: Int? = nil
    ) -> RuleRow {
        RuleRow(
            id: id, match: RuleMatch(method: "GET", path: "/api/v2/orders", query: query),
            rewrite: Rewrite(
                active: active, mode: "replace", bodyKind: "none", delayMs: delayMs,
                sequence: RewriteSequence(
                    advanceOn: advanceOn, onExhausted: onExhausted,
                    steps: (1...steps).map { StepSummary(status: 199 + $0, bodyKind: "json", bodyBytes: 250 + $0) })))
    }

    private func snapshot(_ rules: [RuleRow], scenario: String = "orders-outage") -> RulesSnapshot {
        RulesSnapshot(scenario: scenario, notWhole: [], rules: rules)
    }

    // MARK: - The lines

    func testAReplaceRuleReadsAsWhatItReturnsAndWhatThatIs() {
        let rule = replaceRule(status: 200, delayMs: 1000)

        XCTAssertEqual(RuleFormatting.requestLine(rule.match), RequestLine(method: "GET", path: "/api/v1/orders"))
        XCTAssertEqual(RuleFormatting.behaviourLine(rule.rewrite), "Returns 200 after 1 s")
        XCTAssertEqual(RuleFormatting.clauseLine(rule.rewrite), "", "only a patch has clauses")
        XCTAssertEqual(RuleFormatting.metaLine(rule.rewrite), "JSON · 251 B")
    }

    func testAPatchHasNoBodyOfItsOwnToDescribe() {
        // It answers with the upstream's body merged, which this engine has not seen —
        // `describe_rewrite` reports `bodyKind` "none" for every patch for exactly that reason, and
        // rendered that read as "No body" over a rule that answers with whatever the real server
        // sent.
        XCTAssertNil(RuleFormatting.metaLine(patchRule().rewrite))
        XCTAssertEqual(RuleFormatting.metaLine(replaceRule().rewrite), "JSON · 251 B")
    }

    func testEveryModeSaysTheDelayItWaits() {
        // The proxy applies the delay before it looks at the mode, so a patch and a sequenced rule
        // wait exactly as a `replace` does; two of the three branches used to drop it.
        XCTAssertEqual(
            RuleFormatting.behaviourLine(replaceRule(status: 200, delayMs: 1000).rewrite),
            "Returns 200 after 1 s")
        XCTAssertEqual(
            RuleFormatting.behaviourLine(patchRule(delayMs: 1000).rewrite),
            "Patches the real response · 3 keys after 1 s")
        XCTAssertEqual(
            RuleFormatting.behaviourLine(sequenceRule(steps: 2, delayMs: 250).rewrite),
            "Sequence · 2 steps after 250 ms")
    }

    func testACappedDelaySaysSoRatherThanReadingAsTheOneItWasSetTo() {
        // The engine reports the delay it will *apply* — 60 s, whatever the rule asked for — so the
        // flag is the only thing that says the two differ, and without it a rule configured for two
        // minutes reads as one configured for exactly a minute.
        XCTAssertEqual(
            RuleFormatting.behaviourLine(
                replaceRule(status: 200, delayMs: 60000, delayCapped: true).rewrite),
            "Returns 200 after 60 s (capped)")
        XCTAssertEqual(
            RuleFormatting.behaviourLine(replaceRule(status: 200, delayMs: 60000).rewrite),
            "Returns 200 after 60 s",
            "a rule that asked for exactly the ceiling was not capped")
    }

    func testADelayIsSaidInTheUnitAReaderThinksIn() {
        XCTAssertEqual(RuleFormatting.seconds(1000), "1 s")
        XCTAssertEqual(RuleFormatting.seconds(1500), "1.5 s")
        XCTAssertEqual(RuleFormatting.seconds(250), "250 ms")
        XCTAssertEqual(
            RuleFormatting.behaviourLine(replaceRule(status: 204, delayMs: 0).rewrite), "Returns 204",
            "a delay of nothing is not a delay")
    }

    func testABodylessAnswerSaysSoRatherThanSizingNothing() {
        // "none 0 B" would describe a payload no request receives.
        XCTAssertEqual(RuleFormatting.metaLine(kind: "none", bytes: nil), "No body")
        XCTAssertEqual(RuleFormatting.metaLine(kind: "text", bytes: 12), "Text · 12 B")
        XCTAssertEqual(RuleFormatting.metaLine(kind: "json", bytes: nil), "JSON")
    }

    func testAPatchNamesWhatItMergesIntoAndUnderWhatCondition() {
        // A patch needs a buffered JSON upstream response to merge into; without one the request is
        // passed through untouched and recorded as `patchSkipped`, so "JSON responses only" is a
        // condition of the mode and belongs on every patch.
        let rule = patchRule(status: 503, keys: 3, strategy: "appendToArray")

        XCTAssertEqual(RuleFormatting.behaviourLine(rule.rewrite), "Patches the real response · 3 keys")
        XCTAssertEqual(
            RuleFormatting.clauseLine(rule.rewrite),
            "sets status to 503 · appends to arrays · JSON responses only")
    }

    func testAPatchClaimsNoStatusItDoesNotForceAndNoKeyCountItWasNotSent() {
        // `rewrite.status` is null for a patch that keeps the real response's, and a number there
        // would be a claim about a response this engine has never seen. `patchKeys` is null only in
        // a snapshot this app does not understand, and "0 keys" would describe a patch that changes
        // nothing.
        XCTAssertEqual(
            RuleFormatting.clauseLine(patchRule(status: nil, keys: 1).rewrite), "JSON responses only")
        XCTAssertEqual(
            RuleFormatting.behaviourLine(patchRule(status: nil, keys: 1).rewrite),
            "Patches the real response · 1 key")
        XCTAssertEqual(
            RuleFormatting.behaviourLine(patchRule(status: nil, keys: nil).rewrite),
            "Patches the real response")
    }

    func testASequenceReadsAsItsStepCount() {
        XCTAssertEqual(RuleFormatting.behaviourLine(sequenceRule(steps: 2).rewrite), "Sequence · 2 steps")
        XCTAssertEqual(RuleFormatting.behaviourLine(sequenceRule(steps: 1).rewrite), "Sequence · 1 step")
    }

    func testARuleWhoseStatusTheEngineDidNotSendSaysNothingAboutOne() {
        // The engine sends a status for every validated `replace`, so a missing one is a summary
        // this app does not understand — and "Returns" with nothing after it is a sentence cut off
        // mid-way.
        XCTAssertEqual(RuleFormatting.behaviourLine(replaceRule(status: nil).rewrite), "")
    }

    func testConditionsAreListedInFullHoweverManyThereAre() {
        // A matcher is the set of requests a rule answers. "+2 conditions" describes a rule the
        // reader then has to go and look up, and a pin cut in half looks like a pin that is there.
        let rule = replaceRule(
            path: "/api/v1/checkout/payment/authorisation",
            query: ["provider": .string("card"), "attempt": .int(2), "region": .string("eu-west")],
            bodyContains: "three-d-secure")

        XCTAssertEqual(
            RuleFormatting.conditionLines(rule.match),
            [
                RuleFormatting.Fact("Query", "attempt = 2 · provider = card · region = eu-west"),
                RuleFormatting.Fact("Body contains", "\"three-d-secure\""),
            ])
        XCTAssertTrue(RuleFormatting.conditionLines(RuleMatch(path: "/a")).isEmpty)
        XCTAssertTrue(
            RuleFormatting.conditionLines(RuleMatch(bodyContains: "")).isEmpty,
            "an empty substring constrains nothing on the wire, and `specificity` does not count it")
    }

    func testABareMatcherSaysWhatItMatchesRatherThanNothing() {
        XCTAssertEqual(RuleFormatting.requestLine(nil), RequestLine(method: "ANY", path: "*"))
    }

    // MARK: - The outline

    func testPlainRulesKeepTheirConfiguredOrder() {
        let outline = RuleFormatting.outline(snapshot([replaceRule(), patchRule()]))

        XCTAssertTrue(outline.sequences.isEmpty)
        XCTAssertEqual(outline.otherRules.map(\.id), ["ovr_orders", "ovr_flags"])
    }

    func testSequencesAreSeparatedFromOtherRules() {
        let outline = RuleFormatting.outline(snapshot([sequenceRule(), replaceRule()]))

        XCTAssertEqual(outline.sequences.map(\.id), ["ovr_order_states"])
        XCTAssertEqual(outline.otherRules.map(\.id), ["ovr_orders"])
    }

    func testEmptyScenariosHaveNoFlow() {
        let outline = RuleFormatting.outline(snapshot([], scenario: "baseline"))

        XCTAssertTrue(outline.sequences.isEmpty)
        XCTAssertTrue(outline.otherRules.isEmpty)
    }

    func testAnInactiveRuleIsLabelledAndListedAfterTheOnesThatAnswer() {
        // Being switched off is configuration, not runtime: still listed — a rule you cannot find is
        // a rule you will write a second time — and labelled rather than hidden.
        let outline = RuleFormatting.outline(
            snapshot([replaceRule(id: "ovr_off", active: false, path: "/api/v1/profile"), replaceRule()]))

        XCTAssertEqual(outline.otherRules.map(\.id), ["ovr_orders", "ovr_off"])
        XCTAssertEqual(outline.otherRules.map(\.inactive), [false, true])
    }

    // MARK: - A sequence container

    func testASelfAdvancingSequenceSaysSoOnceAndPutsNothingBetweenItsStates() {
        // Each answer is the move, so there is nothing to say between two states; saying it there,
        // once per state, would be the same sentence N times.
        let container = RuleFormatting.outline(snapshot([sequenceRule(steps: 3)])).sequences[0]

        XCTAssertEqual(container.advanceNote, "Advances after each answer")
        XCTAssertEqual(container.states.map(\.number), [1, 2, 3])
        XCTAssertTrue(container.states.allSatisfy { $0.transition == nil })
    }

    func testAnExplicitTriggerIsABlockAfterEveryState() {
        // After each of them, the last included: a trigger is what moves the cursor off that state
        // too, and the footer says what a request meets once it has.
        let container = RuleFormatting.outline(
            snapshot([
                sequenceRule(
                    steps: 3,
                    advanceOn: RuleMatch(method: "PATCH", path: "/api/v2/orders"))
            ])
        ).sequences[0]

        XCTAssertNil(container.advanceNote, "the blocks say it, where it happens")
        XCTAssertEqual(container.states.map { $0.transition != nil }, [true, true, true])
        let transition = container.states[0].transition!
        XCTAssertEqual(
            transition.request, RequestLine(method: "PATCH", path: "/api/v2/orders"))
    }

    func testTheAdvanceMatchersConditionsAreShownWhateverElseIs() {
        // A trigger is a matcher, and one drawn as method and path alone read as "ANY *" — every
        // request there is — while the sequence in fact moved on one of them.
        let container = RuleFormatting.outline(
            snapshot([
                sequenceRule(
                    steps: 2,
                    advanceOn: RuleMatch(
                        method: "PATCH", path: "/api/v2/orders",
                        query: ["id": .string("order-42")]))
            ])
        ).sequences[0]

        XCTAssertEqual(
            container.states[0].transition?.conditions,
            [RuleFormatting.Fact("Query", "id = order-42")])
    }

    func testScenarioNotesBelongToTheBrowsedScenarioAndBlankNotesStayHidden() {
        let scenarios = ScenarioList(
            active: "active",
            scenarios: [
                ScenarioSummary(name: "active", overrideCount: 1, verified: false, notes: "Active description"),
                ScenarioSummary(
                    name: "browsed", overrideCount: 2, verified: false, notes: "  Shows a retry after failure.\n"),
                ScenarioSummary(name: "empty", overrideCount: 0, verified: false, notes: " \n "),
            ])
        XCTAssertEqual(RuleFormatting.scenarioNotes("browsed", in: scenarios), "Shows a retry after failure.")
        XCTAssertNil(RuleFormatting.scenarioNotes("empty", in: scenarios))
        XCTAssertNil(RuleFormatting.scenarioNotes("missing", in: scenarios))
        XCTAssertNil(RuleFormatting.scenarioNotes("browsed", in: nil))
    }

    func testResponseModesDescribeTheRewriteRatherThanTheHTTPMethod() {
        let replacement = replaceRule(method: "PATCH")
        XCTAssertEqual(RuleFormatting.responseKind(replacement.rewrite), .replace)
        XCTAssertEqual(RuleFormatting.responseKind(patchRule().rewrite), .patch)
        XCTAssertEqual(RuleFormatting.responseKind(sequenceRule(steps: 2).rewrite), .sequence)
        var unknown = replacement.rewrite
        unknown.mode = "future-mode"
        XCTAssertEqual(RuleFormatting.responseKind(unknown), .unknown("future-mode"))
        let shot = snapshot([
            sequenceRule(id: "ovr_orders", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
            replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204),
        ])
        XCTAssertEqual(RuleFormatting.flowSections(shot)[0].rows.map(\.responseKind), [.sequence, .replace, .sequence])
    }

    func testFlowListRowsOpenTheCorrectRuleAndStepAndDoNotDuplicateTheTrigger() {
        let shot = snapshot([
            sequenceRule(id: "ovr_orders", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
            replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204),
        ])
        let sections = RuleFormatting.flowSections(shot)
        XCTAssertEqual(sections.count, 1)
        let rows = sections[0].rows
        XCTAssertEqual(rows.map(\.number), [1, 2, 3])
        XCTAssertEqual(rows.map(\.ruleId), ["ovr_orders", "ovr_update", "ovr_orders"])
        XCTAssertEqual(rows.map(\.step), [1, nil, 2])
        XCTAssertEqual(RuleFormatting.detailRule(selection: rows[2].selection, in: shot)?.id, "ovr_orders")
        XCTAssertEqual(RuleFormatting.flowRow(rows[2].selection, in: shot)?.step, 2)
        XCTAssertEqual(RuleFormatting.selection(current: nil, in: shot), rows[0].selection)
        XCTAssertEqual(RuleFormatting.selection(current: rows[2].selection, in: shot), rows[2].selection)
        let filtered = RuleFormatting.flowSections(shot, query: "PATCH").flatMap(\.rows)
        XCTAssertEqual(filtered.map(\.number), [2], "search must not renumber configured order")
        XCTAssertEqual(RuleFormatting.detailRule(selection: rows[2].selection, in: shot)?.id, "ovr_orders")
    }

    func testSequenceIDsCannotCollideWithTheOtherResponsesSection() {
        let sections = RuleFormatting.flowSections(
            snapshot([
                sequenceRule(id: "other-rules"), replaceRule(),
            ]))
        XCTAssertEqual(sections.count, 2)
        XCTAssertEqual(Set(sections.map(\.id)).count, 2)
    }

    func testAmbiguousTriggerResponsesRemainAccessibleOutsideTheFlow() {
        let sections = RuleFormatting.flowSections(
            snapshot([
                sequenceRule(advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
                replaceRule(id: "first", method: "PATCH", path: "/api/orders", query: ["id": .int(1)]),
                replaceRule(id: "second", method: "PATCH", path: "/api/orders", query: ["id": .int(2)]),
            ]))
        let trigger = sections[0].rows[1]
        XCTAssertNil(trigger.ruleId)
        XCTAssertNil(trigger.status)
        XCTAssertEqual(trigger.transition?.relatedResponses.map(\.id), ["first", "second"])
        XCTAssertEqual(sections[1].rows.map(\.ruleId), ["first", "second"])
    }

    func testAnInactiveResponseDoesNotDisableTheAdvanceRequestOrClaimItsStatus() {
        var response = replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders")
        response.rewrite.active = false
        let sections = RuleFormatting.flowSections(
            snapshot([
                sequenceRule(steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
                response,
            ]))
        let trigger = sections[0].rows[1]
        XCTAssertFalse(trigger.inactive)
        XCTAssertNil(trigger.status)
        XCTAssertNil(trigger.ruleId)
        XCTAssertNil(trigger.responseKind)
        XCTAssertEqual(trigger.transition?.relatedResponses.map(\.id), ["ovr_update"])
        XCTAssertEqual(sections[1].rows.map(\.ruleId), ["ovr_update"])
        XCTAssertTrue(sections[1].rows[0].inactive)
    }

    func testTheFinalResponseRetainsItsEndingMatcherWithoutAnExtraFlowRow() {
        let matcher = RuleMatch(
            method: "PATCH", path: "/api/orders/ack", query: ["id": .int(42)], bodyContains: "confirmed")
        for count in [1, 3] {
            let rows = RuleFormatting.flowSections(
                snapshot([
                    sequenceRule(steps: count, advanceOn: matcher)
                ]))[0].rows
            XCTAssertEqual(rows.count, count * 2 - 1)
            XCTAssertEqual(rows.last?.step, count)
            XCTAssertNil(rows.last?.transition)
            XCTAssertEqual(rows.last?.endingTransition?.request, RuleFormatting.requestLine(matcher))
            XCTAssertEqual(rows.last?.endingTransition?.conditions, RuleFormatting.conditionLines(matcher))
            XCTAssertTrue(rows.dropLast().allSatisfy { $0.endingTransition == nil })
        }
    }

    func testSharedTriggersKeepDistinctFlowSelectionsAndUnresolvedTriggersHaveNoInventedResponse() {
        let shot = snapshot([
            sequenceRule(id: "ovr_one", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
            sequenceRule(id: "ovr_two", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
        ])
        let sections = RuleFormatting.flowSections(shot)
        XCTAssertEqual(sections.count, 2)
        XCTAssertNotEqual(sections[0].rows[1].selection, sections[1].rows[1].selection)
        for section in sections {
            let trigger = section.rows[1]
            XCTAssertNil(trigger.ruleId)
            XCTAssertNil(trigger.status)
            XCTAssertNotNil(trigger.transition)
            XCTAssertNil(RuleFormatting.detailRule(selection: trigger.selection, in: shot))
        }
        XCTAssertNil(RuleFormatting.selection(current: nil, in: snapshot([])))
    }

    func testRequestFlowInterleavesReadsAndTriggersWithoutTreatingTheEndingAsAnotherStep() {
        let outline = RuleFormatting.outline(
            snapshot([
                sequenceRule(steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
                replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204),
            ]))
        let sequence = outline.sequences[0]
        XCTAssertEqual(sequence.flow.map(\.id), ["response-1", "trigger-1", "response-2"])
        guard case .trigger(_, let transition) = sequence.flow[1] else { return XCTFail("missing PATCH") }
        XCTAssertEqual(transition.request.method, "PATCH")
        XCTAssertEqual(transition.relatedResponses.first?.status, 204)
        XCTAssertNotNil(sequence.states.last?.transition, "the final trigger belongs in the ending disclosure")
        guard case .response(let state) = sequence.flow[2] else { return XCTFail("missing second response") }
        XCTAssertEqual(state.number, 2, "flow row 3 must still open sequence step 2")
    }

    func testATriggerIncludesItsConditionalResponseWithoutNarrowingAdvancement() {
        var response = replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204)
        response.match = RuleMatch(method: "PATCH", path: "/api/orders", query: ["id": .string("42")])
        let outline = RuleFormatting.outline(
            snapshot([
                sequenceRule(steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
                response,
                replaceRule(id: "ovr_unrelated", method: "PATCH", path: "/api/items"),
                replaceRule(id: "ovr_read", method: "GET", path: "/api/orders"),
            ]))
        let transition = outline.sequences[0].states[0].transition!
        XCTAssertTrue(transition.conditions.isEmpty)
        XCTAssertEqual(transition.relatedResponses.map(\.id), ["ovr_update"])
        XCTAssertEqual(transition.relatedResponses[0].conditions, [.init("Query", "id = 42")])
        XCTAssertEqual(transition.relatedResponses[0].behaviour, "Returns 204")
        XCTAssertTrue(outline.otherRules.contains { $0.id == "ovr_update" })
        XCTAssertEqual(outline.sequences[0].states[1].transition, transition)
    }

    func testRelatedResponseRulesKeepAllCandidatesAndInactiveLabels() {
        var inactive = replaceRule(id: "ovr_disabled", method: "PATCH", path: "/api/orders")
        inactive.rewrite.active = false
        let outline = RuleFormatting.outline(
            snapshot([
                sequenceRule(steps: 1, advanceOn: RuleMatch(method: "patch", path: "/api/orders")),
                inactive,
                replaceRule(id: "ovr_enabled", method: "PATCH", path: "/api/orders"),
            ]))
        let candidates = outline.sequences[0].states[0].transition!.relatedResponses
        XCTAssertEqual(candidates.map(\.id), ["ovr_disabled", "ovr_enabled"])
        XCTAssertTrue(candidates[0].inactive)
        XCTAssertFalse(candidates[1].inactive)
    }

    func testARuleThatAnswersATriggerIsStillOneOfTheScenariosRules() {
        // It answers requests of its own whether or not any sequence is running, and leaving it out
        // of the list would hide it from the one place that claims to hold every rule.
        let outline = RuleFormatting.outline(
            snapshot([
                sequenceRule(
                    steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/v2/ack")),
                replaceRule(id: "ovr_ack", method: "PATCH", path: "/api/v2/ack"),
            ]))

        XCTAssertEqual(outline.otherRules.map(\.id), ["ovr_ack"])
    }

    func testAOneStepSequenceStillShowsWhatMovesItPastItsOnlyState() {
        // The transition used to be drawn only *before* another state, so a one-step sequence lost
        // the whole of what moves it on — and every sequence lost the trigger that carries it into
        // exhaustion.
        let one = RuleFormatting.outline(
            snapshot([sequenceRule(steps: 1, advanceOn: RuleMatch(method: "PATCH", path: "/api/v2/ack"))])
        ).sequences[0]
        XCTAssertEqual(one.states.count, 1)
        XCTAssertNotNil(one.states[0].transition)

        let three = RuleFormatting.outline(
            snapshot([sequenceRule(steps: 3, advanceOn: RuleMatch(method: "PATCH", path: "/api/v2/ack"))])
        ).sequences[0]
        XCTAssertEqual(three.states.map { $0.transition != nil }, [true, true, true])
        XCTAssertTrue(
            RuleFormatting.outline(snapshot([sequenceRule(steps: 2)])).sequences[0]
                .states.allSatisfy { $0.transition == nil },
            "a self-advancing sequence still has nothing to put between its states")
    }

    func testEachExhaustionPolicyIsWordedAsWhatComesAfter() {
        // The engine's word is the setting; the footer says the outcome — a sequence under `error`
        // answers 500, which is what the reader will see.
        func footer(_ policy: String?) -> String? {
            RuleFormatting.outline(snapshot([sequenceRule(steps: 2, onExhausted: policy)])).sequences[0].footer
        }

        XCTAssertEqual(footer("error"), "Further requests return 500")
        XCTAssertEqual(footer("repeatLast"), "Further requests repeat step 2")
        XCTAssertEqual(footer("passThrough"), "Further requests pass through")
        XCTAssertEqual(
            footer("somethingNew"), "Further requests: somethingNew",
            "a newer engine's word is a fact this app does not know, not one it may rename")
    }

    func testASequenceWithNoPolicyPromisesNothingAboutWhatFollows() {
        // The engine always sends the policy that will actually apply, so a missing one is a
        // snapshot this app does not understand, and naming a behaviour would promise what the
        // proxy has not agreed to.
        XCTAssertNil(
            RuleFormatting.outline(snapshot([sequenceRule(onExhausted: nil)])).sequences[0].footer)
    }

    func testAFiftyStepSequenceStaysALinearListOfStates() {
        // One state a step and one transition between each pair: the outline grows with the sequence
        // and not with its square.
        let container = RuleFormatting.outline(
            snapshot([sequenceRule(steps: 50, advanceOn: RuleMatch(method: "POST", path: "/next"))])
        ).sequences[0]

        XCTAssertEqual(container.states.count, 50)
        XCTAssertEqual(container.states.filter { $0.transition != nil }.count, 50)
        XCTAssertEqual(container.states.map(\.id), Array(1...50), "numbers are positions, and unique")
    }

    // MARK: - Where a click lands, and what a pick belongs to

    func testAnOutlineClickStaysInTheScenarioItWasDrawnFrom() {
        // Rules with the same id in different scenarios must remain distinct navigation targets.
        XCTAssertEqual(
            RuleFormatting.destination(rule: "ovr_cart", drawnFrom: "checkout"),
            RuleFormatting.Destination(scenario: "checkout", selection: .rule("ovr_cart"), step: nil))
        XCTAssertEqual(
            RuleFormatting.destination(rule: "ovr_cart", step: 3, drawnFrom: "checkout"),
            RuleFormatting.Destination(scenario: "checkout", selection: .rule("ovr_cart"), step: 3),
            "a state row opens its rule at the step it is")
    }

    func testAStepPickBelongsToTheScenarioAndTheRuleItWasMadeOn() {
        // A pick is a step *number*, and step 3 of another rule — or of a rule of the same id in
        // another scenario — is a different step. Keyed by both, so nothing has to remember to clear
        // it, and a click that opens a rule *at* a step is not undone by the selection change that
        // arrives with it.
        let rule = sequenceRule(steps: 3)
        func shown(_ pick: RuleFormatting.StepPick?) -> Int? {
            RuleFormatting.shownStep(rule, in: "orders-outage", pick: pick)
        }

        XCTAssertEqual(shown(nil), 1)
        XCTAssertEqual(shown(.init(scenario: "orders-outage", rule: "ovr_order_states", step: 2)), 2)
        XCTAssertEqual(
            shown(.init(scenario: "checkout", rule: "ovr_order_states", step: 2)), 1,
            "another scenario's rule of the same id is another rule")
        XCTAssertEqual(
            shown(.init(scenario: "orders-outage", rule: "ovr_other", step: 2)), 1,
            "another rule's pick names nothing here")
        XCTAssertEqual(
            shown(.init(scenario: "orders-outage", rule: "ovr_order_states", step: 9)), 1,
            "and a rule reloaded with fewer steps does not leave the pane past the end of them")
        XCTAssertNil(
            RuleFormatting.shownStep(replaceRule(), in: "orders-outage", pick: nil),
            "a rule with no steps shows none")
    }

    func testTheStepSelectorIsLabelledAndItsMenuCarriesTheStatus() {
        // A segmented row of digits says which step is chosen but not what any of them answers with;
        // beyond six the menu has the room to say both.
        XCTAssertEqual(RuleFormatting.stepSelectorLabel(step: 2, of: 9), "Step 2 of 9")
        XCTAssertEqual(
            RuleFormatting.stepMenuLabel(number: 3, step: StepSummary(status: 200, bodyKind: "json")),
            "Step 3 · 200")
        XCTAssertEqual(
            RuleFormatting.stepMenuLabel(number: 3, step: StepSummary(bodyKind: "none")), "Step 3",
            "a step whose status the engine did not send is not given one here")
    }

    // MARK: - List selection

    func testASelectedScenarioOpensOnItsFirstResponseAndKeepsARuleTheReaderPicked() {
        let shot = snapshot([replaceRule(), patchRule()])

        XCTAssertEqual(RuleFormatting.selection(current: nil, in: shot), .rule("ovr_orders"))
        XCTAssertEqual(RuleFormatting.selection(current: .rule("ovr_flags"), in: shot), .rule("ovr_flags"))
        XCTAssertEqual(RuleFormatting.selection(current: .rule("ovr_gone"), in: shot), .rule("ovr_orders"))
        XCTAssertEqual(
            RuleFormatting.selection(current: .rule("ovr_flags"), in: nil), .rule("ovr_flags"),
            "a read in flight is no reason to move what the reader is looking at")
    }

    // MARK: - What the rules column draws

    func testEmptyScenarioRetainsItsListWithAnExplanation() {
        let empty = RulesSnapshot(scenario: "baseline", notWhole: [], rules: [])

        guard
            case .list(let shown, let note) = RuleFormatting.rulesColumn(
                status: .intercepting, read: .ok(empty), controlPort: 8088)
        else { return XCTFail("a scenario that answered with no rules is still a scenario") }
        XCTAssertEqual(shown.scenario, "baseline")
        XCTAssertEqual(note?.message, "baseline has no rules yet.")
    }

    func testUnavailableSnapshotsReplaceTheRulesList() {
        for read in [MockClient.RulesRead.unsupported, .unavailable("timed out"), nil] {
            guard
                case .vacancy = RuleFormatting.rulesColumn(
                    status: .intercepting, read: read, controlPort: 8088)
            else { return XCTFail("a read that produced no snapshot offered a list") }
        }
        guard
            case .vacancy(let down) = RuleFormatting.rulesColumn(
                status: .down, read: .ok(snapshot([replaceRule()])), controlPort: 8088)
        else { return XCTFail("a proxy that is not running replaces the column") }
        XCTAssertEqual(down.message, "Proxy is not running.")
    }
}
