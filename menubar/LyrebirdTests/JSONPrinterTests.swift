import XCTest

@testable import Lyrebird

/// The detail pane's claim is that the block it shows is what the scenario file says, and the Copy
/// button hands that text to someone who will paste it back into a scenario file. A printer of our
/// own is what makes the block readable and coloured; these are what stop it from being readable and
/// wrong.
final class JSONPrinterTests: XCTestCase {

    /// Every shape the printer has a branch for, in one value.
    ///
    /// No integral `Double` on purpose: `.number(2.0)` would print `2.0` and decode back as
    /// `.int(2)`, and it cannot arise in the first place — a `JSONValue` only ever comes from a JSON
    /// decode, which reads `2.0` as an `Int` too.
    private let value = JSONValue.object([
        "nested": .object([
            "list": .array([.int(1), .string("two"), .bool(true), .null]),
            "empty object": .object([:]),
            "empty array": .array([]),
        ]),
        "quotes": .string(#"she said "no" \ then left"#),
        "whitespace": .string("first\nsecond\tthird"),
        "control": .string("bell\u{01}here"),
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

    func testAStringIsEscapedTheWayJsonEscapesIt() {
        XCTAssertEqual(
            RuleFormatting.jsonText(.string("a\"b\\c\nd\te\u{01}f\u{08}g")),
            #""a\"b\\c\nd\te\u0001f\bg""#)
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
