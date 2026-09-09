import Foundation
import Testing

@testable import Lyrebird

/// The detail pane's claim is that the block it shows is what the scenario file says, and the Copy
/// button hands that text to someone who will paste it back into a scenario file. A printer of our
/// own is what makes the block readable and coloured; these are what stop it from being readable and
/// wrong.
struct JSONPrinterTests {

    /// Every shape the printer has a branch for, in one value.
    ///
    /// No integral `Double` on purpose: `.number(2.0)` cannot arise — see
    /// `What the wire can carry is what the printer ever sees`, which pins that on the decode side.
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

    @Test func thePrintedJSONParsesBackToTheValueItCameFrom()
        throws
    {
        // Two claims in one: that the output is JSON at all — escaping, commas, the lot — and that
        // nothing was lost on the way out. A printer that renders a tab literally, or drops a digit
        // from an integer above 2^53, produces a block that reads perfectly and pastes back as a
        // different rule.
        let text = RuleFormatting.jsonText(value)
        let data = try #require(text.data(using: .utf8))

        #expect(throws: Never.self, Comment(rawValue: text)) { try JSONSerialization.jsonObject(with: data) }
        #expect(try JSONDecoder().decode(JSONValue.self, from: data) == value, Comment(rawValue: text))
    }

    @Test
    func anObjectPrintsSortedKeysTwoSpaceIndentAndOneSpaceAfterTheColon() {
        let rule = JSONValue.object([
            "status": .int(503),
            "body": .object(["error": .string("orders is down")]),
            "active": .bool(false),
        ])

        #expect(
            RuleFormatting.jsonText(rule) == """
                {
                  "active": false,
                  "body": {
                    "error": "orders is down"
                  },
                  "status": 503
                }
                """)
    }

    @Test func anEmptyObjectOrArrayStaysOnOneLine() {
        // Three lines for `{}` reads as a container whose contents failed to render.
        #expect(RuleFormatting.jsonText(.object([:])) == "{}")
        #expect(RuleFormatting.jsonText(.array([])) == "[]")
        #expect(RuleFormatting.jsonText(.object(["items": .array([])])) == "{\n  \"items\": []\n}")
    }

    private static let escapes: [(what: String, text: String, printed: String)] = [
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

    @Test(arguments: escapes)
    func everyEscapeAndLiteralTheWireCanCarryIsPrintedAndReadsBack(
        one: (what: String, text: String, printed: String)
    ) throws {
        let value = JSONValue.object(["k": .string(one.text)])
        let printed = RuleFormatting.jsonText(value)
        let data = Data(printed.utf8)

        #expect(printed == "{\n  \"k\": \(one.printed)\n}", Comment(rawValue: one.what))
        #expect(throws: Never.self, Comment(rawValue: one.what)) { try JSONSerialization.jsonObject(with: data) }
        #expect(try JSONDecoder().decode(JSONValue.self, from: data) == value, Comment(rawValue: one.what))
    }

    @Test func aKeyIsEscapedTheSameWayAValueIs() throws {
        // Keys go through the same `quoted`, and a key carrying a quote is the one that breaks the
        // block into invalid JSON if it does not.
        let value = JSONValue.object([#"a"b"#: .int(1)])
        let printed = RuleFormatting.jsonText(value)

        #expect(
            printed == #"""
                {
                  "a\"b": 1
                }
                """#)
        #expect(try JSONDecoder().decode(JSONValue.self, from: Data(printed.utf8)) == value)
    }

    @Test func nestingIsIndentedTwoSpacesPerContainer() {
        var value = JSONValue.int(1)
        for _ in 0..<5 { value = .object(["n": value]) }

        #expect(
            RuleFormatting.jsonText(value) == """
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

    @Test func whatTheWireCanCarryIsWhatThePrinterEverSees()
        throws
    {
        // `attributedJSON` says its input is bounded by what a JSON decode produces; this is that
        // claim checked on the decode side, where it is made. `JSONValue` tries `Int` before
        // `Double`, so every whole number arrives as `.int` however it was written, and `.number`
        // holds only a genuine fraction — which is why the printer never meets an integral double,
        // and why JSON's lack of an infinity or NaN literal is the whole of the non-finite story.
        let wire = #"{"whole": 2.0, "exponent": 1e3, "negativeZero": -0.0, "fraction": 2.5e-1}"#
        let value = try JSONDecoder().decode(JSONValue.self, from: Data(wire.utf8))
        let members: [String: JSONValue]? = if case .object(let members) = value { members } else { nil }
        let object = try #require(members, "The wire sent an object")

        #expect(object["whole"] == .int(2))
        #expect(object["exponent"] == .int(1000))
        // The sign is already gone by the time the app is handed the value: the wire says 0, exactly
        // as it would for `0`, so there is nothing here that could have kept it.
        #expect(object["negativeZero"] == .int(0))
        #expect(object["fraction"] == .number(0.25))

        let printed = RuleFormatting.jsonText(value)
        #expect(try JSONDecoder().decode(JSONValue.self, from: Data(printed.utf8)) == value, Comment(rawValue: printed))
    }

    @Test func anIntegerTooLargeForADoubleKeepsEveryDigit() {
        // `JSONSerialization` routed such a number through `Double` and printed ...992.
        #expect(RuleFormatting.jsonText(.int(9_007_199_254_740_993)) == "9007199254740993")
    }

    @Test
    func theColouredPrintCarriesTheSameCharactersAsTheCopiedText() {
        // The pane renders the attributed print and the Copy button sends `jsonText`. They are one
        // implementation today; this fails the moment someone gives them two.
        #expect(String(RuleFormatting.attributedJSON(value).characters) == RuleFormatting.jsonText(value))
    }

    @Test
    func everyKindOfValueIsColouredSoTheBlockCanBeSkimmed() {
        let printed = RuleFormatting.attributedJSON(.object(["a": .string("s"), "b": .int(1), "c": .null]))
        let colours = Set(printed.runs.compactMap(\.foregroundColor))

        // Keys, strings, numbers, literals and punctuation: five readings, five colours, or the
        // colouring is decoration rather than information.
        #expect(colours.count == 5, "\(colours)")
    }
}
