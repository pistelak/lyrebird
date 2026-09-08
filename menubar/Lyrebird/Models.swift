import Foundation

/// Only the fields the menu actually renders. Unknown JSON keys are ignored by the decoder.
struct Health: Codable, Sendable {
    var activeScenario: String?
    var overrideCount: Int?
    var proxyUp: Bool?
    var intercepting: Bool?
    /// The port the proxy listens on, shown in the Rules window's header strip.
    var proxyPort: Int?
    /// Comes from the active profile, so the app never carries a default app identifier of its own.
    var simBundleId: String?
    /// Which profile the answering proxy is running. The app never computes this — it is
    /// `sha256(profile dir)[:12]` in the engine's `config`, and re-deriving it here would be a
    /// second implementation of a rule only the engine owns. Optional because an older engine
    /// does not send it, which the CLI accepts too.
    var profileFingerprint: String?
}

struct ScenarioSummary: Codable, Sendable, Identifiable {
    var name: String
    var overrideCount: Int
    var verified: Bool
    var notes: String?
    var id: String { name }
}

struct ScenarioList: Codable, Sendable {
    var active: String
    var scenarios: [ScenarioSummary]
}

struct RecentEntry: Codable, Sendable {
    var method: String
    var path: String
    var status: Int
    var matched: String?
}

// MARK: - The rules snapshot
//
// `GET /__mock__/rules` hands back every rule of the active scenario as stored, plus three things
// the engine derives and no client may re-derive: `rewrite` (what the rule answers with), `answer`
// and `sequenceState` (what it has done and where its cursor is). Everything below decodes only
// what the Rules window renders; unknown keys are ignored, so a newer engine adding a field does
// not stop the window reading the ones it knows.

/// Arbitrary JSON, kept so a rule's body, patch or step can be shown exactly as it is stored.
/// Nothing here interprets it — the engine owns what a rule means, and this type exists only so a
/// payload shaped like nothing in particular can still be printed.
enum JSONValue: Codable, Sendable, Equatable {
    case null
    case bool(Bool)
    /// Whole numbers keep their own case. `Double` cannot hold an integer above 2^53 exactly, so a
    /// body containing 9007199254740993 was printed back as 9007199254740992 — a payload the
    /// scenario does not contain, shown by a window whose whole claim is that this is what the file
    /// says. See testAnIntegerTooLargeForADoubleIsPrintedBackExactly.
    case int(Int)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        // Bool first: `NSNumber(true).intValue` is 1, so a `true` offered to `Int` decodes as one
        // on Darwin and would print as a number the file does not contain. `Int` then `Double`,
        // because `Int` refuses a fractional number but `Double` accepts a large integer lossily.
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "not a JSON value this app can carry")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }
}

/// Which requests a rule answers. Every field is optional because every constraint is: a rule with
/// no matcher at all answers every intercepted request, and `validate_override` allows it.
struct RuleMatch: Codable, Sendable {
    var method: String? = nil
    var path: String? = nil
    /// Values may be numbers as well as strings — the engine compares `str(value)` — so they are
    /// carried as raw JSON rather than forced into `String`.
    var query: [String: JSONValue]? = nil
    var bodyContains: String? = nil
}

/// One sequence step as the engine describes it, *after* it inherits from the parent rule.
struct StepSummary: Codable, Sendable {
    var status: Int? = nil
    var bodyKind: String? = nil
    var bodyBytes: Int? = nil
    var headerCount: Int? = nil
}

/// The sequence half of `rewrite`: how the cursor moves, what happens when it runs out, and what
/// each step answers with.
struct RewriteSequence: Codable, Sendable {
    /// "self" (advances when this rule answers) or "match" (advances on its own matcher).
    var advanceOn: String? = nil
    /// The policy that will actually apply, defaulted by the engine — never the raw omitted field.
    var onExhausted: String? = nil
    var steps: [StepSummary]
}

/// The engine's own description of what a rule answers with. The window renders this and derives
/// nothing from the stored fields beside it: step inheritance, the 200 a `replace` defaults to and
/// the wire encoding of a body are the engine's rules, and a second reading of them here would
/// drift silently — see the `GET /rules` bullet in engine/README.md.
struct Rewrite: Codable, Sendable {
    var mode: String? = nil
    /// Nil for a sequenced rule (its steps carry their own) and for a patch forcing none (the real
    /// response's status is kept, and this engine has not seen it).
    var status: Int? = nil
    var bodyKind: String? = nil
    var bodyBytes: Int? = nil
    var patchKeys: Int? = nil
    var patchStrategy: String? = nil
    var delayMs: Int? = nil
    var sequence: RewriteSequence? = nil
}

/// The sequence as *stored*, whose steps the detail pane prints verbatim. `rewrite.sequence`
/// describes them; this is what the scenario file says.
struct RuleSequence: Codable, Sendable {
    var steps: [JSONValue]? = nil
}

/// The engine's row for this rule in `store.answer_states`: whether it is a rule the engine will
/// consider at all, how many requests it has answered, and in which run.
///
/// `active` is the engine's `rules.is_active`, and it is read from here rather than from the stored
/// `active` field beside it. The encoding is not obvious — an omitted key means active, and only a
/// literal `false` switches a rule off — and a client re-deriving it is a second implementation of
/// a rule the engine owns, which is the drift this whole snapshot exists to prevent.
///
/// `runId` nil means the rule has no run at all — a different fact from a count of zero, which says
/// the run happened and the rule answered nothing.
struct AnswerState: Codable, Sendable {
    var active: Bool
    var count: Int
    var runId: String? = nil
}

/// Where a sequenced rule's cursor is right now. `nextStep` is 1-based, and nil when the sequence
/// is exhausted.
struct SequenceState: Codable, Sendable {
    var runId: String? = nil
    var advanceOn: String? = nil
    var nextStep: Int? = nil
    var stepCount: Int? = nil
    var exhausted: Bool? = nil
    var hasOverrun: Bool? = nil
    /// Keyed by the step number as a string, the way the engine reports it.
    var serves: [String: Int]? = nil
}

/// One rule: the override as stored, spread, with the engine's description and runtime state added.
struct RuleRow: Codable, Sendable, Identifiable {
    var id: String
    var match: RuleMatch? = nil
    var mode: String? = nil
    var delayMs: Int? = nil
    var status: Int? = nil
    var headers: [String: String]? = nil
    var body: JSONValue? = nil
    var patch: JSONValue? = nil
    var patchStrategy: String? = nil
    var notes: String? = nil
    var sequence: RuleSequence? = nil
    var rewrite: Rewrite
    /// Not optional: `control.rules_snapshot` reads `answer_states`, which returns a row for every
    /// rule in the active scenario, so a snapshot missing one is not a snapshot this window can
    /// describe — it fails to decode and the read becomes `.unavailable`, which is the truth.
    var answer: AnswerState
    var sequenceState: SequenceState? = nil

    /// Whether the engine will consider this rule — its answer, not this app's reading of the
    /// stored `active` field, which is deliberately not decoded at all.
    var isActive: Bool { answer.active }
}

struct RulesSnapshot: Codable, Sendable {
    var scenario: String
    /// The problems recorded against this scenario at load time. Rendered as a banner, because a
    /// rule that was dropped otherwise looks exactly like a scenario that is one rule shorter.
    var notWhole: [String]
    var rules: [RuleRow]
}
