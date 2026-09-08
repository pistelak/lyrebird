import XCTest

@testable import Lyrebird

/// The detail pane's claim is that the block it shows is what the scenario file says, and the Copy
/// button hands that text to someone who will paste it back into a scenario file. A printer of our
/// own is what makes the block readable and coloured; these are what stop it from being readable and
/// wrong.
final class JSONPrinterTests: XCTestCase {

    /// Every shape the printer has a branch for, in one value.
    ///
    /// No integral `Double` on purpose: `.number(2.0)` cannot arise — see
    /// `testWhatTheWireCanCarryIsWhatThePrinterEverSees`, which pins that on the decode side.
    private let value = JSONValue.object([
        "nested": .object([
            "list": .array([.int(1), .string("two"), .bool(true), .null]),
            "empty object": .object([:]),
            "empty array": .array([]),
        ]),
        "quotes": .string(#"she said "no" \ then left"#),
        "whitespace": .string("first\nsecond\tthird"),
        "control": .string("bell\u{0001}here"),
        "not ascii": .string("smörgås — ✓"),
        "big": .int(9_007_199_254_740_993),
        "negative": .int(-42),
        "fraction": .number(-2.5),
        "flag": .bool(false),
        "nothing": .null,
    ])

    func testThePrintedJsonParsesBackToTheValueItCameFrom() throws {
        // Two claims in one: that the output is JSON at all — escaping, commas, the lot — and that
        // nothing was lost on the way out. A printer that renders a tab literally, or drops a digit
        // from an integer above 2^53, produces a block that reads perfectly and pastes back as a
        // different rule.
        let text = RuleFormatting.jsonText(value)
        let data = try XCTUnwrap(text.data(using: .utf8))

        XCTAssertNoThrow(try JSONSerialization.jsonObject(with: data), text)
        XCTAssertEqual(try JSONDecoder().decode(JSONValue.self, from: data), value, text)
    }

    func testAnObjectPrintsSortedKeysTwoSpaceIndentAndOneSpaceAfterTheColon() {
        let rule = JSONValue.object([
            "status": .int(503),
            "body": .object(["error": .string("orders is down")]),
            "active": .bool(false),
        ])

        XCTAssertEqual(
            RuleFormatting.jsonText(rule),
            """
            {
              "active": false,
              "body": {
                "error": "orders is down"
              },
              "status": 503
            }
            """)
    }

    func testAnEmptyObjectOrArrayStaysOnOneLine() {
        // Three lines for `{}` reads as a container whose contents failed to render.
        XCTAssertEqual(RuleFormatting.jsonText(.object([:])), "{}")
        XCTAssertEqual(RuleFormatting.jsonText(.array([])), "[]")
        XCTAssertEqual(RuleFormatting.jsonText(.object(["items": .array([])])), "{\n  \"items\": []\n}")
    }

    func testEveryEscapeAndLiteralTheWireCanCarryIsPrintedAndReadsBack() throws {
        // A table rather than one long string, so a wrong escape names the character it got wrong.
        // DEL and the two Unicode separators are legal unescaped in JSON, so leaving them literal is
        // a choice — pinned here either way, and a change to it has to be deliberate.
        let cases: [(what: String, text: String, printed: String)] = [
            ("quote", "a\"b", #""a\"b""#),
            ("backslash", "a\\b", #""a\\b""#),
            ("newline", "a\nb", #""a\nb""#),
            ("tab", "a\tb", #""a\tb""#),
            ("carriage return", "a\rb", #""a\rb""#),
            ("form feed", "a\u{000C}b", #""a\fb""#),
            ("backspace", "a\u{0008}b", #""a\bb""#),
            ("start of heading", "a\u{0001}b", #""a\u0001b""#),
            ("unit separator", "a\u{001F}b", #""a\u001fb""#),
            ("delete", "a\u{007F}b", "\"a\u{007F}b\""),
            ("line separator", "a\u{2028}b", "\"a\u{2028}b\""),
            ("paragraph separator", "a\u{2029}b", "\"a\u{2029}b\""),
            ("emoji outside the BMP", "a\u{1F54A}b", "\"a\u{1F54A}b\""),
        ]

        for one in cases {
            let value = JSONValue.object(["k": .string(one.text)])
            let printed = RuleFormatting.jsonText(value)
            let data = Data(printed.utf8)

            XCTAssertEqual(printed, "{\n  \"k\": \(one.printed)\n}", one.what)
            XCTAssertNoThrow(try JSONSerialization.jsonObject(with: data), one.what)
            XCTAssertEqual(try JSONDecoder().decode(JSONValue.self, from: data), value, one.what)
        }
    }

    func testAKeyIsEscapedTheSameWayAValueIs() throws {
        // Keys go through the same `quoted`, and a key carrying a quote is the one that breaks the
        // block into invalid JSON if it does not.
        let value = JSONValue.object([#"a"b"#: .int(1)])
        let printed = RuleFormatting.jsonText(value)

        XCTAssertEqual(
            printed,
            #"""
            {
              "a\"b": 1
            }
            """#)
        XCTAssertEqual(try JSONDecoder().decode(JSONValue.self, from: Data(printed.utf8)), value)
    }

    func testNestingIsIndentedTwoSpacesPerContainer() {
        var value = JSONValue.int(1)
        for _ in 0..<5 { value = .object(["n": value]) }

        XCTAssertEqual(
            RuleFormatting.jsonText(value),
            """
            {
              "n": {
                "n": {
                  "n": {
                    "n": {
                      "n": 1
                    }
                  }
                }
              }
            }
            """)
    }

    func testWhatTheWireCanCarryIsWhatThePrinterEverSees() throws {
        // `attributedJSON` says its input is bounded by what a JSON decode produces; this is that
        // claim checked on the decode side, where it is made. `JSONValue` tries `Int` before
        // `Double`, so every whole number arrives as `.int` however it was written, and `.number`
        // holds only a genuine fraction — which is why the printer never meets an integral double,
        // and why JSON's lack of an infinity or NaN literal is the whole of the non-finite story.
        let wire = #"{"whole": 2.0, "exponent": 1e3, "negativeZero": -0.0, "fraction": 2.5e-1}"#
        let value = try JSONDecoder().decode(JSONValue.self, from: Data(wire.utf8))
        guard case .object(let members) = value else { return XCTFail("the wire sent an object") }

        XCTAssertEqual(members["whole"], .int(2))
        XCTAssertEqual(members["exponent"], .int(1000))
        // The sign is already gone by the time the app is handed the value: the wire says 0, exactly
        // as it would for `0`, so there is nothing here that could have kept it.
        XCTAssertEqual(members["negativeZero"], .int(0))
        XCTAssertEqual(members["fraction"], .number(0.25))

        let printed = RuleFormatting.jsonText(value)
        XCTAssertEqual(
            try JSONDecoder().decode(JSONValue.self, from: Data(printed.utf8)), value, printed)
    }

    func testAnIntegerTooLargeForADoubleKeepsEveryDigit() {
        // `JSONSerialization` routed such a number through `Double` and printed ...992.
        XCTAssertEqual(RuleFormatting.jsonText(.int(9_007_199_254_740_993)), "9007199254740993")
    }

    func testTheColouredPrintCarriesTheSameCharactersAsTheCopiedText() {
        // The pane renders the attributed print and the Copy button sends `jsonText`. They are one
        // implementation today; this fails the moment someone gives them two.
        XCTAssertEqual(
            String(RuleFormatting.attributedJSON(value).characters), RuleFormatting.jsonText(value))
    }

    func testEveryKindOfValueIsColouredSoTheBlockCanBeSkimmed() {
        let printed = RuleFormatting.attributedJSON(.object(["a": .string("s"), "b": .int(1), "c": .null]))
        let colours = Set(printed.runs.compactMap(\.foregroundColor))

        // Keys, strings, numbers, literals and punctuation: five readings, five colours, or the
        // colouring is decoration rather than information.
        XCTAssertEqual(colours.count, 5, "\(colours)")
    }
}
