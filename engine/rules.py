"""Pure request-matching and response-rewriting logic.

No proxy or IO imports here on purpose: this module is unit-testable with the stdlib alone.

An *override* is a plain dict mirroring the on-disk JSON schema:

    {
      "id": "ovr_ab12cd",      # optional: derived from the rule if omitted
      "active": true,   # optional: true if omitted
      "match": {"method": "GET", "path": "/api/v1/orders/*", "query": {...}?, "bodyContains": "..."?},
      "mode": "replace" | "patch",
      "delayMs": 1000?,
      # replace:
      "status": 200, "headers": {...}?, "body": <json | str>,
      # patch:
      "patch": {...}, "patchStrategy": "appendToArray"?,
      # replace only — answer differently as a scenario progresses:
      "sequence": {
        "steps": [{"status": ..., "headers": ..., "body": ...}, ...],
        "advanceOn": {<matcher>}?,          # omitted means `self`: advance when this rule answers
        "onExhausted": "error" | "repeatLast" | "passThrough"?
      }
    }

Those fields plus `notes` — free text for the author, which the engine never reads — are the whole
vocabulary; `OVERRIDE_FIELD_HELP` below is the list, and anything outside it is rejected rather than
kept, because a field the engine ignores is a rule that answers with something its author did not
write.

A sequence holds a cursor counting the advance events applied so far; the cursor itself is runtime
state and lives in the store, so everything here stays a pure function of (override, cursor).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any, TypeGuard

SCHEMA_VERSION = 1  # the scenario-file format this engine reads; see `normalise_scenario`
MAX_WILDCARDS = 10  # a bounded number of wildcards keeps the generated regex cheap to evaluate
MAX_SEQUENCE_STEPS = 50  # bounded for the same reason: a pasted file must not cost unbounded memory
# The ceiling on a per-override `delayMs`, so a typo cannot wedge a flow indefinitely. Here
# rather than in `config` because it is not configuration: the proxy applies it and
# `describe_rewrite` reports it, and a rule described as waiting two minutes while the wire
# waits one is the drift this module exists to prevent — see
# test_describe_rewrite_reports_the_delay_the_proxy_will_apply.
MAX_DELAY_MS = 60_000
# The largest body `describe_rewrite` will hand back inside a sequence step. Each step is
# described after inheritance, so one 1 MB body on the parent of a 50-step sequence is 50 MB of
# snapshot — the same body, repeated, for a pane that only needs to say how big it is. Over this,
# a step reports its size and says the body was left out; see
# test_describe_rewrite_omits_a_step_body_too_large_to_repeat.
MAX_DESCRIBED_BODY_BYTES = 256 * 1024
VALID_MODES = ("replace", "patch")
VALID_PATCH_STRATEGIES = ("appendToArray",)
VALID_EXHAUSTION_POLICIES = ("error", "repeatLast", "passThrough")
# Statuses that must not carry a body or a Content-Length. Here rather than in `addon` because
# both the wire and the summary of what a rule answers with have to agree about them: a rule
# described as sending 8 bytes with a 204 describes a response no request receives — see
# test_describe_rewrite_reports_no_body_for_a_bodyless_status.
BODYLESS_STATUSES = (204, 304)

# A step is the response half of an override and nothing else. Listing what is allowed rather than
# what is forbidden means a step can never quietly carry a field that cannot take effect: `active`
# is read before step selection, `match`/`mode` belong to the rule, and `delayMs` is rejected with
# its own message because it has a right answer (put it on the parent).
STEP_FIELDS = ("status", "headers", "body")
SEQUENCE_FIELDS = ("steps", "advanceOn", "onExhausted")

# The matcher vocabulary, with what each field means. One dict rather than a tuple and a docstring
# somewhere else, because three things are generated from it — validation, the "unknown field" error,
# and the CLI help — and a capability nobody can discover is reported as a missing feature.
MATCHER_FIELD_HELP = {
    "method": "HTTP method, compared case-insensitively. Omit to match any method.",
    "path": "Path without the query string. '*' is the only wildcard; everything else is literal.",
    "query": "Query parameters that must all be present with these exact values. Others are ignored.",
    "bodyContains": "A substring that must appear in the request body.",
}
MATCHER_FIELDS = tuple(MATCHER_FIELD_HELP)

# The top-level vocabulary, for the same three consumers and the same reason. `notes` is the one
# field the engine never reads: JSON has no comments and scenarios are written by hand, so an author
# needs somewhere to say why a rule exists. Everything outside this table is a typo — including
# `nots` — and a typo'd field is not a harmless extra: `statsu` leaves the rule answering with a
# default status, which is a different response from the one its author wrote.
OVERRIDE_FIELD_HELP = {
    "id": "Stable name for the rule. Generated when omitted; derived from the rule's content for scenario files.",
    "active": "false switches the rule off without deleting it. Default true.",
    "match": "Which requests this rule answers (see the matcher fields).",
    "mode": "'replace' answers locally; 'patch' merges into the real response.",
    "delayMs": "Delay the matched response by this many milliseconds.",
    "status": "HTTP status of the answer (replace) or forced onto the real response (patch).",
    "headers": "Response headers (replace).",
    "body": "Response body, JSON or string (replace).",
    "patch": "JSON deep-merged into the real response (patch).",
    "patchStrategy": "'appendToArray' appends to arrays instead of replacing them (patch).",
    "sequence": "Answer differently as a scenario progresses (replace only).",
    "notes": "Free text for the author. Ignored by the engine.",
}
OVERRIDE_FIELDS = tuple(OVERRIDE_FIELD_HELP)

# The three outcomes of resolving a rule against its cursor. A discriminated action rather than an
# "effective override" the caller reinterprets: exhaustion under `error` and `passThrough` has no
# override to hand back, and disguising that as one is how a caller ends up guessing.
APPLY = "apply"
PASS_THROUGH = "passThrough"
EXHAUSTED_ERROR = "error"


@lru_cache(maxsize=512)
def glob_to_regex(glob: str) -> re.Pattern:
    """`*` is the only special character; everything else is literal.

    Wildcard count is capped at validation time (see MAX_WILDCARDS) rather than here, so that
    matching semantics stay exactly as they were for already-saved scenarios.

    Cached because this runs once per rule per request and dominated that cost: the `re.sub` and
    the f-string run before `re.compile` can reach its own internal cache. Keys are glob strings
    from validated rules, so the key space is bounded by the number of rules; the explicit maxsize
    keeps that true even for a profile someone edits in a loop.
    """
    escaped = re.sub(r"([.+?^${}()|\[\]\\])", r"\\\1", glob).replace("*", ".*")
    return re.compile(f"^{escaped}$")


def specificity(match: Mapping[str, Any]) -> tuple[int, int, int]:
    """Ranking key: fewer wildcards wins, then a longer path, then more constraints.

    The constraint count is the tiebreaker that stops a generic rule from masking a rule on the
    same path that additionally pins the method, a query parameter or the body.

    Every constraint is counted by the same truthiness `explain_matcher` matches by, so a field
    that constrains nothing on the wire cannot add rank. `bodyContains` used to be counted with
    `is not None`, which made `""` — a constraint the wire ignores — outrank an otherwise identical
    rule and answer in its place.

    That agreement is as far as the guarantee goes. Across *different* paths this is a heuristic,
    not a subset proof: `/a/*` and `/*/b` overlap without either containing the other, and the
    wildcard-then-length ordering simply picks one. It is a total order that is stable and
    predictable, which is what a rule author needs; it is not a claim that the winner matches a
    strictly smaller set of requests.
    """
    path = match.get("path") or "*"
    wildcards = path.count("*")
    constraints = int(bool(match.get("method"))) + len(match.get("query") or {}) + int(bool(match.get("bodyContains")))
    return (wildcards, len(path), constraints)


def explain_matcher(
    matcher: Mapping[str, Any],
    method: str,
    pathname: str,
    query: Mapping[str, str],
    body_text: str,
) -> str | None:
    """Why this matcher does not fit the request, or None if it does.

    The single matching primitive: `matches_matcher` is this function's verdict, so the reason shown
    to a person and the decision made on the wire cannot disagree. `match` and `sequence.advanceOn`
    share the same vocabulary and both come through here.

    Fields are tested in the order a person reads them, and the *first* failure is the answer — a
    list of every mismatch buries the one that matters, and the later checks are only meaningful once
    the earlier ones pass.

    Truthiness rather than `is not None` throughout, deliberately: validation checks these fields'
    types but not their emptiness, so `""` and `{}` are already treated as "no constraint" on the
    wire, and a saved scenario may contain them.
    """
    want_method = matcher.get("method")
    if want_method and want_method.upper() != method.upper():
        return f"method: rule wants {want_method.upper()}, request is {method.upper()}"

    want_path = matcher.get("path")
    if want_path and not glob_to_regex(want_path).match(pathname):
        return f"path: {want_path!r} does not match {pathname!r}"

    want_query = matcher.get("query")
    if want_query:
        for key, value in want_query.items():
            # str(value): a rule may carry `{"page": 2}`, and query values off the wire are strings.
            want = str(value)
            if query.get(key) != want:
                # "has no 'kind'" and "has 'beta'" send you to different places: the app is not
                # sending the parameter at all, versus sending a different value.
                have = f"has {query[key]!r}" if key in query else f"has no {key!r}"
                return f"query.{key}: rule wants {want!r}, request {have}"

    body_contains = matcher.get("bodyContains")
    if body_contains and body_contains not in body_text:
        return f"bodyContains: {body_contains!r} is not in the request body"

    return None


def matches_matcher(
    matcher: Mapping[str, Any],
    method: str,
    pathname: str,
    query: Mapping[str, str],
    body_text: str,
) -> bool:
    """Does a request satisfy one matcher?"""
    return explain_matcher(matcher, method, pathname, query, body_text) is None


def is_active(override: Mapping[str, Any]) -> bool:
    """Is this rule one the engine will consider at all?

    One spelling, because the encoding is not obvious — a missing key means active, and only a
    literal `False` switches a rule off. It is read by matching, by the sequence and answer state
    the store reports, and by `explain-match`, which exists to tell an operator why a rule did not
    answer; those disagreeing would misfile a rule as inactive in the very command you run to find
    out why it is not firing.
    """
    return override.get("active", True) is not False


def effective_delay_ms(override: Mapping[str, Any]) -> int | None:
    """How long the proxy will actually hold a matched response, or None for no delay.

    Capped here rather than at the sleep, so the number a client is shown is the number the flow
    waits: a rule configured with 120000 used to be described as two minutes and served after one.
    Validation has already made `delayMs` a non-negative int, and 0 is "no delay" the same as an
    absent field — that is what the proxy does with it, and a description saying "0 ms" would offer
    a distinction the wire does not make.
    """
    delay = override.get("delayMs")
    if not delay:
        return None
    return min(int(delay), MAX_DELAY_MS)


def effective_status(spec: Mapping[str, Any]) -> int:
    """The status a `replace` answers with: its own, or 200 when it names none.

    One spelling, shared by the wire (`addon._answer`) and by `describe_rewrite`, so a client shown
    "200" is shown the status it will actually get. Two copies of `or 200` would be two places to
    change the default in, and the summary exists precisely so no one has to know it.
    """
    return int(spec.get("status") or 200)


def matches(
    override: Mapping[str, Any],
    method: str,
    pathname: str,
    query: Mapping[str, str],
    body_text: str,
) -> bool:
    return matches_matcher(override.get("match") or {}, method, pathname, query, body_text)


def find_override(
    overrides: Sequence[dict],
    method: str,
    pathname: str,
    query: Mapping[str, str],
    body_text: str,
) -> dict | None:
    """Return the most-specific active override matching the request, or None."""
    candidates = [
        override
        for override in overrides
        if is_active(override) and matches(override, method, pathname, query, body_text)
    ]
    if not candidates:
        return None

    def rank(override: dict) -> tuple[int, int, int]:
        wildcards, length, constraints = specificity(override.get("match") or {})
        return (wildcards, -length, -constraints)

    candidates.sort(key=rank)
    return candidates[0]


def is_plain_object(value: Any) -> TypeGuard[Mapping[str, Any]]:
    """A TypeGuard rather than a plain bool so that the `if not is_plain_object(x): return` shape
    used throughout this module narrows `x` for the type checker instead of needing a cast."""
    return isinstance(value, Mapping)


def _matcher_shape(matcher: Any) -> tuple:
    """One comparable shape for a matcher: what it constrains, in the terms the wire compares by.

    Truthiness, `str(value)` and the upper-cased method are `explain_matcher`'s own rules, not new
    ones: two matchers that this says are the same are two matchers that accept exactly the same
    requests, which is the only sense in which one rule's trigger *is* another rule.
    """
    fields = matcher if is_plain_object(matcher) else {}
    method = fields.get("method")
    query = fields.get("query") or {}
    return (
        method.upper() if isinstance(method, str) and method else None,
        fields.get("path") or None,
        tuple(sorted((key, str(value)) for key, value in query.items())) if is_plain_object(query) else (),
        fields.get("bodyContains") or None,
    )


def same_matcher(a: Any, b: Any) -> bool:
    """Do two matchers constrain requests identically?

    One definition, because the only caller is a claim made to a client — that a sequence's trigger
    is some other rule — and a client comparing matchers itself would be comparing them by rules of
    its own: `POST` against `post`, `2` against `"2"`, an absent field against an explicit null.
    """
    return _matcher_shape(a) == _matcher_shape(b)


# MARK: - Sequences
#
# Pure: the cursor is supplied by the caller. The store owns it, because it is runtime state that
# must never reach a profile kept in git.


def sequence_steps(override: Mapping[str, Any]) -> list[dict] | None:
    """The rule's steps, or None if it is an ordinary single-response override."""
    sequence = override.get("sequence")
    if not is_plain_object(sequence):
        return None
    steps = sequence.get("steps")
    return list(steps) if isinstance(steps, list) and steps else None


def advance_matcher(override: Mapping[str, Any]) -> dict | None:
    """The matcher that moves this rule's cursor, or None meaning the implicit `self`.

    `self` is deliberately not "a copy of `match`". `find_override` returns only the most specific
    match, so a rule whose matcher fits a request may still not be the rule that answered it — and
    advancing on a match this rule lost would spend a step it never served, leaving every later
    request off by one. `self` therefore means *answered*, which only the caller can know.
    """
    sequence = override.get("sequence")
    if not is_plain_object(sequence):
        return None
    matcher = sequence.get("advanceOn")
    return dict(matcher) if is_plain_object(matcher) else None


def exhaustion_policy(override: Mapping[str, Any]) -> str:
    sequence = override.get("sequence")
    policy = sequence.get("onExhausted") if is_plain_object(sequence) else None
    return policy if policy in VALID_EXHAUSTION_POLICIES else "error"


def step_view(override: Mapping[str, Any], step: Mapping[str, Any]) -> dict:
    """Parent fields with one step's response fields overlaid, shallowly.

    Shallow on purpose, and documented as such: a step's `headers` replaces the parent's wholesale
    rather than merging, and an explicit null clears an inherited field because validation already
    treats None as absent. `sequence` is dropped so the result is an ordinary override the wire
    logic can consume without knowing sequences exist.
    """
    view = {key: value for key, value in override.items() if key != "sequence"}
    view.update(step)
    return view


def resolve_step(override: Mapping[str, Any], cursor: int) -> tuple[str, dict | None]:
    """What this rule should do for a request, given how many advance events it has seen.

    Returns (APPLY, effective override) | (PASS_THROUGH, None) | (EXHAUSTED_ERROR, None).
    """
    steps = sequence_steps(override)
    if steps is None:
        return APPLY, dict(override)

    if 0 <= cursor < len(steps):
        return APPLY, step_view(override, steps[cursor])

    policy = exhaustion_policy(override)
    if policy == "repeatLast":
        return APPLY, step_view(override, steps[-1])
    if policy == "passThrough":
        return PASS_THROUGH, None
    return EXHAUSTED_ERROR, None


def bumped(cursor: int, step_count: int) -> int:
    """Advance the cursor, clamped one past the last step.

    Clamping at ``step_count + 1`` rather than ``step_count`` is what keeps "used up" distinct from
    "overrun": at the lower clamp a second and a third advance event both land on the same value, so
    `exhausted` and `hasOverrun` collapse into one flag. Distinguishing a third event from a fourth
    is given up deliberately — that is the bound, and neither flag depends on it.
    """
    return min(cursor + 1, step_count + 1)


def deep_merge(target: Any, patch: Any, strategy: str | None = None) -> Any:
    """Deep-merge `patch` into `target`.

    - dicts merge recursively
    - lists: with ``strategy == "appendToArray"`` the upstream items are followed by the patch
      items **at every path where both sides are lists** — the strategy is not scoped to one
      named array, and it does not deduplicate. Without it, the patch list replaces the target.
    - scalars are replaced by the patch value
    """
    if isinstance(target, list) and isinstance(patch, list):
        return target + patch if strategy == "appendToArray" else patch

    if is_plain_object(target) and is_plain_object(patch):
        result = dict(target)
        for key, value in patch.items():
            result[key] = deep_merge(target[key], value, strategy) if key in target else value
        return result

    return patch


# MARK: - The answer on the wire
#
# What a `replace` actually sends. It lives here, in the module with no IO, because two consumers
# have to agree about it: the proxy that serves the answer and the summary that tells a client what
# a rule answers with. A second copy of these rules is a description that drifts from the wire it
# describes, and the drift is invisible — the screen is exactly where nobody can check it.


def headers_with_default_content_type(headers: Mapping[str, str] | None, json_body: bool) -> dict:
    """Merge case-insensitively: a scenario that spells the header `Content-Type` must not end
    up emitting both that and a lowercase `content-type` on the wire."""
    result = dict(headers or {})
    if json_body and not any(key.lower() == "content-type" for key in result):
        result["Content-Type"] = "application/json"
    return result


def wire_response(spec: Mapping[str, Any]) -> dict:
    """The answer one `replace` response spec produces: `{"status", "headers", "body"}`.

    The three shaping decisions are made here and nowhere else: the 200 a rule that names no status
    answers with, the body a bodyless status drops however much the rule carries, and the default
    `Content-Type` — see test_the_wire_answer_for_a_json_rule_is_unchanged.

    `body` is the value as stored, not the bytes it becomes. Encoding is the caller's (`json.dumps`
    then utf-8 for anything that is not a string), because a client asking what a rule answers with
    wants the JSON it will receive, not a length of bytes it cannot read.
    """
    status = effective_status(spec)
    body = None if status in BODYLESS_STATUSES else spec.get("body")
    # `json_body` is "there is a body at all", a string body included. That is what the wire does
    # today, and this function reports the wire rather than deciding it — a string body losing the
    # header it has been sent with is a change to served responses, not to a description of them.
    headers = headers_with_default_content_type(spec.get("headers"), body is not None)
    # A stored `Content-Length` never reaches anyone: `http.Response.make` overwrites it with the
    # true length of what it encodes, and a bodyless status drops it entirely. Reporting the stored
    # one would show a client a header no response carries — see
    # test_the_wire_answer_matches_what_is_described.
    return {
        "status": status,
        "headers": {key: value for key, value in headers.items() if key.lower() != "content-length"},
        "body": body,
    }


# MARK: - Describing a rule
#
# One summary of what a rule answers with, for a client that shows rules but must not decide
# anything about them. Every field comes from the helpers above rather than from a second reading
# of the schema, so a change to step inheritance or to the exhaustion default cannot leave a
# client describing behaviour this engine no longer has.


def _body_summary(wire: Mapping[str, Any]) -> tuple[str, int | None]:
    """The kind and the byte size of an already-shaped answer's body.

    It takes the output of `wire_response` and classifies it; it does not decide anything. Whether a
    status carries a body is `wire_response`'s question, asked once — asking it here as well is a
    second implementation of the same rule, and the two would answer differently the day one of them
    learns a new status.

    Sized as `addon._answer` encodes the payload — `json.dumps` for anything that is not a string,
    then utf-8. That is the size before any `Content-Encoding` the rule's own headers ask for:
    mitmproxy compresses such a body on the way out, and the wire then carries more bytes than the
    payload — see test_a_content_encoding_is_applied_after_the_described_size.
    """
    body = wire.get("body")
    if body is None:
        return "none", None
    if isinstance(body, str):
        return "text", len(body.encode("utf-8"))
    return "json", len(json.dumps(body).encode("utf-8"))


def _step_summary(override: Mapping[str, Any], step: Mapping[str, Any]) -> dict:
    """One sequence step, as the wire would answer it, and which of its fields it did not write.

    Through `step_view` and then `wire_response`, not the raw step: a step that omits `body` answers
    with the parent's, and a step that sets 304 answers with none however much it inherited — see
    test_describe_rewrite_shows_what_a_step_inherits and
    test_describe_rewrite_reports_no_body_for_a_step_that_turns_bodyless.
    """
    wire = wire_response(step_view(override, step))
    kind, size = _body_summary(wire)
    # Reported, then left out: the size is what a pane shows, and repeating a megabyte of body once
    # per step is a snapshot that grows with the square of what a scenario holds. The key is absent
    # rather than false when the body is included, so the ordinary step stays the ordinary shape.
    omitted = size is not None and size > MAX_DESCRIBED_BODY_BYTES
    return {
        "status": wire["status"],
        "headers": wire["headers"],
        "body": None if omitted else wire["body"],
        **({"bodyOmitted": True} if omitted else {}),
        "bodyKind": kind,
        "bodyBytes": size,
        # Which of these values came from the parent. A pane showing an inherited body as the step's
        # own invites an edit to the step that changes nothing, because the value is not written
        # there; sorted so the list does not depend on the order the fields happen to be checked in.
        "inherited": sorted(field for field in STEP_FIELDS if field not in step),
    }


def describe_rewrite(override: Mapping[str, Any]) -> dict:
    """What a validated override answers with, in one flat summary.

    It describes; it does not validate, and it does not decide. Rule semantics — which rule wins,
    which step is selected, what a patch merges into — stay in this module and in the store, and a
    client that renders this dict is the only kind of client that cannot drift away from them.

    `active` is here as well as in `store.answer_states`, because that one describes a run and a
    scenario merely being browsed has none: the field a client reads must not depend on which
    scenario it is looking at.

    The top-level response fields (`status`, `bodyKind`, `bodyBytes`) belong to a rule that answers
    with one response. A sequenced rule does not: its answers are its steps, each described here
    after inheritance, so those fields are empty and `sequence` is what a reader must use instead.
    """
    steps = sequence_steps(override)
    mode = override.get("mode")
    patch = override.get("patch")
    patching = mode == "patch"
    # Only a plain `replace` rule answers with a body of its own: a sequenced rule answers with its
    # steps, and a patch answers with the upstream body merged — the wire ignores a `body` a patch
    # rule carries, and describing one would name a payload no request ever receives.
    kind, size = _body_summary(wire_response(override)) if steps is None and not patching else ("none", None)
    if steps is not None:
        status = None  # each step carries its own, reported below
    elif patching:
        # A patch that forces no status preserves the upstream's, which this engine has not seen —
        # `null` is the whole answer, and reporting 200 would be a claim about someone else's
        # response.
        status = override.get("status")
    else:
        # Not `override.get("status")`: a replace that names none answers 200, and reporting `null`
        # would leave every client re-deriving the default this summary exists to carry.
        status = effective_status(override)
    summary = {
        # First, and on every row: a browsed scenario's rules have no runtime state, so `answer` is
        # null there and a client reading activeness off it would fall back to reading the stored
        # `active` field itself — a second implementation of `is_active`, whose encoding is not
        # obvious (a missing key means active, and only a literal `false` switches a rule off).
        # See test_describe_rewrite_reports_the_engines_own_reading_of_activeness.
        "active": is_active(override),
        "mode": mode,
        "status": status,
        "bodyKind": kind,
        "bodyBytes": size,
        # Present as None for a `replace` rule rather than absent, so one shape decodes both modes.
        "patchKeys": (len(patch) if is_plain_object(patch) else 0) if patching else None,
        "patchStrategy": override.get("patchStrategy") if patching else None,
        # The delay as it will be applied, not as it was written; the flag is what tells a reader
        # the two differ. Absent rather than false when they agree, like `bodyOmitted`.
        "delayMs": effective_delay_ms(override),
        **({"delayCapped": True} if (override.get("delayMs") or 0) > MAX_DELAY_MS else {}),
        "sequence": None,
    }
    if steps is not None:
        summary["sequence"] = {
            # The matcher itself, as stored, or None for the implicit `self`. `store.sequence_states`
            # reports the *word* ("self"/"match") because a cursor is all it describes; a client
            # showing what moves a sequence on needs the request that does it, and deriving one from
            # the other is the reimplementation this endpoint exists to spare it.
            "advanceOn": advance_matcher(override),
            # Via `exhaustion_policy`, so an omitted `onExhausted` reads as the "error" the engine
            # will actually apply — a client showing "none" would promise a rule that keeps serving.
            "onExhausted": exhaustion_policy(override),
            "steps": [_step_summary(override, step) for step in steps],
        }
    return summary


# MARK: - Validation
#
# Overrides are validated before they are persisted, so a malformed rule fails at the API call
# that created it rather than raising inside the proxy hook on every matching request.


class ValidationError(ValueError):
    pass


def _validate_matcher(matcher: Any, where: str, *, require_constraint: bool = False) -> None:
    """Shared by `match` and `sequence.advanceOn` so the two cannot drift apart."""
    if not is_plain_object(matcher):
        raise ValidationError(f"{where} must be a JSON object")

    # A typo'd field is not a stricter matcher, it is a missing constraint: `"paths"` is ignored
    # and the matcher silently widens to everything its remaining fields allow.
    for key in matcher:
        if key not in MATCHER_FIELDS:
            raise ValidationError(
                f"{where}: unknown field {key!r} — a matcher may only carry {', '.join(MATCHER_FIELDS)}"
            )

    path = matcher.get("path")
    if path is not None:
        if not isinstance(path, str):
            raise ValidationError(f"{where}.path must be a string")
        if path.count("*") > MAX_WILDCARDS:
            raise ValidationError(f"{where}.path has more than {MAX_WILDCARDS} wildcards")

    method = matcher.get("method")
    if method is not None and not isinstance(method, str):
        raise ValidationError(f"{where}.method must be a string")

    query = matcher.get("query")
    if query is not None and not is_plain_object(query):
        raise ValidationError(f"{where}.query must be a JSON object")

    body_contains = matcher.get("bodyContains")
    if body_contains is not None and not isinstance(body_contains, str):
        raise ValidationError(f"{where}.bodyContains must be a string")

    # An override with no match matching everything is documented and occasionally useful. An
    # *advance* matcher with no constraints is neither: it would step the sequence on every request
    # that reaches the proxy, silently, which is the opposite of what advanceOn is for.
    if require_constraint and not (matcher.get("method") or matcher.get("path")):
        raise ValidationError(
            f"{where} must constrain at least 'method' or 'path' — a matcher with neither matches "
            f"every request, so every request would advance the sequence"
        )


def _validate_response_fields(spec: Mapping[str, Any], where: str) -> None:
    """Shared by an override and by each of its sequence steps, for the same reason."""
    status = spec.get("status")
    if status is not None:
        if isinstance(status, bool) or not isinstance(status, int):
            raise ValidationError(f"{where}status must be an integer")
        if not 100 <= status <= 599:
            raise ValidationError(f"{where}status must be a valid HTTP status code")

    headers = spec.get("headers")
    if headers is not None:
        if not is_plain_object(headers):
            raise ValidationError(f"{where}headers must be a JSON object")
        for key, value in headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValidationError(f"{where}header names and values must be strings")


def _validate_sequence(sequence: Any, mode: str) -> None:
    if not is_plain_object(sequence):
        raise ValidationError("sequence must be a JSON object")

    # Steps already refuse unknown fields; the sequence object must too, or a typo'd `advanceOn`
    # loads cleanly and quietly means `self` — every read spends a step the scenario reserved for
    # the mutating request.
    for key in sequence:
        if key not in SEQUENCE_FIELDS:
            raise ValidationError(
                f"sequence: unknown field {key!r} — a sequence may only carry {', '.join(SEQUENCE_FIELDS)}"
            )

    # `patch` defers to the response hook, so an exhausted patch under the `error` policy has no way
    # to answer locally; and a patch skipped by a streamed or non-JSON upstream would spend a step
    # the app never saw, leaving the scenario silently unrepeatable. One restriction is cheaper than
    # reserve/commit/rollback.
    if mode != "replace":
        raise ValidationError(
            f"sequence is only supported with mode 'replace', not {mode!r} — a patch cannot answer "
            f"locally when the sequence is exhausted, and a patch skipped by a non-JSON upstream "
            f"would spend a step the app never saw"
        )

    steps = sequence.get("steps")
    if not isinstance(steps, list):
        raise ValidationError("sequence.steps must be a list")
    if not steps:
        raise ValidationError("sequence.steps must not be empty — such a rule could never answer")
    if len(steps) > MAX_SEQUENCE_STEPS:
        raise ValidationError(f"sequence.steps has more than {MAX_SEQUENCE_STEPS} steps")

    for index, step in enumerate(steps):
        where = f"sequence.steps[{index}]"
        if not is_plain_object(step):
            raise ValidationError(f"{where} must be a JSON object")
        for key in step:
            if key == "delayMs":
                raise ValidationError(
                    f"{where}: delayMs is not supported on a sequence step; set it on the override for a uniform delay"
                )
            if key not in STEP_FIELDS:
                raise ValidationError(
                    f"{where}: unknown field {key!r} — a step may only carry {', '.join(STEP_FIELDS)}"
                )
        _validate_response_fields(step, f"{where}: ")

    matcher = sequence.get("advanceOn")
    if matcher is not None:
        _validate_matcher(matcher, "sequence.advanceOn", require_constraint=True)

    policy = sequence.get("onExhausted")
    if policy is not None and policy not in VALID_EXHAUSTION_POLICIES:
        raise ValidationError(f"sequence.onExhausted must be one of {VALID_EXHAUSTION_POLICIES} if present")


def validate_override(override: Any) -> dict:
    if not is_plain_object(override):
        raise ValidationError("override must be a JSON object")
    result = dict(override)

    # First, before any field-specific check: a typo reports itself rather than the secondary error
    # it causes — `{"mod": "replace"}` must say `'mod'`, not "mode must be one of", which points at
    # a field the author never wrote.
    for key in result:
        if key not in OVERRIDE_FIELDS:
            raise ValidationError(f"unknown field {key!r} — an override may only carry {', '.join(OVERRIDE_FIELDS)}")

    # `notes` is text or nothing. Other optional fields read `None` as absence; here an explicit
    # `null` is a mistake worth naming, since the only reason to write the key is to put words in it.
    if "notes" in result and not isinstance(result["notes"], str):
        raise ValidationError("notes must be a string")

    mode = result.get("mode")
    if mode not in VALID_MODES:
        raise ValidationError(f"mode must be one of {VALID_MODES}, got {mode!r}")

    override_id = result.get("id")
    if override_id is not None and (not isinstance(override_id, str) or not override_id.strip()):
        raise ValidationError("id must be a non-empty string when present")

    # Not `or {}`, and not `.get`: the first let a falsy non-object — `[]`, `""`, `0`, `false` —
    # skip the object check below and then be read as an *absent* matcher on the wire, which is a
    # rule that answers every intercepted request; the second cannot tell an explicit `null` from
    # an omission. Omission is the one spelling of "no matcher"; a `match` that is present must be
    # an object.
    _validate_matcher(result["match"] if "match" in result else {}, "match")

    delay = result.get("delayMs")
    if delay is not None:
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise ValidationError("delayMs must be a number")
        # Before the comparison and the conversion, both of which a non-finite float gets past or
        # through: `json.loads` turns `1e309`, `Infinity` and `NaN` into floats, `NaN < 0` is False,
        # and `int()` then raises OverflowError (not even a ValueError) from inside validation.
        # That is not a rule this file refuses, it is validation itself falling over — the caller
        # gets a traceback instead of the field name, and the loader's `except ValidationError`
        # never sees it, so one such rule took a whole scenario's diagnostics with it.
        if isinstance(delay, float) and not math.isfinite(delay):
            raise ValidationError("delayMs must be a finite number")
        if delay < 0:
            raise ValidationError("delayMs must not be negative")
        result["delayMs"] = int(delay)

    _validate_response_fields(result, "")

    if mode == "patch":
        if not is_plain_object(result.get("patch") or {}):
            raise ValidationError("patch must be a JSON object")
        strategy = result.get("patchStrategy")
        if strategy is not None and strategy not in VALID_PATCH_STRATEGIES:
            raise ValidationError(f"patchStrategy must be one of {VALID_PATCH_STRATEGIES} if present")

    sequence = result.get("sequence")
    if sequence is not None:
        _validate_sequence(sequence, mode)

    return result


def derived_id(override: Mapping[str, Any]) -> str:
    """A stable id for an override that did not name itself."""
    shape = json.dumps({k: v for k, v in override.items() if k != "id"}, sort_keys=True, default=str)
    return f"ovr_{hashlib.sha256(shape.encode('utf-8')).hexdigest()[:6]}"


def normalise_scenario(scenario: Any, name: str) -> dict:
    """Coerce a scenario read from disk into a shape the store can rely on.

    Two failure grades, deliberately different: an invalid *override* is dropped into `_problems`
    rather than taking the whole scenario (or the proxy) down, and the caller reports it; a scenario
    that is not an object, or is stamped with a `schemaVersion` this engine does not read, raises —
    there is no part of it that can be trusted, so keeping any of it would be a guess.
    """
    if not is_plain_object(scenario):
        raise ValidationError("scenario must be a JSON object")

    # Underscore keys are ours: `_problems` below, and the sequence cursors the store hangs off the
    # scenario. `_persistable` strips them on the way out, so nothing we wrote can contain one — but
    # a hand-edited or imported file can, and `_ruleRuntime` reaching the store means either a
    # crash inside a proxy hook or a scenario that quietly starts on step 2.
    result = {key: value for key, value in scenario.items() if not key.startswith("_")}
    result["name"] = name
    result.setdefault("schemaVersion", 1)
    # Refused whole, not per-rule. A file written for a format this engine does not know is a file
    # it would read with the wrong rules — silently, and only in the ways the format changed. The
    # refusal turns that into a scenario the loader *names* as skipped, which is the difference
    # between "your scenario is not loaded" and "your scenario loaded and behaves oddly".
    # `isinstance(True, int)` is True, so bools are excluded explicitly: `"schemaVersion": true`
    # is a typo, not version 1.
    version = result["schemaVersion"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ValidationError(
            f"schemaVersion: unsupported version {version!r} — this engine reads version {SCHEMA_VERSION}"
        )
    result.setdefault("notes", "")
    result.setdefault("verified", False)

    overrides, problems, seen_ids = [], [], set()
    raw_overrides = result.get("overrides")
    if not isinstance(raw_overrides, list):
        # An absent key is a legitimately empty scenario. A present one that is not a list is a
        # file whose rules cannot be kept — substituting [] without saying so would load an empty
        # scenario and report nothing, so a scenario with every rule lost reads as one with none.
        if "overrides" in result:
            problems.append("overrides must be a list")
        raw_overrides = []
    for index, entry in enumerate(raw_overrides):
        try:
            validated = validate_override(entry)
        except ValidationError as error:
            problems.append(f"override[{index}]: {error}")
            continue
        # An id is only needed to address the rule later (replace or reset it, or show what matched). Writing
        # a scenario by hand is the documented workflow, so derive one rather than dropping the rule
        # for omitting something the author had no reason to invent. Derived from the content, so
        # it stays the same across restarts without rewriting the file.
        # Not `setdefault`: an explicit `"id": null` leaves the key present and the value None, so
        # the rule would load with no usable id — unaddressable by the CLI, and recorded as
        # `matched: null`, which reads as "nothing answered".
        if validated.get("id") is None:
            validated["id"] = derived_id(validated)
        # Ids address a rule: `add_override` replaces by id, and sequence state is keyed by it. Two
        # rules sharing an id would share a cursor and could not be replaced independently — so the
        # duplicate is reported and dropped rather than loaded into a scenario where it would
        # misbehave quietly.
        if validated["id"] in seen_ids:
            problems.append(
                f"override[{index}]: duplicate id {validated['id']!r} — ids must be unique within a "
                f"scenario; this rule was skipped"
            )
            continue
        seen_ids.add(validated["id"])
        overrides.append(validated)

    result["overrides"] = overrides
    result["_problems"] = problems
    return result
