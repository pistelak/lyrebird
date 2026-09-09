import Foundation

/// Only the fields the app actually renders. Unknown JSON keys are ignored by the decoder.
struct Health: Codable, Sendable, Equatable {
    var activeScenario: String?
    var overrideCount: Int?
    var proxyUp: Bool?
    var intercepting: Bool?
    /// The control port the proxy is listening on, named in the window's "another profile holds it"
    /// line so the reader knows which port to run `lyrebird down` against.
    var proxyPort: Int?
    /// Comes from the active profile, so the app never carries a default app identifier of its own.
    var simBundleId: String?
    /// Which profile the answering proxy is running. The app never computes this — it is
    /// `sha256(profile dir)[:12]` in the engine's `config`, and re-deriving it here would be a
    /// second implementation of a rule only the engine owns. Optional because an older engine
    /// does not send it, which the CLI accepts too.
    var profileFingerprint: String?
    /// The load problems keyed by the scenario each belongs to. Read from here rather than from the
    /// flat `loadProblems`, for the reason the engine keeps both: a file named
    /// `orders-outage.json: backup.json` leaves a line that begins exactly like a problem with
    /// `orders-outage`, and the sidebar would mark a scenario that loaded whole.
    var scenariosNotWhole: [String: [String]]?
}

struct ScenarioSummary: Codable, Sendable, Equatable, Identifiable {
    var name: String
    var overrideCount: Int
    var verified: Bool
    var notes: String?
    var id: String { name }
}

struct ScenarioList: Codable, Sendable, Equatable {
    var active: String
    var scenarios: [ScenarioSummary]
}

/// One request the proxy saw, as `store.record_recent` filed it. Only `method`, `path` and
/// `status` are always there; the rest say what Lyrebird did about it, and each is absent rather
/// than false when it did not — see `addon._record`.
struct RecentEntry: Codable, Sendable, Equatable {
    struct Key: Hashable {
        var id: String
        var time: String?
    }

    var selectionKey: Key? { id.map { Key(id: $0, time: time) } }

    /// Engine event ID. Older engines omit it, so their rows remain readable but not selectable.
    var id: String? = nil
    /// Engine timestamp; together with the counter, distinguishes events after a proxy restart.
    var time: String? = nil
    var method: String
    var path: String
    var status: Int
    /// The rule that answered. Nil for a request no override answered, which includes an exhausted
    /// `passThrough` — it stands aside, so it has no `matched` however much it moved.
    var matched: String? = nil
    /// Why a patch could not be applied. A dropped patch and "no rule matched" look identical on
    /// the wire, which is why the engine records this at all.
    var patchSkipped: String? = nil
    var overrun: Bool? = nil
    /// The sequences this request moved on — ids, and possibly none of them the rule that answered.
    var advanced: [String]? = nil
    /// Which step served this request at the time. The detail
    /// pane shows the recorded numbers rather than today's cursor: the point of the row is what
    /// happened, and the cursor has moved since.
    var selectedStep: Int? = nil
    var runId: String? = nil
}

// MARK: - Rules snapshot

// Render the engine-derived rewrite. Ignore runtime counters and unknown fields so traffic
// updates do not invalidate an unchanged configured flow.

/// Preserves stored bodies and patches for display without interpreting response behavior.
enum JSONValue: Codable, Sendable, Equatable {
    case null
    case bool(Bool)
    /// Avoid Double rounding integers above 2^53.
    /// See `RulesDecodingTests`.
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
struct RuleMatch: Codable, Sendable, Equatable {
    var method: String? = nil
    var path: String? = nil
    /// Values may be numbers as well as strings — the engine compares `str(value)` — so they are
    /// carried as raw JSON rather than forced into `String`.
    var query: [String: JSONValue]? = nil
    var bodyContains: String? = nil
}

/// Effective response after the engine applies step inheritance and wire encoding.
struct StepSummary: Codable, Sendable, Equatable {
    var status: Int? = nil
    var headers: [String: String]? = nil
    /// Nil either because the answer carries no body or because it was too large to repeat once per
    /// step; `bodyOmitted` is what tells the two apart, and the pane must not show the second as
    /// the first — see `StepResponseTests`.
    var body: JSONValue? = nil
    var bodyOmitted: Bool? = nil
    /// Always sent, for the reason `Rewrite.bodyKind` is.
    var bodyKind: String
    var bodyBytes: Int? = nil
    /// Which of `status`, `headers` and `body` this step took from the rule rather than writing
    /// itself. Shown beside the block, so an edit aimed at the step is not aimed at nothing.
    var inherited: [String]? = nil
}

/// The sequence half of `rewrite`: how the cursor moves, what happens when it runs out, and what
/// each step answers with.
struct RewriteSequence: Codable, Sendable, Equatable {
    /// Nil advances on this rule's own answers; otherwise only this matcher advances it.
    var advanceOn: RuleMatch? = nil
    /// The policy that will actually apply, defaulted by the engine — never the raw omitted field.
    var onExhausted: String? = nil
    var steps: [StepSummary]
}

/// Engine-derived response behavior, including defaults, inheritance and wire encoding.
struct Rewrite: Codable, Sendable, Equatable {
    /// Required engine interpretation of activeness; a missing field must not default to active.
    var active: Bool
    var mode: String? = nil
    /// Nil for a sequenced rule (its steps carry their own) and for a patch forcing none (the real
    /// response's status is kept, and this engine has not seen it).
    var status: Int? = nil
    /// `"none"`, `"text"` or `"json"` — always sent, never absent, so it is not optional: a summary
    /// with no body kind is one this app cannot read, and `nil` treated as "no body" would describe
    /// a response that carries one.
    var bodyKind: String
    var bodyBytes: Int? = nil
    /// Missing counts remain unknown, not zero. See `RulesDecodingTests`.
    var patchKeys: Int? = nil
    var patchStrategy: String? = nil
    /// How long the proxy will actually hold a matched response — the engine caps it at 60 s and
    /// reports the capped value, so this is what the wire waits and not what the rule asked for.
    var delayMs: Int? = nil
    /// Set when the rule asked for longer than the ceiling. Absent rather than false when the two
    /// agree, so the ordinary rule keeps the ordinary shape.
    var delayCapped: Bool? = nil
    var sequence: RewriteSequence? = nil
}

/// Fields used to display a configured rule. Runtime counters are intentionally not decoded.
struct RuleRow: Codable, Sendable, Equatable, Identifiable {
    var id: String
    // No stored `active` here on purpose. `rewrite.active` is the engine's own reading of it, sent
    // on every row of every scenario, and decoding the raw field beside it would invite a client to
    // read the one whose encoding it has to know — see `isActive` below.
    var match: RuleMatch? = nil
    var headers: [String: String]? = nil
    var body: JSONValue? = nil
    var patch: JSONValue? = nil
    var notes: String? = nil
    // Display the effective steps in rewrite.sequence, not a second copy of stored step fields.
    var rewrite: Rewrite
    /// Whether the engine will consider this rule at all — its own reading, never the stored field
    /// beside it. See `RulesDecodingTests`.
    var isActive: Bool { rewrite.active }
}

struct RulesSnapshot: Codable, Sendable, Equatable {
    var scenario: String
    /// The problems recorded against this scenario at load time. Shown beside the rules, because a
    /// rule that was dropped otherwise looks exactly like a scenario that is one rule shorter.
    var notWhole: [String]
    var rules: [RuleRow]
}
