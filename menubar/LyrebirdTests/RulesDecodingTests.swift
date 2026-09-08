import XCTest

@testable import Lyrebird

/// The Rules window renders `GET /__mock__/rules` and derives nothing of its own, so what it can
/// show is exactly what these types decode. Each of these pins a shape the engine actually sends —
/// a plain rule with no sequence, a sequenced one, a rule that omits `active` — because a field
/// silently lost in decoding does not look like a bug in a read-only window: it looks like a rule
/// that does less than it does.
final class RulesDecodingTests: XCTestCase {

    private func decoded() throws -> RulesSnapshot {
        try JSONDecoder().decode(RulesSnapshot.self, from: Data(RulesFixture.snapshot.utf8))
    }

    func testASnapshotDecodesTheScenarioItsProblemsAndEveryRuleInOrder() throws {
        let snapshot = try decoded()

        XCTAssertEqual(snapshot.scenario, "orders-outage")
        XCTAssertEqual(snapshot.rules.map(\.id), ["ovr_orders", "ovr_flags", "ovr_items"])
        // The banner's whole purpose: a rule dropped at load time is invisible in the list of the
        // ones that survived, so the problem has to arrive beside them.
        XCTAssertEqual(snapshot.notWhole.count, 1)
        XCTAssertTrue(snapshot.notWhole[0].contains("statsu"), snapshot.notWhole[0])
    }

    func testAPlainRuleCarriesItsStoredFieldsAndTheEnginesDescriptionOfThem() throws {
        let rule = try XCTUnwrap(decoded().rules.first)

        XCTAssertEqual(rule.match?.method, "GET")
        XCTAssertEqual(rule.match?.path, "/api/v1/orders")
        // A query pin may be written as a number; the engine compares `str(value)`, so it is kept
        // as raw JSON rather than forced into a String and lost.
        XCTAssertEqual(rule.match?.query?["page"], .int(2))
        XCTAssertEqual(rule.headers?["Content-Type"], "application/json")
        XCTAssertEqual(
            rule.body,
            .object(["error": .string("orders is down"), "retryAfterNanos": .int(9_007_199_254_740_993)]))
        XCTAssertEqual(rule.notes, "what the app shows during the outage")
        XCTAssertEqual(rule.rewrite.status, 503)
        XCTAssertEqual(rule.rewrite.bodyKind, "json")
        XCTAssertEqual(rule.rewrite.bodyBytes, 1229)
        XCTAssertEqual(rule.rewrite.delayMs, 1000)
        XCTAssertEqual(rule.answer.count, 3)
        XCTAssertEqual(rule.answer.runId, "r7")
    }

    func testARuleWithNoSequenceDecodesBothSequenceSlotsAsAbsent() throws {
        // Null, not a zeroed cursor: a plain rule has no step to be on, and a `nextStep` of 0 or 1
        // invented here would render as a sequence waiting at its first step.
        let rule = try XCTUnwrap(decoded().rules.first { $0.id == "ovr_flags" })

        XCTAssertNil(rule.sequenceState)
        XCTAssertNil(rule.rewrite.sequence)
        XCTAssertNil(rule.sequence)
        XCTAssertEqual(rule.rewrite.patchKeys, 1)
        XCTAssertNil(rule.rewrite.status, "a patch forcing no status keeps the real response's")
    }

    func testASequencedRuleCarriesItsStepsBothAsStoredAndAsDescribed() throws {
        // Two readings of the same steps, and the window needs both: `rewrite.sequence.steps` says
        // what each step answers with *after* inheritance, and `sequence.steps` is what the file
        // says. Decoding only one leaves the detail pane unable to show one of them.
        let rule = try XCTUnwrap(decoded().rules.first { $0.id == "ovr_items" })

        let described = try XCTUnwrap(rule.rewrite.sequence)
        XCTAssertEqual(described.advanceOn, "self")
        XCTAssertEqual(described.onExhausted, "repeatLast")
        XCTAssertEqual(described.steps.map(\.status), [201, 202])
        XCTAssertEqual(described.steps[1].bodyKind, "json")
        XCTAssertEqual(rule.sequence?.steps?.count, 2)
        XCTAssertEqual(rule.sequenceState?.nextStep, 2)
        XCTAssertEqual(rule.sequenceState?.serves?["1"], 1)
        XCTAssertEqual(rule.answer.count, 1)
        XCTAssertEqual(rule.sequenceState?.runId, "r7")
    }

    func testWhetherARuleIsActiveComesFromTheEngineAndNotFromTheStoredField() throws {
        // `rules.is_active` reads an omitted `active` as true and only a literal `false` as off,
        // and `answer_states` already applies it to every rule in the scenario. Re-deriving it here
        // would be a second implementation of a rule the engine owns — so the stored field is not
        // decoded at all, and this pins that the rendered state follows `answer.active` even when
        // the two are made to disagree.
        let rules = try decoded().rules
        XCTAssertTrue(rules[0].isActive, "the fixture omits `active`, which the engine reads as on")
        XCTAssertFalse(try XCTUnwrap(rules.first { $0.id == "ovr_flags" }).isActive)

        let disagreeing = #"""
            {"scenario": "orders-outage", "notWhole": [], "rules": [{
              "id": "ovr_orders", "mode": "replace", "active": true,
              "match": {"path": "/api/v1/orders"},
              "rewrite": {"mode": "replace", "status": 200, "bodyKind": "none", "sequence": null},
              "answer": {"id": "ovr_orders", "active": false, "count": 0, "runId": null},
              "sequenceState": null
            }]}
            """#
        let snapshot = try JSONDecoder().decode(RulesSnapshot.self, from: Data(disagreeing.utf8))

        XCTAssertFalse(snapshot.rules[0].isActive, "the stored field was read in place of the engine's")
    }

    func testAnIntegerTooLargeForADoubleIsPrintedBackExactly() throws {
        // Every number used to arrive through `Double`, which cannot hold an integer above 2^53:
        // 9007199254740993 came back as ...992, and the pane whose whole claim is "this is what the
        // scenario file says" showed a body the file does not contain.
        let rule = try XCTUnwrap(decoded().rules.first)
        let body = try XCTUnwrap(rule.body)

        XCTAssertTrue(
            RuleFormatting.prettyJSON(body).contains("9007199254740993"),
            RuleFormatting.prettyJSON(body))
    }

    func testFieldsThisAppDoesNotRenderAreIgnoredRatherThanFailingTheWholeSnapshot() throws {
        // The fixture carries keys no version of this window reads. A newer engine adding one must
        // not turn the rules read into `.unavailable`, which is what a strict decode would do.
        XCTAssertEqual(try decoded().rules.count, 3)
    }

    func testAStoredStepPrintsAsTheJsonItWasWrittenAs() throws {
        // The detail pane prints the step verbatim, so `202` must not come back as `202.0`: the
        // pane's claim is that this is what the scenario file says.
        let rule = try XCTUnwrap(decoded().rules.first { $0.id == "ovr_items" })
        let step = try XCTUnwrap(rule.sequence?.steps?.first)

        XCTAssertEqual(RuleFormatting.prettyJSON(step), "{\n  \"status\" : 201\n}")
    }
}

/// One snapshot in the shape `control.rules_snapshot` sends: the rule as stored, spread, with
/// `rewrite`, `answer` and `sequenceState` beside it. RFC 2606 hosts and `ovr_`-style ids only.
enum RulesFixture {
    static let ours = "a1b2c3"
    static let theirs = "d4e5f6"

    static let snapshot = #"""
        {
          "scenario": "orders-outage",
          "notWhole": ["orders-outage: rule 4 skipped — unknown field 'statsu'"],
          "engineFieldThisAppDoesNotRead": true,
          "rules": [
            {
              "id": "ovr_orders",
              "mode": "replace",
              "match": {"method": "GET", "path": "/api/v1/orders", "query": {"page": 2}},
              "status": 503,
              "delayMs": 1000,
              "headers": {"Content-Type": "application/json"},
              "body": {"error": "orders is down", "retryAfterNanos": 9007199254740993},
              "notes": "what the app shows during the outage",
              "rewrite": {
                "mode": "replace", "status": 503, "bodyKind": "json", "bodyBytes": 1229,
                "patchKeys": null, "patchStrategy": null, "delayMs": 1000, "sequence": null,
                "somethingNewerEnginesSend": 1
              },
              "answer": {"id": "ovr_orders", "active": true, "count": 3, "runId": "r7"},
              "sequenceState": null
            },
            {
              "id": "ovr_flags",
              "active": false,
              "mode": "patch",
              "match": {"path": "/api/v1/features"},
              "patch": {"checkout_v2": true},
              "rewrite": {
                "mode": "patch", "status": null, "bodyKind": "none", "bodyBytes": null,
                "patchKeys": 1, "patchStrategy": null, "delayMs": null, "sequence": null
              },
              "answer": {"id": "ovr_flags", "active": false, "count": 0, "runId": null},
              "sequenceState": null
            },
            {
              "id": "ovr_items",
              "mode": "replace",
              "match": {"path": "/api/v1/items"},
              "sequence": {
                "steps": [{"status": 201}, {"status": 202, "body": {"items": []}}],
                "onExhausted": "repeatLast"
              },
              "rewrite": {
                "mode": "replace", "status": null, "bodyKind": "none", "bodyBytes": null,
                "patchKeys": null, "patchStrategy": null, "delayMs": null,
                "sequence": {
                  "advanceOn": "self",
                  "onExhausted": "repeatLast",
                  "steps": [
                    {"status": 201, "bodyKind": "none", "bodyBytes": null, "headerCount": 0},
                    {"status": 202, "bodyKind": "json", "bodyBytes": 13, "headerCount": 1}
                  ]
                }
              },
              "answer": {"id": "ovr_items", "active": true, "count": 1, "runId": "r7"},
              "sequenceState": {
                "id": "ovr_items", "runId": "r7", "advanceOn": "self", "nextStep": 2,
                "stepCount": 2, "exhausted": false, "hasOverrun": false, "serves": {"1": 1}
              }
            }
          ]
        }
        """#

    /// A health reading scoped to `ours`, so the model's gate opens and the rules read happens.
    static func health(fingerprint: String = ours) -> Data {
        Data(
            (#"{"activeScenario":"orders-outage","overrideCount":3,"proxyUp":true,"intercepting":true,"#
                + #""proxyPort":8080,"profileFingerprint":"\#(fingerprint)"}"#).utf8)
    }

    /// Plausible answers for every read a refresh makes, including the rules snapshot.
    static func serve(_ request: URLRequest, fingerprint: String = ours) -> (HTTPURLResponse, Data) {
        switch request.url?.path {
        case "/__mock__/health": return (Stub.response(request, 200), health(fingerprint: fingerprint))
        case "/__mock__/rules": return (Stub.response(request, 200), Data(snapshot.utf8))
        default: return Stub.read(request)
        }
    }
}
