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
      "patch": {...}, "patchStrategy": "appendToArray"?
    }
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

MAX_WILDCARDS = 10  # a bounded number of wildcards keeps the generated regex cheap to evaluate
VALID_MODES = ("replace", "patch")
VALID_PATCH_STRATEGIES = ("appendToArray",)


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


def matches(
    override: Mapping[str, Any],
    method: str,
    pathname: str,
    query: Mapping[str, str],
    body_text: str,
) -> bool:
    match = override.get("match") or {}

    want_method = match.get("method")
    if want_method and want_method.upper() != method.upper():
        return False

    want_path = match.get("path")
    if want_path and not glob_to_regex(want_path).match(pathname):
        return False

    want_query = match.get("query")
    if want_query:
        for key, value in want_query.items():
            if query.get(key) != str(value):
                return False

    body_contains = match.get("bodyContains")
    if body_contains and body_contains not in body_text:
        return False

    return True


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


def is_plain_object(value: Any) -> bool:
    return isinstance(value, Mapping)


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

    match = result.get("match") or {}
    if not is_plain_object(match):
        raise ValidationError("match must be a JSON object")

    path = match.get("path")
    if path is not None:
        if not isinstance(path, str):
            raise ValidationError("match.path must be a string")
        if path.count("*") > MAX_WILDCARDS:
            raise ValidationError(f"match.path has more than {MAX_WILDCARDS} wildcards")

    method = match.get("method")
    if method is not None and not isinstance(method, str):
        raise ValidationError("match.method must be a string")

    query = match.get("query")
    if query is not None and not is_plain_object(query):
        raise ValidationError("match.query must be a JSON object")

    body_contains = match.get("bodyContains")
    if body_contains is not None and not isinstance(body_contains, str):
        raise ValidationError("match.bodyContains must be a string")

    delay = result.get("delayMs")
    if delay is not None:
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise ValidationError("delayMs must be a number")
        if delay < 0:
            raise ValidationError("delayMs must not be negative")
        result["delayMs"] = int(delay)

    status = result.get("status")
    if status is not None:
        if isinstance(status, bool) or not isinstance(status, int):
            raise ValidationError("status must be an integer")
        if not 100 <= status <= 599:
            raise ValidationError("status must be a valid HTTP status code")

    headers = result.get("headers")
    if headers is not None:
        if not is_plain_object(headers):
            raise ValidationError("headers must be a JSON object")
        for key, value in headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValidationError("header names and values must be strings")

    if mode == "patch":
        if not is_plain_object(result.get("patch") or {}):
            raise ValidationError("patch must be a JSON object")
        strategy = result.get("patchStrategy")
        if strategy is not None and strategy not in VALID_PATCH_STRATEGIES:
            raise ValidationError(f"patchStrategy must be one of {VALID_PATCH_STRATEGIES} if present")

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

    result = dict(session)
    result["name"] = name
    result.setdefault("schemaVersion", 1)
    result.setdefault("notes", "")
    result.setdefault("verified", False)

    raw_overrides = result.get("overrides")
    if not isinstance(raw_overrides, list):
        raw_overrides = []

    overrides, problems = [], []
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
        validated.setdefault("id", derived_id(validated))
        overrides.append(validated)

    result["overrides"] = overrides
    result["_problems"] = problems
    return result
