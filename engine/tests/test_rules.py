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


# MARK: - Sequences
#
# A sequence answers differently as a scenario progresses. Everything here is pure: the cursor is
# an argument, because the store owns it.

def _sequenced(**sequence):
    """A minimal valid sequenced rule, with the sequence body overridden per test."""
    return {"mode": "replace", "match": {"path": "/a"},
            "sequence": {"steps": [{"status": 200}], **sequence}}


@pytest.mark.parametrize("override", [
    pytest.param({"mode": "replace", "sequence": "nope"}, id="sequence-not-an-object"),
    pytest.param({"mode": "replace", "sequence": {"steps": "nope"}}, id="steps-not-a-list"),
    pytest.param({"mode": "replace", "sequence": {"steps": []}}, id="empty-steps"),
    pytest.param({"mode": "replace", "sequence": {"steps": [[]]}}, id="step-not-an-object"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"delayMs": 10}]}}, id="step-delayMs"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"active": False}]}}, id="step-active"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"match": {}}]}}, id="step-match"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"mode": "patch"}]}}, id="step-mode"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"id": "x"}]}}, id="step-id"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"sequence": {}}]}}, id="step-nested"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"patch": {}}]}}, id="step-patch"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"status": 9999}]}}, id="step-bad-status"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"headers": {"a": 1}}]}}, id="step-bad-headers"),
    pytest.param({"mode": "patch", "sequence": {"steps": [{"status": 200}]}}, id="sequence-on-patch"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"status": 200}],
                                                  "onExhausted": "nonsense"}}, id="unknown-policy"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"status": 200}],
                                                  "advanceOn": {}}}, id="advanceOn-unconstrained"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"status": 200}],
                                                  "advanceOn": {"query": {"a": "b"}}}},
                 id="advanceOn-neither-method-nor-path"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"status": 200}],
                                                  "advanceOn": {"path": 42}}}, id="advanceOn-bad-path"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"status": 200}],
                                                  "advance_on": {"method": "DELETE", "path": "/x"}}},
                 id="sequence-unknown-field"),
    pytest.param({"mode": "replace", "sequence": {"steps": [{"status": 200}],
                                                  "advanceOn": {"method": "DELETE", "paths": "/x/*"}}},
                 id="advanceOn-unknown-field"),
    pytest.param({"mode": "replace", "match": {"path": "/a", "methods": "GET"},
                  "sequence": {"steps": [{"status": 200}]}}, id="match-unknown-field"),
])
def test_sequence_validation_rejects(override):
    """Each of these would otherwise be a rule that loads cleanly and quietly does the wrong thing:
    a step that can never answer, a field that cannot take effect, or an advance matcher that fires
    on every request that reaches the proxy."""
    with pytest.raises(rules.ValidationError):
        rules.validate_override(override)


def test_too_many_steps_is_rejected():
    with pytest.raises(rules.ValidationError):
        rules.validate_override(_sequenced(steps=[{"status": 200}] * (rules.MAX_SEQUENCE_STEPS + 1)))


def test_a_valid_sequence_is_accepted():
    assert rules.validate_override(_sequenced(advanceOn={"method": "DELETE", "path": "/x/*"},
                                              onExhausted="repeatLast"))


def test_the_step_delay_rejection_says_where_to_put_it():
    """A step carrying delayMs has a right answer, so the error gives it rather than only refusing."""
    with pytest.raises(rules.ValidationError, match="set it on the override"):
        rules.validate_override(_sequenced(steps=[{"delayMs": 10}]))


# MARK: - Step overlay

OVERLAY_PARENT = {
    "id": "o", "mode": "replace", "status": 500, "delayMs": 250,
    "headers": {"X-Parent": "1"}, "body": {"from": "parent"},
    "sequence": {"steps": [{}]},
}


def test_a_step_inherits_the_parent_response_fields():
    view = rules.step_view(OVERLAY_PARENT, {})
    assert view["status"] == 500
    assert view["body"] == {"from": "parent"}
    assert view["delayMs"] == 250, "delay is parent-level, so every step inherits it"


def test_step_headers_replace_the_parent_wholesale():
    """Documented as a shallow overlay. A merge would be defensible but this is what is specified,
    and the difference is invisible until a session relies on one of them."""
    view = rules.step_view(OVERLAY_PARENT, {"headers": {"X-Step": "2"}})
    assert view["headers"] == {"X-Step": "2"}


def test_empty_headers_clears_the_inherited_headers():
    assert rules.step_view(OVERLAY_PARENT, {"headers": {}})["headers"] == {}


def test_null_clears_an_inherited_field():
    """Validation treats None as absent, so an explicit null is how a step opts out of a default."""
    assert rules.step_view(OVERLAY_PARENT, {"body": None})["body"] is None


def test_step_view_drops_the_sequence_key():
    """The result is handed to wire logic that must not need to know sequences exist."""
    assert "sequence" not in rules.step_view(OVERLAY_PARENT, {})


# MARK: - Cursor arithmetic

def test_bumped_clamps_one_past_the_last_step():
    """Clamping at n rather than n+1 would make a second and a third advance event land on the same
    value, collapsing 'used up' into 'overrun' — the two states health reports separately."""
    assert [rules.bumped(c, 2) for c in (0, 1, 2, 3, 4)] == [1, 2, 3, 3, 3]


@pytest.mark.parametrize("cursor,expected", [(0, 201), (1, 202)])
def test_resolve_step_selects_by_cursor(cursor, expected):
    rule = _sequenced(steps=[{"status": 201}, {"status": 202}])
    action, view = rules.resolve_step(rule, cursor)
    assert action == rules.APPLY
    assert view["status"] == expected


@pytest.mark.parametrize("policy,cursor,expected_action", [
    ("error", 2, rules.EXHAUSTED_ERROR),
    ("error", 3, rules.EXHAUSTED_ERROR),
    ("passThrough", 2, rules.PASS_THROUGH),
    ("repeatLast", 2, rules.APPLY),
])
def test_resolve_step_past_the_end_follows_the_policy(policy, cursor, expected_action):
    rule = _sequenced(steps=[{"status": 201}, {"status": 202}], onExhausted=policy)
    action, _ = rules.resolve_step(rule, cursor)
    assert action == expected_action


def test_repeat_last_serves_the_final_step_again():
    rule = _sequenced(steps=[{"status": 201}, {"status": 202}], onExhausted="repeatLast")
    _, view = rules.resolve_step(rule, 5)
    assert view["status"] == 202


def test_the_default_policy_is_error():
    """A normal-looking response to an unplanned extra request is a false success, which is the
    failure this project's house rules single out."""
    assert rules.exhaustion_policy(_sequenced()) == "error"
    action, _ = rules.resolve_step(_sequenced(), 1)
    assert action == rules.EXHAUSTED_ERROR


def test_an_ordinary_override_resolves_to_itself():
    action, view = rules.resolve_step({"id": "o", "mode": "replace", "status": 204}, 0)
    assert action == rules.APPLY and view["status"] == 204


def test_advance_matcher_is_none_for_the_self_default():
    """`self` cannot be represented as a copy of `match`: the caller has to know whether this rule
    actually answered, and a matcher cannot express that."""
    assert rules.advance_matcher(_sequenced()) is None
    assert rules.advance_matcher(_sequenced(advanceOn={"method": "DELETE"})) == {"method": "DELETE"}


# MARK: - Unique ids

def test_duplicate_override_ids_are_reported_and_dropped():
    """Ids address a rule: add replaces by id, remove deletes every rule carrying one, and sequence
    state is keyed by it. Two rules sharing an id would share a cursor."""
    session = rules.normalise_session({"overrides": [
        {"id": "dup", "mode": "replace", "match": {"path": "/a"}},
        {"id": "dup", "mode": "replace", "match": {"path": "/b"}},
    ]}, "s")
    assert [o["match"]["path"] for o in session["overrides"]] == ["/a"]
    assert any("duplicate id" in problem for problem in session["_problems"])


def test_identical_rules_collide_on_their_derived_id_and_one_is_dropped():
    """Two byte-identical rules derive the same id, so the same rule applies."""
    rule = {"mode": "replace", "match": {"path": "/a"}}
    session = rules.normalise_session({"overrides": [dict(rule), dict(rule)]}, "s")
    assert len(session["overrides"]) == 1
    assert len(session["_problems"]) == 1


# MARK: - Untrusted input at the boundary

def test_a_null_id_is_replaced_by_a_derived_one():
    """`setdefault` leaves an explicit null in place, so the rule loaded with no usable id: not
    addressable by the CLI, and recorded as `matched: null`, which reads as "nothing answered"."""
    session = rules.normalise_session(
        {"overrides": [{"id": None, "mode": "replace", "match": {"path": "/a"}}]}, "s")
    assert session["overrides"][0]["id"].startswith("ovr_")


@pytest.mark.parametrize("injected", ["bad", {"seq": {"cursor": 1, "runId": "x"}}])
def test_internal_runtime_keys_are_stripped_from_input(injected):
    """Underscore keys are ours. `_persistable` strips them on the way out, so nothing we wrote can
    contain one — but a hand-edited or imported file can, and `_sequenceRuntime` reaching the store
    means either a crash inside a proxy hook or a scenario that quietly starts on step 2."""
    session = rules.normalise_session({"_sequenceRuntime": injected, "overrides": []}, "s")
    assert "_sequenceRuntime" not in session
