import Testing

@testable import Lyrebird

struct ScenarioOutlineTests {
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

    /// A sequence that waits 250 ms, the PATCH that advances it waiting 3 s, and two standalone
    /// rules — one delayed, one not — so a row's delay can only come from the rule it belongs to.
    private var delayedScenario: RulesSnapshot {
        snapshot([
            sequenceRule(
                id: "ovr_orders", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders"),
                delayMs: 250),
            replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204, delayMs: 3000),
            replaceRule(id: "ovr_read", method: "GET", path: "/api/items", status: 200),
            replaceRule(id: "ovr_slow", method: "GET", path: "/api/slow", status: 200, delayMs: 3000),
        ])
    }

    private func snapshot(_ rules: [RuleRow], scenario: String = "orders-outage") -> RulesSnapshot {
        RulesSnapshot(scenario: scenario, notWhole: [], rules: rules)
    }

    // MARK: - The lines

    @Test
    func aReplaceRuleReadsAsWhatItReturnsAndWhatThatIs() {
        let rule = replaceRule(status: 200, delayMs: 1000)

        #expect(RuleFormatting.requestLine(rule.match) == RequestLine(method: "GET", path: "/api/v1/orders"))
        #expect(RuleFormatting.behaviourLine(rule.rewrite) == "Returns 200")
        #expect(RuleFormatting.delayLabel(rule.rewrite) == "1 s")
        #expect(RuleFormatting.clauseLine(rule.rewrite) == "", "only a patch has clauses")
        #expect(RuleFormatting.metaLine(rule.rewrite) == "JSON · 251 B")
    }

    @Test func aPatchHasNoBodyOfItsOwnToDescribe() {
        // It answers with the upstream's body merged, which this engine has not seen —
        // `describe_rewrite` reports `bodyKind` "none" for every patch for exactly that reason, and
        // rendered that read as "No body" over a rule that answers with whatever the real server
        // sent.
        #expect(RuleFormatting.metaLine(patchRule().rewrite) == nil)
        #expect(RuleFormatting.metaLine(replaceRule().rewrite) == "JSON · 251 B")
    }

    @Test func everyModeSaysTheDelayItWaits() {
        // The proxy applies the delay before it looks at the mode, so a patch and a sequenced rule
        // wait exactly as a `replace` does; two of the three branches used to drop it.
        #expect(RuleFormatting.delayLabel(replaceRule(status: 200, delayMs: 1000).rewrite) == "1 s")
        #expect(RuleFormatting.delayLabel(patchRule(delayMs: 1000).rewrite) == "1 s")
        #expect(RuleFormatting.delayLabel(sequenceRule(steps: 2, delayMs: 250).rewrite) == "250 ms")
    }

    @Test func theResponsePaneStillSaysTheWaitTheRowShowsAsABadge() {
        // The pane draws no badge. Splitting the delay out of `behaviourLine` for the list took it
        // out of the pane as well, and `Returns 200` there described a rule that answers a second
        // later.
        #expect(
            RuleFormatting.behaviourSentence(replaceRule(status: 200, delayMs: 1000).rewrite)
                == "Returns 200 after 1 s")
        #expect(
            RuleFormatting.behaviourSentence(patchRule(delayMs: 1000).rewrite)
                == "Patches the real response · 3 keys after 1 s")
        #expect(
            RuleFormatting.behaviourSentence(replaceRule(status: 200).rewrite) == "Returns 200",
            "no delay, nothing to append")
        #expect(
            RuleFormatting.behaviourSentence(replaceRule(status: nil, delayMs: 1000).rewrite) == "",
            "a rule with nothing to say is not described by its delay alone")
    }

    @Test func theBehaviourLineLeavesTheDelayToTheBadgeBesideIt() {
        // Both are shown on one row now. A line that also spelled the delay out read `Returns 200
        // after 1 s` next to a badge saying `1 s`, which is one wait described as two.
        #expect(RuleFormatting.behaviourLine(replaceRule(status: 200, delayMs: 1000).rewrite) == "Returns 200")
        #expect(
            RuleFormatting.behaviourLine(patchRule(delayMs: 1000).rewrite) == "Patches the real response · 3 keys")
        #expect(RuleFormatting.behaviourLine(sequenceRule(steps: 2, delayMs: 250).rewrite) == "Sequence · 2 steps")
    }

    @Test
    func aCappedDelaySaysSoRatherThanReadingAsTheOneItWasSetTo() {
        // The engine reports the delay it will *apply* — 60 s, whatever the rule asked for — so the
        // flag is the only thing that says the two differ, and without it a rule configured for two
        // minutes reads as one configured for exactly a minute.
        #expect(
            RuleFormatting.delayLabel(replaceRule(status: 200, delayMs: 60000, delayCapped: true).rewrite)
                == "60 s (capped)")
        #expect(
            RuleFormatting.delayLabel(replaceRule(status: 200, delayMs: 60000).rewrite) == "60 s",
            "a rule that asked for exactly the ceiling was not capped")
    }

    @Test func aDelayIsSaidInTheUnitAReaderThinksIn() {
        #expect(RuleFormatting.seconds(1000) == "1 s")
        #expect(RuleFormatting.seconds(1500) == "1.5 s")
        #expect(RuleFormatting.seconds(250) == "250 ms")
        #expect(
            RuleFormatting.delayLabel(replaceRule(status: 204, delayMs: 0).rewrite) == nil,
            "a delay of nothing is not a delay")
        #expect(RuleFormatting.delayLabel(replaceRule(status: 204).rewrite) == nil)
    }

    @Test func aBodylessAnswerSaysSoRatherThanSizingNothing() {
        // "none 0 B" would describe a payload no request receives.
        #expect(RuleFormatting.metaLine(kind: "none", bytes: nil) == "No body")
        #expect(RuleFormatting.metaLine(kind: "text", bytes: 12) == "Text · 12 B")
        #expect(RuleFormatting.metaLine(kind: "json", bytes: nil) == "JSON")
    }

    @Test
    func aPatchNamesWhatItMergesIntoAndUnderWhatCondition() {
        // A patch needs a buffered JSON upstream response to merge into; without one the request is
        // passed through untouched and recorded as `patchSkipped`, so "JSON responses only" is a
        // condition of the mode and belongs on every patch.
        let rule = patchRule(status: 503, keys: 3, strategy: "appendToArray")

        #expect(RuleFormatting.behaviourLine(rule.rewrite) == "Patches the real response · 3 keys")
        #expect(
            RuleFormatting.clauseLine(rule.rewrite) == "sets status to 503 · appends to arrays · JSON responses only")
    }

    @Test
    func aPatchClaimsNoStatusItDoesNotForceAndNoKeyCountItWasNotSent() {
        // `rewrite.status` is null for a patch that keeps the real response's, and a number there
        // would be a claim about a response this engine has never seen. `patchKeys` is null only in
        // a snapshot this app does not understand, and "0 keys" would describe a patch that changes
        // nothing.
        #expect(RuleFormatting.clauseLine(patchRule(status: nil, keys: 1).rewrite) == "JSON responses only")
        #expect(
            RuleFormatting.behaviourLine(patchRule(status: nil, keys: 1).rewrite) == "Patches the real response · 1 key"
        )
        #expect(RuleFormatting.behaviourLine(patchRule(status: nil, keys: nil).rewrite) == "Patches the real response")
    }

    @Test func aSequenceReadsAsItsStepCount() {
        #expect(RuleFormatting.behaviourLine(sequenceRule(steps: 2).rewrite) == "Sequence · 2 steps")
        #expect(RuleFormatting.behaviourLine(sequenceRule(steps: 1).rewrite) == "Sequence · 1 step")
    }

    @Test
    func aRuleWhoseStatusTheEngineDidNotSendSaysNothingAboutOne() {
        // The engine sends a status for every validated `replace`, so a missing one is a summary
        // this app does not understand — and "Returns" with nothing after it is a sentence cut off
        // mid-way.
        #expect(RuleFormatting.behaviourLine(replaceRule(status: nil).rewrite) == "")
    }

    @Test func conditionsAreListedInFullHoweverManyThereAre() {
        // A matcher is the set of requests a rule answers. "+2 conditions" describes a rule the
        // reader then has to go and look up, and a pin cut in half looks like a pin that is there.
        let rule = replaceRule(
            path: "/api/v1/checkout/payment/authorisation",
            query: ["provider": .string("card"), "attempt": .int(2), "region": .string("eu-west")],
            bodyContains: "three-d-secure")

        #expect(
            RuleFormatting.conditionLines(rule.match) == [
                RuleFormatting.Fact("Query", "attempt = 2 · provider = card · region = eu-west"),
                RuleFormatting.Fact("Body contains", "\"three-d-secure\""),
            ])
        #expect(RuleFormatting.conditionLines(RuleMatch(path: "/a")).isEmpty)
        #expect(
            RuleFormatting.conditionLines(RuleMatch(bodyContains: "")).isEmpty,
            "an empty substring constrains nothing on the wire, and `specificity` does not count it")
    }

    @Test
    func aBareMatcherSaysWhatItMatchesRatherThanNothing() {
        #expect(RuleFormatting.requestLine(nil) == RequestLine(method: "ANY", path: "*"))
    }

    // MARK: - The outline

    @Test func plainRulesKeepTheirConfiguredOrder() {
        let outline = RuleFormatting.outline(snapshot([replaceRule(), patchRule()]))

        #expect(outline.sequences.isEmpty)
        #expect(outline.otherRules.map(\.id) == ["ovr_orders", "ovr_flags"])
    }

    @Test func sequencesAreSeparatedFromOtherRules() {
        let outline = RuleFormatting.outline(snapshot([sequenceRule(), replaceRule()]))

        #expect(outline.sequences.map(\.id) == ["ovr_order_states"])
        #expect(outline.otherRules.map(\.id) == ["ovr_orders"])
    }

    @Test func emptyScenariosHaveNoFlow() {
        let outline = RuleFormatting.outline(snapshot([], scenario: "baseline"))

        #expect(outline.sequences.isEmpty)
        #expect(outline.otherRules.isEmpty)
    }

    @Test
    func anInactiveRuleIsLabelledAndListedAfterTheOnesThatAnswer() {
        // Being switched off is configuration, not runtime: still listed — a rule you cannot find is
        // a rule you will write a second time — and labelled rather than hidden.
        let outline = RuleFormatting.outline(
            snapshot([replaceRule(id: "ovr_off", active: false, path: "/api/v1/profile"), replaceRule()]))

        #expect(outline.otherRules.map(\.id) == ["ovr_orders", "ovr_off"])
        #expect(outline.otherRules.map(\.inactive) == [false, true])
    }

    // MARK: - A sequence container

    @Test
    func aSelfAdvancingSequenceSaysSoOnceAndPutsNothingBetweenItsStates() {
        // Each answer is the move, so there is nothing to say between two states; saying it there,
        // once per state, would be the same sentence N times.
        let container = RuleFormatting.outline(snapshot([sequenceRule(steps: 3)])).sequences[0]

        #expect(container.advanceNote == "Advances after each answer")
        #expect(container.states.map(\.number) == [1, 2, 3])
        #expect(container.states.allSatisfy { $0.transition == nil })
    }

    @Test func anExplicitTriggerIsABlockAfterEveryState() throws {
        // After each of them, the last included: a trigger is what moves the cursor off that state
        // too, and the footer says what a request meets once it has.
        let container = RuleFormatting.outline(
            snapshot([
                sequenceRule(
                    steps: 3,
                    advanceOn: RuleMatch(method: "PATCH", path: "/api/v2/orders"))
            ])
        ).sequences[0]

        #expect(container.advanceNote == nil, "the blocks say it, where it happens")
        #expect(container.states.map { $0.transition != nil } == [true, true, true])
        let transition = try #require(container.states[0].transition)
        #expect(transition.request == RequestLine(method: "PATCH", path: "/api/v2/orders"))
    }

    @Test
    func theAdvanceMatchersConditionsAreShownWhateverElseIs() {
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

        #expect(container.states[0].transition?.conditions == [RuleFormatting.Fact("Query", "id = order-42")])
    }

    @Test
    func scenarioNotesBelongToTheBrowsedScenarioAndBlankNotesStayHidden() {
        let scenarios = ScenarioList(
            active: "active",
            scenarios: [
                ScenarioSummary(name: "active", overrideCount: 1, verified: false, notes: "Active description"),
                ScenarioSummary(
                    name: "browsed", overrideCount: 2, verified: false, notes: "  Shows a retry after failure.\n"),
                ScenarioSummary(name: "empty", overrideCount: 0, verified: false, notes: " \n "),
            ])
        #expect(RuleFormatting.scenarioNotes("browsed", in: scenarios) == "Shows a retry after failure.")
        #expect(RuleFormatting.scenarioNotes("empty", in: scenarios) == nil)
        #expect(RuleFormatting.scenarioNotes("missing", in: scenarios) == nil)
        #expect(RuleFormatting.scenarioNotes("browsed", in: nil) == nil)
    }

    @Test
    func responseModesDescribeTheRewriteRatherThanTheHTTPMethod() {
        let replacement = replaceRule(method: "PATCH")
        #expect(RuleFormatting.responseKind(replacement.rewrite) == .replace)
        #expect(RuleFormatting.responseKind(patchRule().rewrite) == .patch)
        #expect(RuleFormatting.responseKind(sequenceRule(steps: 2).rewrite) == .sequence)
        var unknown = replacement.rewrite
        unknown.mode = "future-mode"
        #expect(RuleFormatting.responseKind(unknown) == .unknown("future-mode"))
        let shot = snapshot([
            sequenceRule(id: "ovr_orders", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
            replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204),
        ])
        #expect(RuleFormatting.flowSections(shot)[0].rows.map(\.responseKind) == [.sequence, .replace, .sequence])
    }

    @Test
    func flowListRowsOpenTheCorrectRuleAndStepAndDoNotDuplicateTheTrigger() {
        let shot = snapshot([
            sequenceRule(id: "ovr_orders", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
            replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204),
        ])
        let sections = RuleFormatting.flowSections(shot)
        #expect(sections.count == 1)
        let rows = sections[0].rows
        #expect(rows.map(\.number) == [1, 2, 3])
        #expect(rows.map(\.ruleId) == ["ovr_orders", "ovr_update", "ovr_orders"])
        #expect(rows.map(\.step) == [1, nil, 2])
        #expect(RuleFormatting.detailRule(selection: rows[2].selection, in: shot)?.id == "ovr_orders")
        #expect(RuleFormatting.flowRow(rows[2].selection, in: shot)?.step == 2)
        #expect(RuleFormatting.selection(current: nil, in: shot) == rows[0].selection)
        #expect(RuleFormatting.selection(current: rows[2].selection, in: shot) == rows[2].selection)
    }

    @Test
    func everyRowThatAnswersSaysHowLongItIsHeld() {
        // The delay lived only in the detail pane, so a list of rules said nothing about the one
        // fact that explains a screen sitting still. A sequence's own delay is every step's — the
        // engine refuses a `delayMs` on a step — and a trigger's is its response rule's, which is
        // also where the trigger row's status comes from.
        let rows = RuleFormatting.flowSections(delayedScenario).flatMap(\.rows)

        #expect(rows.map(\.delay) == ["250 ms", "3 s", "250 ms", nil, "3 s"])
        // Exact subtitles, because `subtitle: rule.line` would pass an assertion that only looked
        // for the absence of a duplicate on rows that have no delay to duplicate.
        #expect(rows[3].subtitle == "Returns 200")
        #expect(rows[4].subtitle == "Returns 200", "the badge carries the wait; the subtitle says it once")
        #expect(
            rows.allSatisfy { row in row.delay.map { !row.subtitle.contains($0) } ?? true },
            "a subtitle repeating the badge states one wait twice")
    }

    @Test
    func aTriggerWithNoIdentifiedResponseNamesNoDelay() {
        // The wait belongs to the rule that answers the trigger. With none identified there is no
        // rule to read one off, and the sequence's own delay is a different rule's wait.
        let shot = snapshot([
            sequenceRule(
                id: "ovr_orders", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders"),
                delayMs: 250)
        ])
        let rows = RuleFormatting.flowSections(shot).flatMap(\.rows)

        #expect(rows[1].ruleId == nil)
        #expect(rows[1].delay == nil)
    }

    @Test
    func aCandidateResponseInTheDetailPaneStillReadsAsOneSentence() {
        // The pane draws a line of text where the list draws a badge, so the delay has to go back
        // into the words there — otherwise splitting it out quietly dropped it from that button.
        let outline = RuleFormatting.outline(
            snapshot([
                sequenceRule(steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
                replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204, delayMs: 3000),
            ]))
        let candidate = outline.sequences[0].states[0].transition?.relatedResponses.first

        #expect(candidate?.line == "Returns 204 after 3 s")
        #expect(
            ScenarioOutline.RuleSummary(
                id: "ovr_x", request: RequestLine(method: "GET", path: "/"), conditions: [], behaviour: "",
                inactive: false, status: nil, delay: "3 s"
            ).line == "", "a rule with nothing to say is not described by its delay alone")
    }

    @Test
    func sequenceIDsCannotCollideWithTheOtherResponsesSection() {
        let sections = RuleFormatting.flowSections(
            snapshot([
                sequenceRule(id: "other-rules"), replaceRule(),
            ]))
        #expect(sections.count == 2)
        #expect(Set(sections.map(\.id)).count == 2)
    }

    @Test
    func ambiguousTriggerResponsesRemainAccessibleOutsideTheFlow() {
        let sections = RuleFormatting.flowSections(
            snapshot([
                sequenceRule(advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
                replaceRule(id: "first", method: "PATCH", path: "/api/orders", query: ["id": .int(1)]),
                replaceRule(id: "second", method: "PATCH", path: "/api/orders", query: ["id": .int(2)]),
            ]))
        let trigger = sections[0].rows[1]
        #expect(trigger.ruleId == nil)
        #expect(trigger.status == nil)
        #expect(trigger.transition?.relatedResponses.map(\.id) == ["first", "second"])
        #expect(sections[1].rows.map(\.ruleId) == ["first", "second"])
    }

    @Test
    func anInactiveResponseDoesNotDisableTheAdvanceRequestOrClaimItsStatus() {
        var response = replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders")
        response.rewrite.active = false
        let sections = RuleFormatting.flowSections(
            snapshot([
                sequenceRule(steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
                response,
            ]))
        let trigger = sections[0].rows[1]
        #expect(!trigger.inactive)
        #expect(trigger.status == nil)
        #expect(trigger.ruleId == nil)
        #expect(trigger.responseKind == nil)
        #expect(trigger.transition?.relatedResponses.map(\.id) == ["ovr_update"])
        #expect(sections[1].rows.map(\.ruleId) == ["ovr_update"])
        #expect(sections[1].rows[0].inactive)
    }

    @Test(arguments: [1, 3])
    func theFinalResponseRetainsItsEndingMatcherWithoutAnExtraFlowRow(count: Int) {
        let matcher = RuleMatch(
            method: "PATCH", path: "/api/orders/ack", query: ["id": .int(42)], bodyContains: "confirmed")
        let rows = RuleFormatting.flowSections(
            snapshot([
                sequenceRule(steps: count, advanceOn: matcher)
            ]))[0].rows
        #expect(rows.count == count * 2 - 1)
        #expect(rows.last?.step == count)
        #expect(rows.last?.transition == nil)
        #expect(rows.last?.endingTransition?.request == RuleFormatting.requestLine(matcher))
        #expect(rows.last?.endingTransition?.conditions == RuleFormatting.conditionLines(matcher))
        #expect(rows.dropLast().allSatisfy { $0.endingTransition == nil })
    }

    @Test
    func sharedTriggersKeepDistinctFlowSelectionsAndUnresolvedTriggersHaveNoInventedResponse() {
        let shot = snapshot([
            sequenceRule(id: "ovr_one", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
            sequenceRule(id: "ovr_two", steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
        ])
        let sections = RuleFormatting.flowSections(shot)
        #expect(sections.count == 2)
        #expect(sections[0].rows[1].selection != sections[1].rows[1].selection)
        for section in sections {
            let trigger = section.rows[1]
            #expect(trigger.ruleId == nil)
            #expect(trigger.status == nil)
            #expect(trigger.transition != nil)
            #expect(RuleFormatting.detailRule(selection: trigger.selection, in: shot) == nil)
        }
        #expect(RuleFormatting.selection(current: nil, in: snapshot([])) == nil)
    }

    @Test
    func requestFlowInterleavesReadsAndTriggersWithoutTreatingTheEndingAsAnotherStep() {
        let outline = RuleFormatting.outline(
            snapshot([
                sequenceRule(steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
                replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204),
            ]))
        let sequence = outline.sequences[0]
        #expect(sequence.flow.map(\.id) == ["response-1", "trigger-1", "response-2"])
        guard case .trigger(_, let transition) = sequence.flow[1] else {
            Issue.record("missing PATCH")
            return
        }
        #expect(transition.request.method == "PATCH")
        #expect(transition.relatedResponses.first?.status == 204)
        #expect(sequence.states.last?.transition != nil, "the final trigger belongs in the ending disclosure")
        guard case .response(let state) = sequence.flow[2] else {
            Issue.record("missing second response")
            return
        }
        #expect(state.number == 2, "flow row 3 must still open sequence step 2")
    }

    @Test
    func aTriggerIncludesItsConditionalResponseWithoutNarrowingAdvancement() throws {
        var response = replaceRule(id: "ovr_update", method: "PATCH", path: "/api/orders", status: 204)
        response.match = RuleMatch(method: "PATCH", path: "/api/orders", query: ["id": .string("42")])
        let outline = RuleFormatting.outline(
            snapshot([
                sequenceRule(steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/orders")),
                response,
                replaceRule(id: "ovr_unrelated", method: "PATCH", path: "/api/items"),
                replaceRule(id: "ovr_read", method: "GET", path: "/api/orders"),
            ]))
        let transition = try #require(outline.sequences[0].states[0].transition)
        #expect(transition.conditions.isEmpty)
        #expect(transition.relatedResponses.map(\.id) == ["ovr_update"])
        #expect(transition.relatedResponses[0].conditions == [.init("Query", "id = 42")])
        #expect(transition.relatedResponses[0].behaviour == "Returns 204")
        #expect(transition.relatedResponses[0].line == "Returns 204", "no delay, nothing to append")
        #expect(outline.otherRules.contains { $0.id == "ovr_update" })
        #expect(outline.sequences[0].states[1].transition == transition)
    }

    @Test
    func relatedResponseRulesKeepAllCandidatesAndInactiveLabels() throws {
        var inactive = replaceRule(id: "ovr_disabled", method: "PATCH", path: "/api/orders")
        inactive.rewrite.active = false
        let outline = RuleFormatting.outline(
            snapshot([
                sequenceRule(steps: 1, advanceOn: RuleMatch(method: "patch", path: "/api/orders")),
                inactive,
                replaceRule(id: "ovr_enabled", method: "PATCH", path: "/api/orders"),
            ]))
        let candidates = try #require(outline.sequences[0].states[0].transition).relatedResponses
        #expect(candidates.map(\.id) == ["ovr_disabled", "ovr_enabled"])
        #expect(candidates[0].inactive)
        #expect(!candidates[1].inactive)
    }

    @Test
    func aRuleThatAnswersATriggerIsStillOneOfTheScenariosRules() {
        // It answers requests of its own whether or not any sequence is running, and leaving it out
        // of the list would hide it from the one place that claims to hold every rule.
        let outline = RuleFormatting.outline(
            snapshot([
                sequenceRule(
                    steps: 2, advanceOn: RuleMatch(method: "PATCH", path: "/api/v2/ack")),
                replaceRule(id: "ovr_ack", method: "PATCH", path: "/api/v2/ack"),
            ]))

        #expect(outline.otherRules.map(\.id) == ["ovr_ack"])
    }

    @Test
    func aOneStepSequenceStillShowsWhatMovesItPastItsOnlyState() {
        // The transition used to be drawn only *before* another state, so a one-step sequence lost
        // the whole of what moves it on — and every sequence lost the trigger that carries it into
        // exhaustion.
        let one = RuleFormatting.outline(
            snapshot([sequenceRule(steps: 1, advanceOn: RuleMatch(method: "PATCH", path: "/api/v2/ack"))])
        ).sequences[0]
        #expect(one.states.count == 1)
        #expect(one.states[0].transition != nil)

        let three = RuleFormatting.outline(
            snapshot([sequenceRule(steps: 3, advanceOn: RuleMatch(method: "PATCH", path: "/api/v2/ack"))])
        ).sequences[0]
        #expect(three.states.map { $0.transition != nil } == [true, true, true])
        #expect(
            RuleFormatting.outline(snapshot([sequenceRule(steps: 2)])).sequences[0]
                .states.allSatisfy { $0.transition == nil },
            "a self-advancing sequence still has nothing to put between its states")
    }

    @Test(
        arguments: [
            ("error", "Further requests return 500"),
            ("repeatLast", "Further requests repeat step 2"),
            ("passThrough", "Further requests pass through"),
            ("somethingNew", "Further requests: somethingNew"),
        ])
    func eachExhaustionPolicyDescribesWhatHappensAfterTheFinalResponse(policy: String, expected: String) {
        let sequence = RuleFormatting.outline(snapshot([sequenceRule(steps: 2, onExhausted: policy)])).sequences[0]
        #expect(sequence.footer == expected)
    }

    @Test
    func aSequenceWithNoPolicyPromisesNothingAboutWhatFollows() {
        // The engine always sends the policy that will actually apply, so a missing one is a
        // snapshot this app does not understand, and naming a behaviour would promise what the
        // proxy has not agreed to.
        #expect(RuleFormatting.outline(snapshot([sequenceRule(onExhausted: nil)])).sequences[0].footer == nil)
    }

    @Test func aFiftyStepSequenceStaysALinearListOfStates() {
        // One state a step and one transition between each pair: the outline grows with the sequence
        // and not with its square.
        let container = RuleFormatting.outline(
            snapshot([sequenceRule(steps: 50, advanceOn: RuleMatch(method: "POST", path: "/next"))])
        ).sequences[0]

        #expect(container.states.count == 50)
        #expect(container.states.filter { $0.transition != nil }.count == 50)
        #expect(container.states.map(\.id) == Array(1...50), "numbers are positions, and unique")
    }

    // MARK: - Where a click lands, and what a pick belongs to

    @Test
    func anOutlineClickStaysInTheScenarioItWasDrawnFrom() {
        // Rules with the same id in different scenarios must remain distinct navigation targets.
        #expect(
            RuleFormatting.destination(rule: "ovr_cart", drawnFrom: "checkout")
                == RuleFormatting.Destination(scenario: "checkout", selection: .rule("ovr_cart"), step: nil))
        #expect(
            RuleFormatting.destination(rule: "ovr_cart", step: 3, drawnFrom: "checkout")
                == RuleFormatting.Destination(scenario: "checkout", selection: .rule("ovr_cart"), step: 3),
            "a state row opens its rule at the step it is")
    }

    @Test
    func aStepPickBelongsToTheScenarioAndTheRuleItWasMadeOn() {
        // A pick is a step *number*, and step 3 of another rule — or of a rule of the same id in
        // another scenario — is a different step. Keyed by both, so nothing has to remember to clear
        // it, and a click that opens a rule *at* a step is not undone by the selection change that
        // arrives with it.
        let rule = sequenceRule(steps: 3)
        func shown(_ pick: RuleFormatting.StepPick?) -> Int? {
            RuleFormatting.shownStep(rule, in: "orders-outage", pick: pick)
        }

        #expect(shown(nil) == 1)
        #expect(shown(.init(scenario: "orders-outage", rule: "ovr_order_states", step: 2)) == 2)
        #expect(
            shown(.init(scenario: "checkout", rule: "ovr_order_states", step: 2)) == 1,
            "another scenario's rule of the same id is another rule")
        #expect(
            shown(.init(scenario: "orders-outage", rule: "ovr_other", step: 2)) == 1,
            "another rule's pick names nothing here")
        #expect(
            shown(.init(scenario: "orders-outage", rule: "ovr_order_states", step: 9)) == 1,
            "and a rule reloaded with fewer steps does not leave the pane past the end of them")
        #expect(
            RuleFormatting.shownStep(replaceRule(), in: "orders-outage", pick: nil) == nil,
            "a rule with no steps shows none")
    }

    @Test
    func theStepSelectorIsLabelledAndItsMenuCarriesTheStatus() {
        // A segmented row of digits says which step is chosen but not what any of them answers with;
        // beyond six the menu has the room to say both.
        #expect(RuleFormatting.stepSelectorLabel(step: 2, of: 9) == "Step 2 of 9")
        #expect(
            RuleFormatting.stepMenuLabel(number: 3, step: StepSummary(status: 200, bodyKind: "json")) == "Step 3 · 200")
        #expect(
            RuleFormatting.stepMenuLabel(number: 3, step: StepSummary(bodyKind: "none")) == "Step 3",
            "a step whose status the engine did not send is not given one here")
    }

    // MARK: - List selection

    @Test
    func aSelectedScenarioOpensOnItsFirstResponseAndKeepsARuleTheReaderPicked() {
        let shot = snapshot([replaceRule(), patchRule()])

        #expect(RuleFormatting.selection(current: nil, in: shot) == .rule("ovr_orders"))
        #expect(RuleFormatting.selection(current: .rule("ovr_flags"), in: shot) == .rule("ovr_flags"))
        #expect(RuleFormatting.selection(current: .rule("ovr_gone"), in: shot) == .rule("ovr_orders"))
        #expect(
            RuleFormatting.selection(current: .rule("ovr_flags"), in: nil) == .rule("ovr_flags"),
            "a read in flight is no reason to move what the reader is looking at")
    }

    // MARK: - What the rules column draws

    @Test func emptyScenarioRetainsItsListWithAnExplanation() {
        let empty = RulesSnapshot(scenario: "baseline", notWhole: [], rules: [])

        guard
            case .list(let shown, let note) = RuleFormatting.rulesColumn(
                status: .intercepting, read: .ok(empty), controlPort: 8088)
        else {
            Issue.record("a scenario that answered with no rules is still a scenario")
            return
        }
        #expect(shown.scenario == "baseline")
        #expect(note?.message == "baseline has no rules yet.")
    }

    @Test func unavailableSnapshotsReplaceTheRulesList() {
        for read in [MockClient.RulesRead.unsupported, .unavailable("timed out"), nil] {
            guard
                case .vacancy = RuleFormatting.rulesColumn(
                    status: .intercepting, read: read, controlPort: 8088)
            else {
                Issue.record("a read that produced no snapshot offered a list")
                return
            }
        }
        guard
            case .vacancy(let down) = RuleFormatting.rulesColumn(
                status: .down, read: .ok(snapshot([replaceRule()])), controlPort: 8088)
        else {
            Issue.record("a proxy that is not running replaces the column")
            return
        }
        #expect(down.message == "Proxy is not running.")
    }
}
