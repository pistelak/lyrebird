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


@pytest.mark.parametrize("field", ["statsu", "bdoy", "delay_ms", "matches", "Mode"])
def test_validate_override_rejects_an_unknown_top_level_field(field):
    """A kept-but-ignored field is not a harmless extra: `statsu: 503` used to validate, persist and
    answer 200, so the rule replied with something its author never wrote."""
    override = {"match": {"path": "/api/items"}, "mode": "replace", "status": 200, field: 503}
    with pytest.raises(rules.ValidationError) as caught:
        rules.validate_override(override)
    assert f"{field!r}" in str(caught.value)
    assert "an override may only carry" in str(caught.value)


def test_validate_override_reports_the_typo_before_the_error_it_causes():
    """`{"mod": "replace"}` has two problems, and only one of them is real. Reporting the missing
    `mode` sends the reader to the field they did not misspell."""
    with pytest.raises(rules.ValidationError) as caught:
        rules.validate_override({"mod": "replace", "match": {"path": "/api/items"}})
    assert "'mod'" in str(caught.value)
    assert "mode must be one of" not in str(caught.value)


@pytest.mark.parametrize("notes", ["", "why this rule exists"])
def test_validate_override_accepts_notes_as_a_string(notes):
    """JSON has no comments and sessions are written by hand, so `notes` is the one field the engine
    keeps without reading — and it must survive the round trip a session load makes."""
    override = {"mode": "replace", "match": {"path": "/api/items"}, "status": 200, "notes": notes}
    assert rules.validate_override(override)["notes"] == notes
    loaded = rules.normalise_session({"overrides": [override]}, "s")
    assert not loaded["_problems"]
    assert loaded["overrides"][0]["notes"] == notes


@pytest.mark.parametrize("notes", [None, 7, True, {"why": "x"}, ["x"]])
def test_validate_override_rejects_non_string_notes(notes):
    """Text or nothing. An explicit `null` is a mistake worth naming here, unlike the other optional
    fields, because the only reason to write the key at all is to put words in it."""
    with pytest.raises(rules.ValidationError, match="notes must be a string"):
        rules.validate_override({"mode": "replace", "match": {"path": "/api/items"},
                                 "status": 200, "notes": notes})


EVERY_FIELD_RULES = {
    "replace": {"id": "ovr_items", "active": True, "match": {"path": "/api/items"},
                "mode": "replace", "delayMs": 250, "status": 201,
                "headers": {"Content-Type": "application/json"}, "body": {"items": []},
                "notes": "the empty-state screen"},
    "patch": {"match": {"path": "/api/features"}, "mode": "patch",
              "patch": {"features": [{"id": "BETA", "enabled": True}]},
              "patchStrategy": "appendToArray"},
    "sequenced": {"match": {"path": "/api/orders"}, "mode": "replace",
                  "sequence": {"steps": [{"status": 202}, {"status": 200}]}},
}


@pytest.mark.parametrize("rule", list(EVERY_FIELD_RULES.values()), ids=list(EVERY_FIELD_RULES))
def test_a_rule_using_every_documented_field_validates(rule):
    """Between them these three use every field in OVERRIDE_FIELDS, so a field the vocabulary
    advertises but some later check refuses fails here rather than in front of the author who read
    the help and believed it."""
    assert rules.validate_override(rule) == rule


def test_the_documented_override_fields_are_all_exercised_above():
    """The other half of the pairing: a field added to the vocabulary but to no rule above would let
    the test pass while nothing proves validation accepts it."""
    used = set().union(*(set(rule) for rule in EVERY_FIELD_RULES.values()))
    assert used == set(rules.OVERRIDE_FIELDS)


def test_normalise_session_drops_a_rule_with_an_unknown_field_and_names_it():
    """A saved session written before this check loads with the rule gone and the field named — the
    rule never did what its author meant, and silently keeping it is how that stayed invisible."""
    session = rules.normalise_session(
        {"overrides": [{"mode": "replace", "match": {"path": "/api/items"}, "statsu": 503}]}, "s")
    assert session["overrides"] == []
    assert len(session["_problems"]) == 1
    assert "override[0]: unknown field 'statsu'" in session["_problems"][0]


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
    """Ids address a rule: add replaces by id, reset names one by id, and sequence state is keyed
    by it. Two rules sharing an id would share a cursor."""
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
    contain one — but a hand-edited or imported file can, and `_ruleRuntime` reaching the store
    means either a crash inside a proxy hook or a scenario that quietly starts on step 2."""
    session = rules.normalise_session({"_ruleRuntime": injected, "overrides": []}, "s")
    assert "_ruleRuntime" not in session


# MARK: - Explaining a matcher
#
# `matches_matcher` is now `explain_matcher(...) is None`, so these pin the edges where the two
# could have parted company. Validation checks these fields' types but not their emptiness, and a
# saved session may carry `""` or `{}` — which the wire has always treated as "no constraint".

@pytest.mark.parametrize("matcher,method,path,query,body,expected", [
    # Fields validation accepts but does not require to be non-empty. The wire has always read
    # these as "no constraint", and a saved session may contain them.
    ({}, "GET", "/a", {}, "", True),
    ({"method": ""}, "POST", "/a", {}, "", True),
    ({"path": ""}, "GET", "/a", {}, "", True),
    ({"query": {}}, "GET", "/a", {}, "", True),
    ({"bodyContains": ""}, "GET", "/a", {}, "", True),
    # Method comparison is case-insensitive in both directions.
    ({"method": "get"}, "GET", "/a", {}, "", True),
    ({"method": "GET"}, "get", "/a", {}, "", True),
    # `*` is the only wildcard; a rule may carry a non-string query value; extra parameters on the
    # request are ignored.
    ({"path": "/api/*/x"}, "GET", "/api/v1/x", {}, "", True),
    ({"path": "/api/*/x"}, "GET", "/api/v1/y", {}, "", False),
    ({"query": {"page": 2}}, "GET", "/a", {"page": "2"}, "", True),
    ({"query": {"k": "v"}}, "GET", "/a", {"k": "v", "other": "z"}, "", True),
    ({"bodyContains": "id"}, "GET", "/a", {}, "the id here", True),
    ({"method": "POST"}, "GET", "/a", {}, "", False),
    ({"path": "/b"}, "GET", "/a", {}, "", False),
    ({"query": {"k": "v"}}, "GET", "/a", {}, "", False),
    ({"query": {"k": "v"}}, "GET", "/a", {"k": "w"}, "", False),
    ({"bodyContains": "id"}, "GET", "/a", {}, "nothing", False),
])
def test_matching_semantics_are_pinned_field_by_field(matcher, method, path, query, body, expected):
    """`matches_matcher` is `explain_matcher(...) is None`, so asserting only that the two agree
    would be a tautology. These pin the *outcome* — which is what must not change now that every
    match runs through the explainer."""
    assert rules.matches_matcher(matcher, method, path, query, body) is expected
    assert (rules.explain_matcher(matcher, method, path, query, body) is None) is expected


@pytest.mark.parametrize("matcher,query,expected_field", [
    ({"method": "POST", "path": "/b"}, {}, "method"),
    ({"path": "/b", "query": {"k": "v"}}, {}, "path"),
    ({"query": {"k": "v"}}, {"k": "w"}, "query.k"),
    ({"bodyContains": "zzz"}, {}, "bodyContains"),
])
def test_the_reason_names_the_first_field_that_failed(matcher, query, expected_field):
    """One reason, not a list: the later checks are only meaningful once the earlier ones pass, and
    a list of every mismatch buries the one that matters."""
    reason = rules.explain_matcher(matcher, "GET", "/a", query, "body")
    assert reason is not None and reason.startswith(f"{expected_field}:")


def test_a_missing_query_parameter_reads_differently_from_a_wrong_one():
    """"has no 'kind'" and "has 'beta'" send you to different places — the app is not sending the
    parameter at all, versus it is sending a different value."""
    absent = rules.explain_matcher({"query": {"kind": "alpha"}}, "GET", "/a", {}, "")
    wrong = rules.explain_matcher({"query": {"kind": "alpha"}}, "GET", "/a", {"kind": "beta"}, "")
    assert absent is not None and "has no 'kind'" in absent
    assert wrong is not None and "has 'beta'" in wrong


def test_the_matcher_help_covers_exactly_the_accepted_fields():
    """The CLI help is generated from MATCHER_FIELD_HELP and validation from MATCHER_FIELDS. If they
    could drift, a documented field would be rejected or a supported one stay invisible."""
    assert tuple(rules.MATCHER_FIELD_HELP) == rules.MATCHER_FIELDS


@pytest.mark.parametrize("match", [[], "", 0, False, "/api/items", 7])
def test_a_match_that_is_not_an_object_is_rejected(match):
    """`or {}` used to let a falsy non-object through the object check, after which the wire read it
    as an *absent* matcher — a rule that answers every intercepted request."""
    with pytest.raises(rules.ValidationError, match="match must be a JSON object"):
        rules.validate_override({"mode": "replace", "status": 200, "match": match})


def test_an_absent_match_is_still_allowed():
    """Absent is the documented spelling of "no matcher", and it is occasionally useful."""
    assert rules.validate_override({"mode": "replace", "status": 200})["mode"] == "replace"
    assert rules.validate_override({"mode": "replace", "status": 200, "match": {}})


def test_an_empty_body_constraint_does_not_win_on_specificity():
    """`bodyContains: ""` is ignored when matching, so counting it as a constraint let it outrank
    an otherwise identical rule and answer in its place — "more specific" claiming a smaller set of
    requests than it actually matches."""
    generic = {"id": "generic", "mode": "replace", "match": {"path": "/api/items"}}
    empty = {"id": "empty", "mode": "replace",
             "match": {"path": "/api/items", "bodyContains": ""}}
    assert rules.matches(generic, "GET", "/api/items", {}, "body")
    assert rules.matches(empty, "GET", "/api/items", {}, "body")
    winner = rules.find_override([generic, empty], "GET", "/api/items", {}, "body")
    assert winner["id"] == "generic", "neither is more specific; order decides, not a phantom field"


def test_a_real_body_constraint_still_wins_on_specificity():
    generic = {"id": "generic", "mode": "replace", "match": {"path": "/api/items"}}
    pinned = {"id": "pinned", "mode": "replace",
              "match": {"path": "/api/items", "bodyContains": "kind"}}
    winner = rules.find_override([generic, pinned], "GET", "/api/items", {}, "kind=alpha")
    assert winner["id"] == "pinned"


def test_an_explicit_null_match_is_rejected():
    """`.get("match")` cannot tell `null` from omission, and the two are not the same claim: one
    says "no matcher", the other says "here is my matcher" and hands over nothing."""
    with pytest.raises(rules.ValidationError, match="match must be a JSON object"):
        rules.validate_override({"mode": "replace", "status": 200, "match": None})


# MARK: - Session schema version

def test_a_session_without_a_schema_version_is_read_as_version_1():
    """The field has always been optional, and hand-writing a session is the documented workflow."""
    assert rules.normalise_session({"overrides": []}, "s")["schemaVersion"] == 1


def test_an_unsupported_schema_version_refuses_the_whole_session():
    """A file written for a format this engine does not know would be read with the wrong rules —
    silently, and only in the ways the format changed. Refusing turns it into a session the loader
    names as skipped, which is a different thing to act on from one that loaded and behaves oddly."""
    with pytest.raises(rules.ValidationError, match="schemaVersion"):
        rules.normalise_session({"schemaVersion": 2, "overrides": []}, "s")


def test_a_boolean_schema_version_is_not_read_as_version_1():
    """`isinstance(True, int)` is True, so `"schemaVersion": true` would otherwise load as the
    version it happens to equal rather than being reported as the typo it is."""
    with pytest.raises(rules.ValidationError, match="schemaVersion"):
        rules.normalise_session({"schemaVersion": True, "overrides": []}, "s")


@pytest.mark.parametrize("version", ["1", 1.0, None, [1]])
def test_a_non_integer_schema_version_is_refused(version):
    with pytest.raises(rules.ValidationError, match="schemaVersion"):
        rules.normalise_session({"schemaVersion": version, "overrides": []}, "s")


def test_a_valid_rule_survives_a_refused_schema_version_nowhere():
    """The refusal is whole-session: no part of a file stamped with an unknown version is kept,
    because nothing in it can be trusted to mean what this engine would read it as."""
    payload = {"schemaVersion": 99,
               "overrides": [{"mode": "replace", "status": 200, "match": {"path": "/a"}}]}
    with pytest.raises(rules.ValidationError):
        rules.normalise_session(payload, "s")


# MARK: - Numbers that are not numbers
#
# `json.loads` accepts `1e309`, `Infinity` and `NaN` and hands back floats. Validation has to refuse
# them as rules, not fall over on them: an exception that is not a ValidationError escapes every
# `except ValidationError` between here and the caller, so one such rule costs a whole file its
# diagnostics — and the operator gets a traceback where the field name belongs.

@pytest.mark.parametrize("delay", [float("inf"), -float("inf"), float("nan"), 1e309])
def test_a_non_finite_delay_is_refused_as_a_rule_not_as_a_crash(delay):
    with pytest.raises(rules.ValidationError, match="finite"):
        rules.validate_override({"mode": "replace", "status": 200, "delayMs": delay})


@pytest.mark.parametrize("status", [float("inf"), float("nan"), 10**400])
def test_a_status_that_is_not_a_usable_integer_is_refused_the_same_way(status):
    """The other numeric fields were checked for the same hole: `status` (on the rule and on every
    sequence step) and `schemaVersion` all test `isinstance(..., int)` before comparing, so no
    float ever reaches a conversion. `delayMs` was the only one that converted."""
    with pytest.raises(rules.ValidationError):
        rules.validate_override({"mode": "replace", "status": status})
    with pytest.raises(rules.ValidationError):
        rules.validate_override({"mode": "replace", "status": 200,
                                 "sequence": {"steps": [{"status": status}]}})


@pytest.mark.parametrize("version", [float("inf"), float("nan"), 10**400])
def test_a_schema_version_that_is_not_a_usable_integer_is_refused_the_same_way(version):
    with pytest.raises(rules.ValidationError, match="schemaVersion"):
        rules.normalise_session({"schemaVersion": version, "overrides": []}, "s")
