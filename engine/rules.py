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

A sequence holds a cursor counting the advance events applied so far; the cursor itself is runtime
state and lives in the store, so everything here stays a pure function of (override, cursor).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, TypeGuard

MAX_WILDCARDS = 10  # a bounded number of wildcards keeps the generated regex cheap to evaluate
MAX_SEQUENCE_STEPS = 50  # bounded for the same reason: a pasted file must not cost unbounded memory
VALID_MODES = ("replace", "patch")
VALID_PATCH_STRATEGIES = ("appendToArray",)
VALID_EXHAUSTION_POLICIES = ("error", "repeatLast", "passThrough")

# A step is the response half of an override and nothing else. Listing what is allowed rather than
# what is forbidden means a step can never quietly carry a field that cannot take effect: `active`
# is read before step selection, `match`/`mode` belong to the rule, and `delayMs` is rejected with
# its own message because it has a right answer (put it on the parent).
STEP_FIELDS = ("status", "headers", "body")
SEQUENCE_FIELDS = ("steps", "advanceOn", "onExhausted")
MATCHER_FIELDS = ("method", "path", "query", "bodyContains")

# The three outcomes of resolving a rule against its cursor. A discriminated action rather than an
# "effective override" the caller reinterprets: exhaustion under `error` and `passThrough` has no
# override to hand back, and disguising that as one is how a caller ends up guessing.
APPLY = "apply"
PASS_THROUGH = "passThrough"
EXHAUSTED_ERROR = "error"


def glob_to_regex(glob: str) -> re.Pattern:
    """`*` is the only special character; everything else is literal.

    Wildcard count is capped at validation time (see MAX_WILDCARDS) rather than here, so that
    matching semantics stay exactly as they were for already-saved sessions.
    """
    escaped = re.sub(r"([.+?^${}()|\[\]\\])", r"\\\1", glob).replace("*", ".*")
    return re.compile(f"^{escaped}$")


def specificity(match: Mapping[str, Any]) -> tuple[int, int, int]:
    """Ranking key: fewer wildcards wins, then a longer path, then more constraints.

    The constraint count is the tiebreaker that stops a generic rule from masking a rule on the
    same path that additionally pins the method, a query parameter or the body.
    """
    path = match.get("path") or "*"
    wildcards = path.count("*")
    constraints = (
        int(bool(match.get("method")))
        + len(match.get("query") or {})
        + int(match.get("bodyContains") is not None)
    )
    return (wildcards, len(path), constraints)


def matches_matcher(
    matcher: Mapping[str, Any],
    method: str,
    pathname: str,
    query: Mapping[str, str],
    body_text: str,
) -> bool:
    """Does a request satisfy one matcher?

    The single matching primitive. `match` and `sequence.advanceOn` share the same vocabulary and
    must agree on what it means, so both go through here rather than one of them being reimplemented
    or wrapped in a synthetic override to reuse `matches`.
    """
    want_method = matcher.get("method")
    if want_method and want_method.upper() != method.upper():
        return False

    want_path = matcher.get("path")
    if want_path and not glob_to_regex(want_path).match(pathname):
        return False

    want_query = matcher.get("query")
    if want_query:
        for key, value in want_query.items():
            if query.get(key) != str(value):
                return False

    body_contains = matcher.get("bodyContains")
    if body_contains and body_contains not in body_text:
        return False

    return True


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
        if override.get("active", True) is not False
        and matches(override, method, pathname, query, body_text)
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
                f"{where}: unknown field {key!r} — a matcher may only carry "
                f"{', '.join(MATCHER_FIELDS)}"
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
                f"sequence: unknown field {key!r} — a sequence may only carry "
                f"{', '.join(SEQUENCE_FIELDS)}"
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
                    f"{where}: delayMs is not supported on a sequence step; set it on the override "
                    f"for a uniform delay"
                )
            if key not in STEP_FIELDS:
                raise ValidationError(
                    f"{where}: unknown field {key!r} — a step may only carry "
                    f"{', '.join(STEP_FIELDS)}"
                )
        _validate_response_fields(step, f"{where}: ")

    matcher = sequence.get("advanceOn")
    if matcher is not None:
        _validate_matcher(matcher, "sequence.advanceOn", require_constraint=True)

    policy = sequence.get("onExhausted")
    if policy is not None and policy not in VALID_EXHAUSTION_POLICIES:
        raise ValidationError(
            f"sequence.onExhausted must be one of {VALID_EXHAUSTION_POLICIES} if present"
        )


def validate_override(override: Any) -> dict:
    if not is_plain_object(override):
        raise ValidationError("override must be a JSON object")
    result = dict(override)

    mode = result.get("mode")
    if mode not in VALID_MODES:
        raise ValidationError(f"mode must be one of {VALID_MODES}, got {mode!r}")

    override_id = result.get("id")
    if override_id is not None and (not isinstance(override_id, str) or not override_id.strip()):
        raise ValidationError("id must be a non-empty string when present")

    _validate_matcher(result.get("match") or {}, "match")

    delay = result.get("delayMs")
    if delay is not None:
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise ValidationError("delayMs must be a number")
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


def normalise_session(session: Any, name: str) -> dict:
    """Coerce a session read from disk or an import payload into a shape the store can rely on.

    Invalid overrides are dropped rather than taking the whole session (or the proxy) down; the
    caller reports them.
    """
    if not is_plain_object(session):
        raise ValidationError("session must be a JSON object")

    # Underscore keys are ours: `_problems` below, and the sequence cursors the store hangs off the
    # session. `_persistable` strips them on the way out, so nothing we wrote can contain one — but
    # a hand-edited or imported file can, and `_sequenceRuntime` reaching the store means either a
    # crash inside a proxy hook or a scenario that quietly starts on step 2.
    result = {key: value for key, value in session.items() if not key.startswith("_")}
    result["name"] = name
    result.setdefault("schemaVersion", 1)
    result.setdefault("notes", "")
    result.setdefault("verified", False)

    overrides, problems, seen_ids = [], [], set()
    raw_overrides = result.get("overrides")
    if not isinstance(raw_overrides, list):
        # An absent key is a legitimately empty session. A present one that is not a list is a
        # payload whose rules cannot be kept — substituting [] without saying so would persist an
        # empty session and report success, which is the exact lie strict import exists to refuse.
        if "overrides" in result:
            problems.append("overrides must be a list")
        raw_overrides = []
    for index, entry in enumerate(raw_overrides):
        try:
            validated = validate_override(entry)
        except ValidationError as error:
            problems.append(f"override[{index}]: {error}")
            continue
        # An id is only needed to address the rule later (delete it, or show what matched). Writing
        # a session by hand is the documented workflow, so derive one rather than dropping the rule
        # for omitting something the author had no reason to invent. Derived from the content, so
        # it stays the same across restarts without rewriting the file.
        # Not `setdefault`: an explicit `"id": null` leaves the key present and the value None, so
        # the rule would load with no usable id — unaddressable by the CLI, and recorded as
        # `matched: null`, which reads as "nothing answered".
        if validated.get("id") is None:
            validated["id"] = derived_id(validated)
        # Ids address a rule: `add_override` replaces by id, `remove_override` deletes *every* rule
        # carrying one, and sequence state is keyed by it. Two rules sharing an id therefore share a
        # cursor and cannot be removed independently — so the duplicate is reported and dropped
        # rather than loaded into a session where it would misbehave quietly.
        if validated["id"] in seen_ids:
            problems.append(
                f"override[{index}]: duplicate id {validated['id']!r} — ids must be unique within a "
                f"session; this rule was skipped"
            )
            continue
        seen_ids.add(validated["id"])
        overrides.append(validated)

    result["overrides"] = overrides
    result["_problems"] = problems
    return result
