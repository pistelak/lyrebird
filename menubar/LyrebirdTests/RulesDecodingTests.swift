import XCTest

@testable import Lyrebird

/// The window renders `GET /__mock__/rules` and derives nothing of its own, so what it can show is
/// exactly what these types decode. Each of these pins a shape the engine actually sends — a plain
/// rule with no sequence, a sequenced one, a rule that omits `active`, a browsed scenario with no
/// runtime at all — because a field silently lost in decoding does not look like a bug in a
/// read-only window: it looks like a rule that does less than it does.
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
    }

    func testAPatchWithoutASequencePreservesItsResponseDescription() throws {
        let rule = try XCTUnwrap(decoded().rules.first { $0.id == "ovr_flags" })

        XCTAssertNil(rule.rewrite.sequence)
        XCTAssertEqual(rule.rewrite.patchKeys, 1)
        XCTAssertNil(rule.rewrite.status, "a patch forcing no status keeps the real response's")
    }

    func testASequencedRuleCarriesEachStepAsTheWireAnswersIt() throws {
        // The steps arrive after inheritance and after `wire_response`: the status a step will
        // send, the headers it will send including the `Content-Type` a JSON body earns, and the
        // body itself. `inherited` names the fields it did not write — the second step's headers
        // are the rule's, so an edit aimed at the step would change nothing.
        let rule = try XCTUnwrap(decoded().rules.first { $0.id == "ovr_items" })

        let described = try XCTUnwrap(rule.rewrite.sequence)
        XCTAssertNil(described.advanceOn, "null is the implicit `self`, not a matcher this app invents")
        XCTAssertEqual(described.onExhausted, "repeatLast")
        XCTAssertEqual(described.steps.map(\.status), [201, 202])
        XCTAssertEqual(described.steps[0].inherited, ["body", "headers"])
        XCTAssertEqual(described.steps[1].headers, ["Content-Type": "application/json"])
        XCTAssertEqual(described.steps[1].body, .object(["items": .array([])]))
        XCTAssertEqual(described.steps[1].bodyKind, "json")
    }

    func testAnAdvanceMatcherArrivesAsTheRequestThatMovesTheCursor() throws {
        // `advanceOn` in the runtime row is the word "match", which tells a reader that *something
        // else* moves this sequence on without saying what. The matcher itself is what the pane
        // shows, and it arrives in `rewrite`.
        let payload = #"""
            {"scenario": "orders-outage", "active": true, "notWhole": [], "rules": [{
              "id": "ovr_orders", "mode": "replace",
              "match": {"path": "/api/orders"},
              "rewrite": {"active": true, "mode": "replace", "status": null, "bodyKind": "none", "sequence": {
                "advanceOn": {"method": "POST", "path": "/api/orders", "query": {"id": 7},
                              "bodyContains": "confirmed"},
                "onExhausted": "passThrough",
                "steps": [{"status": 200, "headers": {}, "body": null,
                           "bodyKind": "none", "bodyBytes": null, "inherited": ["body", "headers"]}]
              }},
              "answer": {"id": "ovr_orders", "active": true, "count": 0, "runId": "r7"},
              "sequenceState": {"id": "ovr_orders", "runId": "r7", "advanceOn": "match",
                                "nextStep": 1, "stepCount": 1, "exhausted": false,
                                "hasOverrun": false, "serves": {}}
            }]}
            """#

        let matcher = try XCTUnwrap(
            JSONDecoder().decode(RulesSnapshot.self, from: Data(payload.utf8))
                .rules[0].rewrite.sequence?.advanceOn)

        XCTAssertEqual(matcher.method, "POST")
        XCTAssertEqual(matcher.path, "/api/orders")
        XCTAssertEqual(matcher.query?["id"], .int(7))
        XCTAssertEqual(matcher.bodyContains, "confirmed")
    }

    func testAStepWhoseBodyWasTooLargeToRepeatSaysSoRatherThanReadingAsNone() throws {
        // The engine leaves a body over 256 KiB out and still reports its size, because every step
        // is described after inheritance and one large parent body would otherwise be repeated once
        // per step. A null body with no `bodyOmitted` means "answers with none"; these two facts
        // must not decode to the same thing.
        let payload = #"""
            {"scenario": "orders-outage", "active": true, "notWhole": [], "rules": [{
              "id": "ovr_big", "mode": "replace",
              "rewrite": {"active": true, "mode": "replace", "status": null, "bodyKind": "none", "sequence": {
                "advanceOn": null, "onExhausted": "error",
                "steps": [{"status": 200, "headers": {"Content-Type": "application/json"},
                           "body": null, "bodyOmitted": true, "bodyKind": "json",
                           "bodyBytes": 1258291, "inherited": []}]
              }},
              "answer": {"id": "ovr_big", "active": true, "count": 0, "runId": "r7"},
              "sequenceState": {"id": "ovr_big", "runId": "r7", "advanceOn": "self", "nextStep": 1,
                                "stepCount": 1, "exhausted": false, "hasOverrun": false,
                                "serves": {}}
            }]}
            """#

        let step = try XCTUnwrap(
            JSONDecoder().decode(RulesSnapshot.self, from: Data(payload.utf8))
                .rules[0].rewrite.sequence?.steps.first)

        XCTAssertEqual(step.bodyOmitted, true)
        XCTAssertNil(step.body)
        XCTAssertEqual(step.bodyBytes, 1_258_291)
        XCTAssertEqual(
            RuleFormatting.omittedBodyLine(bytes: step.bodyBytes), "body of 1.2 MB not included in the snapshot")
    }

    func testActivenessIsTheEnginesAnswerAndNotTheStoredField() throws {
        // `rewrite.active` is `rules.is_active` applied by the engine, on every row of every
        // scenario. The stored `active` beside it is not decoded at all: its encoding is not
        // obvious — a missing key means active and only a literal `false` switches a rule off — and
        // a client that reads it is a second implementation of a rule the engine owns. Pinned by
        // making the two disagree, which no real snapshot does.
        let disagreeing = #"""
            {"scenario": "orders-outage", "active": true, "notWhole": [], "rules": [{
              "id": "ovr_orders", "mode": "replace", "active": true,
              "match": {"path": "/api/v1/orders"},
              "rewrite": {"active": false, "mode": "replace", "status": 200, "bodyKind": "none", "sequence": null},
              "answer": {"id": "ovr_orders", "active": true, "count": 0, "runId": null},
              "sequenceState": null
            }]}
            """#
        let snapshot = try JSONDecoder().decode(RulesSnapshot.self, from: Data(disagreeing.utf8))

        XCTAssertFalse(
            snapshot.rules[0].isActive,
            "the stored field, or the run row, was read in place of the engine's own reading")
        XCTAssertEqual(RuleFormatting.grouped(snapshot.rules).inactive.map(\.id), ["ovr_orders"])
    }

    func testABrowsedScenariosRulesCarryTheirActivenessToo() throws {
        // The reason the field is on `rewrite` and not only on `answer`: a browsed scenario has no
        // run row, and reading activeness off one meant falling back to the stored field for every
        // rule the window shows while browsing — which was half of them.
        let browsed = try JSONDecoder().decode(RulesSnapshot.self, from: Data(RulesFixture.browsed.utf8))

        XCTAssertTrue(browsed.rules[0].isActive)
        XCTAssertFalse(browsed.rules[1].isActive)
        XCTAssertEqual(RuleFormatting.grouped(browsed.rules).inactive.map(\.id), ["ovr_cart_off"])
    }

    func testAPatchWithNoKeyCountSaysNothingRatherThanZero() throws {
        // The engine sends `patchKeys` for every patch, so a row without one is a summary this app
        // does not understand — and "merge 0 keys" describes a patch that changes nothing, which is
        // a claim about the rule rather than a gap in the line.
        let payload = #"""
            {"scenario": "orders-outage", "active": true, "notWhole": [], "rules": [{
              "id": "ovr_flags", "mode": "patch", "match": {"path": "/api/v1/features"},
              "rewrite": {"active": true, "mode": "patch", "status": null, "bodyKind": "none",
                          "patchKeys": null, "sequence": null},
              "answer": {"id": "ovr_flags", "active": true, "count": 0, "runId": null},
              "sequenceState": null
            }]}
            """#
        let rule = try JSONDecoder().decode(RulesSnapshot.self, from: Data(payload.utf8)).rules[0]

        XCTAssertNil(rule.rewrite.patchKeys)
        XCTAssertEqual(RuleFormatting.behaviourLine(rule.rewrite), "Patches the real response")
        XCTAssertEqual(RuleFormatting.clauseLine(rule.rewrite), "JSON responses only")
    }

    func testABrowsedScenarioDecodesItsRulesWithNoRuntimeAtAll() throws {
        // `?scenario=NAME` sends the rules and the engine's description of them, and null for every
        // cursor and count: those belong to the scenario the proxy is serving. Null and not zero —
        // "this rule answered nothing" is a claim about a run that happened.
        let snapshot = try JSONDecoder().decode(
            RulesSnapshot.self, from: Data(RulesFixture.browsed.utf8))

        XCTAssertEqual(snapshot.scenario, "checkout")
        XCTAssertEqual(snapshot.rules[0].rewrite.status, 200, "the rule's shape is still fully described")
    }

    func testAnIntegerTooLargeForADoubleIsPrintedBackExactly() throws {
        // Every number used to arrive through `Double`, which cannot hold an integer above 2^53:
        // 9007199254740993 came back as ...992, and the pane whose whole claim is "this is what the
        // scenario file says" showed a body the file does not contain.
        let rule = try XCTUnwrap(decoded().rules.first)
        let body = try XCTUnwrap(rule.body)

        XCTAssertTrue(
            RuleFormatting.jsonText(body).contains("9007199254740993"),
            RuleFormatting.jsonText(body))
    }

    func testRuntimeCounterChangesDoNotChangeTheConfiguredSnapshot() throws {
        let original = try decoded()
        var payload = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(RulesFixture.snapshot.utf8)) as? [String: Any])
        var rules = try XCTUnwrap(payload["rules"] as? [[String: Any]])
        for index in rules.indices {
            rules[index]["answer"] = ["active": true, "count": 900, "runId": "new-run"]
            rules[index]["sequenceState"] = ["nextStep": 1, "runId": "new-run", "serves": ["1": 900]]
        }
        payload["rules"] = rules
        let updated = try JSONDecoder().decode(
            RulesSnapshot.self, from: JSONSerialization.data(withJSONObject: payload))
        XCTAssertEqual(original, updated)
    }

    func testFieldsThisAppDoesNotRenderAreIgnoredRatherThanFailingTheWholeSnapshot() throws {
        // The fixture carries keys no version of this window reads. A newer engine adding one must
        // not turn the rules read into `.unavailable`, which is what a strict decode would do.
        XCTAssertEqual(try decoded().rules.count, 3)
    }

    func testAStepsBodyPrintsAsTheJsonItWasWrittenAs() throws {
        // The pane prints the step's body verbatim, so an empty array must not come back as `[ ]`
        // or a number as `202.0`: what is on screen has to paste back into the scenario file.
        let rule = try XCTUnwrap(decoded().rules.first { $0.id == "ovr_items" })
        let body = try XCTUnwrap(rule.rewrite.sequence?.steps[1].body)

        XCTAssertEqual(RuleFormatting.jsonText(body), "{\n  \"items\": []\n}")
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
          "active": true,
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
                "active": true, "mode": "replace", "status": 503, "bodyKind": "json", "bodyBytes": 1229,
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
                "active": false, "mode": "patch", "status": null, "bodyKind": "none", "bodyBytes": null,
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
                "active": true, "mode": "replace", "status": null, "bodyKind": "none", "bodyBytes": null,
                "patchKeys": null, "patchStrategy": null, "delayMs": null,
                "sequence": {
                  "advanceOn": null,
                  "onExhausted": "repeatLast",
                  "steps": [
                    {
                      "status": 201, "headers": {}, "body": null,
                      "bodyKind": "none", "bodyBytes": null, "inherited": ["body", "headers"]
                    },
                    {
                      "status": 202, "headers": {"Content-Type": "application/json"},
                      "body": {"items": []}, "bodyKind": "json", "bodyBytes": 13,
                      "inherited": ["headers"]
                    }
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

    /// A scenario the proxy is *not* serving: the same shape, with `active` false and every runtime
    /// slot null.
    static let browsed = #"""
        {
          "scenario": "checkout",
          "active": false,
          "notWhole": [],
          "rules": [
            {
              "id": "ovr_cart",
              "mode": "replace",
              "match": {"method": "GET", "path": "/api/v1/cart"},
              "status": 200,
              "body": {"items": []},
              "rewrite": {
                "active": true, "mode": "replace", "status": 200, "bodyKind": "json", "bodyBytes": 13,
                "patchKeys": null, "patchStrategy": null, "delayMs": null, "sequence": null
              },
              "answer": null,
              "sequenceState": null
            },
            {
              "id": "ovr_cart_off",
              "active": false,
              "mode": "replace",
              "match": {"path": "/api/v1/cart/legacy"},
              "rewrite": {
                "active": false, "mode": "replace", "status": 200, "bodyKind": "none", "bodyBytes": null,
                "patchKeys": null, "patchStrategy": null, "delayMs": null, "sequence": null
              },
              "answer": null,
              "sequenceState": null
            }
          ]
        }
        """#

    /// Two requests as `store.record_recent` files them, each under the id the engine gave it.
    static let recent = #"""
        [
          {"id": "evt-2", "time": "2026-09-08T10:41:07.123456+00:00", "method": "GET",
           "host": "api.example.com", "path": "/api/v1/orders", "status": 503,
           "matched": "ovr_orders"},
          {"id": "evt-1", "time": "2026-09-08T10:41:05+00:00", "method": "POST",
           "host": "api.example.com", "path": "/api/v1/items", "status": 201,
           "matched": "ovr_items", "sequenceId": "ovr_items", "runId": "r7",
           "selectedStep": 1, "stepCount": 2}
        ]
        """#

    /// The scenario list the sidebar draws from: one active, one browsable, one that did not load
    /// whole.
    static let scenarios = #"""
        {"active": "orders-outage", "scenarios": [
          {"name": "orders-outage", "overrideCount": 3, "verified": true, "notes": ""},
          {"name": "checkout", "overrideCount": 2, "verified": false, "notes": ""}
        ]}
        """#

    /// A health reading scoped to `ours`, so the model's gate opens and the rules read happens.
    static func health(fingerprint: String = ours, scenario: String = "orders-outage") -> Data {
        Data(
            (#"{"activeScenario":"\#(scenario)","overrideCount":3,"proxyUp":true,"intercepting":true,"#
                + #""proxyPort":8080,"scenariosNotWhole":{"orders-outage":["orders-outage: rule 4 skipped"]},"#
                + #""profileFingerprint":"\#(fingerprint)"}"#).utf8)
    }

    /// Plausible answers for every read a refresh makes, including the rules snapshot — the browsed
    /// one when the request named a scenario other than the active one.
    static func serve(_ request: URLRequest, fingerprint: String = ours) -> (HTTPURLResponse, Data) {
        let asked = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
            .queryItems?.first { $0.name == "scenario" }?.value
        switch request.url?.path {
        case "/__mock__/health": return (Stub.response(request, 200), health(fingerprint: fingerprint))
        case "/__mock__/recent": return (Stub.response(request, 200), Data(recent.utf8))
        case "/__mock__/scenarios": return (Stub.response(request, 200), Data(scenarios.utf8))
        case "/__mock__/rules":
            switch asked {
            case nil, "orders-outage": return (Stub.response(request, 200), Data(snapshot.utf8))
            case "checkout": return (Stub.response(request, 200), Data(browsed.utf8))
            default:
                let name = asked ?? ""
                let refusal =
                    #"{"error":"unknown_scenario","name":"\#(name)","#
                    + #""detail":"no scenario named '\#(name)' in this profile"}"#
                return (Stub.response(request, 404), Data(refusal.utf8))
            }
        default: return Stub.read(request)
        }
    }
}
