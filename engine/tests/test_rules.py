"""Matching, merging and override validation."""

import pytest

import rules

# MARK: - Glob matching

def test_glob_matches_exact_path():
    assert rules.glob_to_regex("/a/b").match("/a/b")


def test_glob_wildcard_matches_trailing_segment():
    assert rules.glob_to_regex("/api/v1/orders/*").match("/api/v1/orders/order_123")


def test_glob_does_not_match_a_different_prefix():
    assert rules.glob_to_regex("/a/*").match("/b/x") is None


def test_glob_treats_dot_as_literal():
    assert rules.glob_to_regex("/a.b").match("/aXb") is None


# MARK: - Specificity ordering

def test_most_specific_path_wins():
    overrides = [
        {"id": "wild", "match": {"path": "/api/*"}, "mode": "replace"},
        {"id": "exact", "match": {"path": "/api/v1/orders/*"}, "mode": "replace"},
    ]
    picked = rules.find_override(overrides, "GET", "/api/v1/orders/order_123", {}, "")
    assert picked["id"] == "exact"


def test_constrained_rule_beats_a_generic_rule_on_the_same_path():
    """A generic rule registered first must not mask a later rule that also pins a query
    parameter — the two have identical path specificity, so constraint count decides."""
    overrides = [
        {"id": "generic", "match": {"path": "/api/v1/checkout"}, "mode": "replace"},
        {"id": "specific", "match": {"path": "/api/v1/checkout", "query": {"mode": "test"}}, "mode": "replace"},
    ]
    picked = rules.find_override(overrides, "POST", "/api/v1/checkout", {"mode": "test"}, "")
    assert picked["id"] == "specific"


def test_generic_rule_still_wins_when_the_constraint_does_not_apply():
    overrides = [
        {"id": "generic", "match": {"path": "/api/v1/checkout"}, "mode": "replace"},
        {"id": "specific", "match": {"path": "/api/v1/checkout", "query": {"mode": "test"}}, "mode": "replace"},
    ]
    picked = rules.find_override(overrides, "POST", "/api/v1/checkout", {}, "")
    assert picked["id"] == "generic"


# MARK: - Match filters

MATCHER = {"match": {"method": "POST", "query": {"mode": "test"}, "bodyContains": "needle"}}


def test_method_mismatch_is_rejected():
    assert rules.matches(MATCHER, "GET", "/x", {"mode": "test"}, "needle") is False


def test_query_mismatch_is_rejected():
    assert rules.matches(MATCHER, "POST", "/x", {"mode": "live"}, "needle") is False


def test_body_mismatch_is_rejected():
    assert rules.matches(MATCHER, "POST", "/x", {"mode": "test"}, "hay") is False


def test_all_constraints_satisfied():
    assert rules.matches(MATCHER, "POST", "/x", {"mode": "test"}, "a needle b") is True


def test_inactive_override_is_skipped():
    overrides = [{"id": "x", "active": False, "match": {}, "mode": "replace"}]
    assert rules.find_override(overrides, "GET", "/x", {}, "") is None


# MARK: - deep_merge

def test_append_to_array_keeps_upstream_items():
    upstream = {"features": [{"id": "STANDARD_EXPORT"}], "accountId": "acct_123", "name": "keep me"}
    patch = {"features": [{"id": "BETA_EXPORT"}]}
    merged = rules.deep_merge(upstream, patch, "appendToArray")
    assert [f["id"] for f in merged["features"]] == ["STANDARD_EXPORT", "BETA_EXPORT"]


def test_append_to_array_applies_to_every_array_in_the_patch():
    """The strategy is global to the merge, not scoped to one named array — documented behaviour."""
    upstream = {"a": [1], "b": [2]}
    merged = rules.deep_merge(upstream, {"a": [9], "b": [8]}, "appendToArray")
    assert merged == {"a": [1, 9], "b": [2, 8]}


def test_default_strategy_replaces_arrays():
    assert rules.deep_merge({"a": [1, 2, 3]}, {"a": [9]})["a"] == [9]


def test_new_keys_are_added():
    assert rules.deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_nested_object_merge_without_arrays():
    merged = rules.deep_merge({"preferences": {"theme": "light", "notifications": True}},
                              {"preferences": {"theme": "dark"}})
    assert merged["preferences"] == {"theme": "dark", "notifications": True}


# MARK: - Validation

def test_rejects_unknown_mode():
    with pytest.raises(rules.ValidationError):
        rules.validate_override({"mode": "nonsense"})


def test_rejects_non_numeric_delay():
    """A string delay used to raise inside the proxy hook on every matching request."""
    with pytest.raises(rules.ValidationError):
        rules.validate_override({"mode": "replace", "delayMs": "1s"})


def test_rejects_negative_delay():
    with pytest.raises(rules.ValidationError):
        rules.validate_override({"mode": "replace", "delayMs": -1})


def test_rejects_non_string_path():
    with pytest.raises(rules.ValidationError):
        rules.validate_override({"mode": "replace", "match": {"path": 42}})


def test_rejects_excessive_wildcards():
    with pytest.raises(rules.ValidationError):
        rules.validate_override({"mode": "replace", "match": {"path": "/" + "*a" * 50}})


def test_rejects_out_of_range_status():
    with pytest.raises(rules.ValidationError):
        rules.validate_override({"mode": "replace", "status": 9999})


def test_coerces_float_delay_to_int():
    assert rules.validate_override({"mode": "replace", "delayMs": 1500.0})["delayMs"] == 1500


def test_normalise_session_drops_invalid_overrides_and_reports_them():
    session = rules.normalise_session(
        {"overrides": [{"mode": "replace", "id": "ok"}, {"mode": "bogus", "id": "bad"}]}, "s")
    assert [o["id"] for o in session["overrides"]] == ["ok"]
    assert len(session["_problems"]) == 1


def test_a_hand_written_override_does_not_need_an_id():
    """Writing a session by hand is the documented workflow; requiring an invented id meant a
    pasted example silently loaded zero rules."""
    session = rules.normalise_session(
        {"overrides": [{"mode": "replace", "status": 500, "match": {"path": "/a"}}]}, "s")
    assert len(session["overrides"]) == 1
    assert session["overrides"][0]["id"].startswith("ovr_")
    assert not session["_problems"]


def test_a_derived_id_is_stable_across_loads():
    rule = {"mode": "replace", "status": 500, "match": {"path": "/a"}}
    first = rules.normalise_session({"overrides": [dict(rule)]}, "s")["overrides"][0]["id"]
    second = rules.normalise_session({"overrides": [dict(rule)]}, "s")["overrides"][0]["id"]
    assert first == second


def test_different_rules_get_different_derived_ids():
    a = rules.normalise_session({"overrides": [{"mode": "replace", "match": {"path": "/a"}}]}, "s")
    b = rules.normalise_session({"overrides": [{"mode": "replace", "match": {"path": "/b"}}]}, "s")
    assert a["overrides"][0]["id"] != b["overrides"][0]["id"]


def test_normalise_session_tolerates_a_missing_overrides_key():
    assert rules.normalise_session({"name": "s"}, "s")["overrides"] == []


def test_normalise_session_rejects_a_non_object():
    with pytest.raises(rules.ValidationError):
        rules.normalise_session([], "s")


def test_deep_merge_replaces_a_non_dict_upstream():
    """A patch against a scalar or list upstream replaces it rather than raising."""
    assert rules.deep_merge(5, {"a": 1}) == {"a": 1}
    assert rules.deep_merge([1, 2], {"a": 1}) == {"a": 1}
    assert rules.deep_merge(None, {"a": 1}) == {"a": 1}


def test_deep_merge_appends_at_the_top_level():
    assert rules.deep_merge([1], [2], "appendToArray") == [1, 2]


def test_validate_override_rejects_an_unknown_patch_strategy():
    with pytest.raises(rules.ValidationError):
        rules.validate_override({"mode": "patch", "patchStrategy": "prependToArray"})
