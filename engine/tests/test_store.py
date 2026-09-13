"""Store persistence, path containment, and the crash paths that used to take the proxy down."""

import errno
import json

import pytest

import config
import rules
import store


def make_store(profile):
    (profile / "profile.json").write_text('{"hosts": []}')
    config.reload_profile()
    return store.Store()


# MARK: - Name validation (these names become path components)


@pytest.mark.parametrize("name", ["../escape", "a/b", "..", ".", "", ".hidden", "a" * 65, "sess ion"])
def test_unsafe_names_are_rejected(name):
    with pytest.raises(store.UnsafeName):
        store.safe_component(name)


@pytest.mark.parametrize("name", ["default", "orders-outage", "a_b.c", "S1"])
def test_reasonable_names_are_accepted(name):
    assert store.safe_component(name) == name


# MARK: - Scenarios


def test_a_malformed_scenario_file_does_not_block_startup(profile):
    """One bad file used to raise AttributeError and stop the proxy from starting at all."""
    (profile / "scenarios" / "bad.json").write_text("[]")
    (profile / "scenarios" / "good.json").write_text(json.dumps({"name": "good", "overrides": []}))
    subject = make_store(profile)
    assert "good" in subject.scenarios
    assert any("bad.json" in problem for problem in subject.load_problems)


def test_unparseable_json_does_not_block_startup(profile):
    (profile / "scenarios" / "broken.json").write_text("{not json")
    subject = make_store(profile)
    assert subject.active_name == "default"
    assert any("broken.json" in problem for problem in subject.load_problems)


def test_startup_does_not_rewrite_existing_scenario_files(profile):
    """A profile kept in git must not go dirty just because the proxy started."""
    path = profile / "scenarios" / "kept.json"
    original = json.dumps({"name": "kept", "overrides": []})  # deliberately not indent=2
    path.write_text(original)
    make_store(profile)
    assert path.read_text() == original


def test_binary_junk_in_the_scenarios_directory_does_not_block_startup(profile):
    """Regression: `read_text` raises UnicodeDecodeError, which is a ValueError but neither an
    OSError nor a JSONDecodeError — so a file of binary junk escaped every arm of the loader and
    took the proxy down at startup, from the one directory operators are told to hand-edit."""
    (profile / "scenarios" / "junk.json").write_bytes(b"\xff\xfe\x00binary")
    subject = make_store(profile)
    assert subject.active_name == "default"
    assert any("junk.json" in problem for problem in subject.load_problems)


def test_an_unsupported_schema_version_is_skipped_and_named(profile):
    (profile / "scenarios" / "future.json").write_text(
        json.dumps({"schemaVersion": 2, "name": "future", "overrides": []})
    )
    subject = make_store(profile)
    assert "future" not in subject.scenarios, "a scenario this engine cannot read must not load"
    assert any("future.json" in problem and "schemaVersion" in problem for problem in subject.load_problems)


# MARK: - The shared scenario loader
#
# `load_scenario_file` is what startup loads with, so an offline inspection command reports what the
# proxy would do rather than a second opinion about it. These tests pin that it is the same answer.


def _write(profile, name, payload):
    path = profile / "scenarios" / f"{name}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def _scenario(profile, name, *overrides):
    """A scenario file holding these rules, which is the only way a rule reaches a store.

    Nothing in the engine writes into a profile, so a test that needs live state writes the file an
    agent would have written and lets the loader read it.
    """
    return _write(profile, name, {"name": name, "overrides": [dict(override) for override in overrides]})


def test_load_scenario_file_keeps_the_good_rules_and_names_the_dropped_ones(profile):
    path = _write(
        profile,
        "partial",
        {
            "name": "partial",
            "overrides": [
                {"id": "keep", "mode": "replace", "status": 200, "match": {"path": "/a"}},
                {"id": "typo", "mode": "replace", "status": 200, "match": {"paths": "/b"}},
                {"id": "keep", "mode": "replace", "status": 204, "match": {"path": "/c"}},
            ],
        },
    )
    scenario, problems = store.load_scenario_file(path)
    assert [o["id"] for o in scenario["overrides"]] == ["keep"]
    assert any("override[1]" in p and "paths" in p for p in problems), "name the rule and the field"
    assert any("override[2]" in p and "duplicate id" in p for p in problems)
    assert all(p.startswith("partial.json:") for p in problems), "every problem names its file"


def test_load_scenario_file_reports_nothing_kept_as_a_none_scenario(profile):
    """None and an empty scenario are different answers: one says the scenario is not loaded, the
    other says it loaded and has no rules in it."""
    scenario, problems = store.load_scenario_file(_write(profile, "broken", "{not json"))
    assert scenario is None
    assert problems and problems[0].startswith("skipped broken.json:")


def test_load_scenario_file_refuses_a_file_whose_name_could_escape_the_profile(profile):
    """The stem becomes a scenario name, and a scenario name becomes a path component."""
    path = profile / "scenarios" / "..json"
    path.write_text("{}")
    scenario, problems = store.load_scenario_file(path)
    assert scenario is None
    assert "invalid scenario name" in problems[0]


def test_load_scenario_file_creates_nothing(profile):
    """An offline inspection must leave the profile exactly as it found it — no directories, no
    synthesised `default`."""
    path = _write(profile, "s", {"name": "s", "overrides": []})
    before = sorted(p.name for p in (profile / "scenarios").iterdir())
    store.load_scenario_file(path)
    store.load_scenario_file(profile / "scenarios" / "absent.json")
    assert sorted(p.name for p in (profile / "scenarios").iterdir()) == before


def test_startup_reports_exactly_what_the_shared_loader_reports(profile):
    """The point of the extraction: an offline verdict that could differ from startup's would be a
    second opinion, and the operator would have no way to know which one the proxy acts on."""
    files = [
        _write(profile, "good", {"name": "good", "overrides": []}),
        _write(profile, "broken", "{not json"),
        _write(profile, "partial", {"name": "partial", "overrides": [{"mode": "nonsense"}]}),
        _write(profile, "future", {"schemaVersion": 7, "name": "future", "overrides": []}),
    ]
    offline = [problem for file in sorted(files) for problem in store.load_scenario_file(file)[1]]
    assert make_store(profile).load_problems == offline


def test_scenario_path_refuses_a_name_that_would_escape_the_scenarios_directory(profile):
    with pytest.raises(store.UnsafeName):
        store.scenario_path("../../etc/passwd")


def test_a_scenario_file_symlinked_out_of_the_profile_is_not_loaded(profile, tmp_path):
    """Startup reaches its files through a glob, so nothing used to check them: a scenario symlinked
    out of the profile loaded into the proxy while `scenario_path` refused that same scenario by
    name — one file, two verdicts, and the permissive one was the one that ran."""
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "outside", "overrides": []}))
    link = profile / "scenarios" / "sneaky.json"
    link.symlink_to(outside)

    scenario, problems = store.load_scenario_file(link)
    assert scenario is None
    assert "escapes" in problems[0]
    assert "sneaky" not in make_store(profile).scenarios, "and startup refuses it for the same reason"


def test_the_loader_holds_a_file_to_the_containment_scenario_path_applies(profile, tmp_path):
    """The rule is `scenario_path`'s, exactly: resolve inside `scenarios/`. A link that leaves it,
    even into the same profile, is a file `scenario_path` refuses by name — so loading it anyway
    would give one identity two verdicts, and the permissive one is the one that runs."""
    elsewhere = profile / "shared.json"
    elsewhere.write_text(json.dumps({"name": "shared", "overrides": []}))
    link = profile / "scenarios" / "kept.json"
    link.symlink_to(elsewhere)
    with pytest.raises(store.UnsafeName):
        store.scenario_path("kept")
    scenario, problems = store.load_scenario_file(link)
    assert scenario is None and "escapes" in problems[0]


def test_the_loader_reads_the_path_that_passed_containment(profile, monkeypatch):
    """A link that resolves inside `scenarios/` passes the check; the read then went through the
    link again, so a link retargeted outside the profile in between was what got read and served.
    The loader reads the resolved path the check approved, and the link is never dereferenced twice."""
    real = profile / "scenarios" / "real.json"
    real.write_text(json.dumps({"name": "kept", "overrides": []}))
    link = profile / "scenarios" / "kept.json"
    link.symlink_to(real)
    read_from = []
    original = store.Path.read_text

    def recording(self, *args, **kwargs):
        read_from.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(store.Path, "read_text", recording)

    scenario, problems = store.load_scenario_file(link)

    assert scenario is not None and problems == []
    assert read_from == [real.resolve()], "read through the resolved path, not the link"


def test_a_scenario_file_that_points_at_itself_does_not_stop_the_proxy_starting(profile):
    """`Path.resolve()` raises `RuntimeError("Symlink loop from …")`, which is not an OSError. The
    containment check runs before the read, so an uncaught one is a single self-referencing file in
    a hand-edited directory stopping the proxy from starting at all — where before the check
    existed, `read_text` raised OSError and the file was simply skipped."""
    loop = profile / "scenarios" / "loop.json"
    loop.symlink_to(loop)
    (profile / "scenarios" / "good.json").write_text(json.dumps({"name": "good", "overrides": []}))

    scenario, problems = store.load_scenario_file(loop)
    assert scenario is None
    assert "cannot resolve path" in problems[0] and "loop.json" in problems[0]

    subject = make_store(profile)
    assert "good" in subject.scenarios, "one unresolvable file must not cost the others"
    assert any("loop.json" in problem for problem in subject.load_problems)


def test_a_rule_whose_delay_is_not_a_finite_number_is_a_reported_problem(profile):
    """`json.loads` turns `1e309` into `inf`, and `int(inf)` raises OverflowError — not a
    ValidationError, and not even a ValueError — from inside validation. The loader's `except
    ValidationError` never saw it, so one such rule took the whole file's diagnostics with it."""
    path = _write(
        profile,
        "wild",
        {
            "name": "wild",
            "overrides": [
                {"id": "ovr_slow", "mode": "replace", "status": 200, "delayMs": 1e309},
                {"id": "ovr_ok", "mode": "replace", "status": 200, "match": {"path": "/a"}},
            ],
        },
    )
    scenario, problems = store.load_scenario_file(path)
    assert [o["id"] for o in scenario["overrides"]] == ["ovr_ok"], "the good rule still loads"
    assert any("override[0]" in p and "finite" in p for p in problems)


# MARK: - Overrides


def test_containment_rejects_a_symlinked_scenarios_directory(profile, tmp_path):
    """Proving a path sits under scenarios/ is not enough if scenarios/ is itself a symlink out."""
    outside = tmp_path / "outside"
    outside.mkdir()
    scenarios = profile / "scenarios"
    for child in scenarios.iterdir():
        child.unlink()
    scenarios.rmdir()
    scenarios.symlink_to(outside, target_is_directory=True)
    with pytest.raises(store.UnsafeName):
        store._contained(config.SCENARIOS_DIR, "escaped.json")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"keep": {"mode": "replace"}}, id="object"),
        pytest.param(None, id="null"),
        pytest.param("[]", id="string"),
    ],
)
def test_a_scenario_whose_overrides_are_not_a_list_is_reported_not_emptied(profile, overrides):
    """`normalise_scenario` substitutes [] for a malformed `overrides`, so the file loads — and the
    problem has to be on the record, or a scenario with every rule lost reads as one with none."""
    (profile / "scenarios" / "broken.json").write_text(
        json.dumps({"name": "broken", "overrides": overrides}), encoding="utf-8"
    )
    scenario, problems = store.load_scenario_file(profile / "scenarios" / "broken.json")
    assert scenario is not None and scenario["overrides"] == []
    assert problems == ["broken.json: overrides must be a list"]


def test_starting_does_not_write_into_the_profile(profile):
    """A profile kept in git must not go dirty just because the proxy started. The in-memory
    `default` scenario used to be persisted, which created a file on first load."""
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    before = sorted(p.name for p in (profile / "scenarios").iterdir())
    subject = store.Store()
    after = sorted(p.name for p in (profile / "scenarios").iterdir())
    assert before == after, "startup wrote a file into the profile"
    assert subject.active_name == "default"
    assert "default" in subject.scenarios, "default must still exist in memory"


# MARK: - Sequence cursors
#
# The cursor is runtime state living on the scenario under a leading underscore, so the invariants
# worth pinning are: it moves when it should, it does NOT move when it should not, and it never
# escapes into a profile someone keeps in git.

SEQ = {
    "id": "seq",
    "mode": "replace",
    "match": {"method": "GET", "path": "/api/items"},
    "sequence": {"steps": [{"status": 201}, {"status": 202}]},
}


def _advanced_once(subject):
    """One request, in the order the addon does it: resolve, then advance.

    Bumping without resolving would not exercise the sticky overrun flag, which is set at selection
    — and which cannot be derived from the cursor, since an `advanceOn` rule never moves when it
    answers.
    """
    override = subject.find_override("GET", "/api/items", {}, "")
    subject.resolve_override(override)
    subject.bump_selected(override)


def test_a_self_triggered_sequence_advances_when_it_answers(profile):
    _scenario(profile, "default", SEQ)
    subject = store.Store()
    assert subject.sequence_states()[0]["nextStep"] == 1
    _advanced_once(subject)
    assert subject.sequence_states()[0]["nextStep"] == 2


def test_a_shadowed_sequenced_rule_does_not_advance(profile):
    """`find_override` returns only the most specific match, so a rule whose matcher fits may still
    not be the rule that answered. Advancing it anyway would spend a step it never served — and the
    next request it *does* answer would serve the wrong one, leaving the scenario off by one for the
    rest of its run. This is why `self` means 'answered' and not 'matched'."""
    _scenario(
        profile,
        "default",
        {
            "id": "seq",
            "mode": "replace",
            "match": {"path": "/api/orders/*"},
            "sequence": {"steps": [{"status": 201}, {"status": 202}]},
        },
        {"id": "specific", "mode": "replace", "status": 404, "match": {"path": "/api/orders/42"}},
    )
    subject = store.Store()

    picked = subject.find_override("GET", "/api/orders/42", {}, "")
    assert picked["id"] == "specific", "the rule with fewer wildcards answers"

    subject.bump_selected(picked)
    subject.advance_matching("GET", "/api/orders/42", {}, "")

    state = next(s for s in subject.sequence_states() if s["id"] == "seq")
    assert state["nextStep"] == 1, "the shadowed sequence must not have moved"


def test_an_advance_on_sequence_ignores_its_own_calls(profile):
    """The property the delete-then-refresh scenario depends on: a screen may fetch the list any
    number of times without consuming a step."""
    _scenario(
        profile,
        "default",
        {**SEQ, "sequence": {**SEQ["sequence"], "advanceOn": {"method": "DELETE", "path": "/api/items/*"}}},
    )
    subject = store.Store()
    for _ in range(3):
        _advanced_once(subject)
    assert subject.sequence_states()[0]["nextStep"] == 1

    assert subject.advance_matching("DELETE", "/api/items/b", {}, "") == ["seq"]
    assert subject.sequence_states()[0]["nextStep"] == 2


def test_advance_matching_ignores_a_rule_whose_matcher_does_not_fit(profile):
    _scenario(
        profile,
        "default",
        {**SEQ, "sequence": {**SEQ["sequence"], "advanceOn": {"method": "DELETE", "path": "/api/items/*"}}},
    )
    subject = store.Store()
    assert subject.advance_matching("DELETE", "/api/other/b", {}, "") == []


def test_an_inactive_sequenced_rule_is_never_advanced(profile):
    """A disabled rule that still moved on the wire would be a rule doing something while off."""
    _scenario(
        profile,
        "default",
        {
            **SEQ,
            "active": False,
            "sequence": {**SEQ["sequence"], "advanceOn": {"method": "DELETE", "path": "/api/items/*"}},
        },
    )
    subject = store.Store()
    assert subject.sequenced_overrides() == []
    assert subject.advance_matching("DELETE", "/api/items/b", {}, "") == []


def test_exhausted_and_overrun_are_reported_separately(profile):
    """The clamp at n+1 exists for this: at n, a second and a third advance event land on the same
    value and the two flags collapse into one."""
    _scenario(profile, "default", SEQ)
    subject = store.Store()

    for _ in range(2):
        _advanced_once(subject)
    state = subject.sequence_states()[0]
    assert (state["exhausted"], state["hasOverrun"]) == (True, False), "used up, nothing past it yet"

    _advanced_once(subject)
    state = subject.sequence_states()[0]
    assert (state["exhausted"], state["hasOverrun"]) == (True, True)


def test_resolve_override_does_not_move_the_cursor(profile):
    """Selection and advancement are separate so a request is answered from the state it arrived
    in — you never see your own write reflected in its own response."""
    _scenario(profile, "default", SEQ)
    subject = store.Store()
    override = subject.find_override("GET", "/api/items", {}, "")
    for _ in range(3):
        action, view, progress = subject.resolve_override(override)
        assert (action, view["status"], progress["selectedStep"]) == (rules.APPLY, 201, 1)


# MARK: - Runtime never escapes


def test_switching_scenarios_restarts_the_scenario(profile):
    _scenario(profile, "default", SEQ)
    _scenario(profile, "other")
    subject = store.Store()
    _advanced_once(subject)
    subject.set_active("other")
    subject.set_active("default")
    assert subject.sequence_states()[0]["nextStep"] == 1, "a scenario always begins at its first step"


# MARK: - Reset


def test_reset_rewinds_and_issues_a_new_run_id(profile):
    _scenario(profile, "default", SEQ)
    subject = store.Store()
    before = subject.sequence_states()[0]["runId"]
    _advanced_once(subject)

    result = subject.reset_runtime()
    assert list(result["reset"]) == ["seq"]
    after = subject.sequence_states()[0]
    assert after["nextStep"] == 1
    assert after["runId"] != before, "a new run so a stale event cannot satisfy a wait"


def test_resetting_an_unknown_id_is_distinguishable_from_resetting_nothing(profile):
    """None rather than an empty result: a caller that cannot tell them apart believes a typo
    worked."""
    _scenario(profile, "default", SEQ)
    subject = store.Store()
    assert subject.reset_runtime("nope") is None
    assert subject.reset_runtime()["reset"] != {}


def test_reset_covers_plain_rules_not_just_sequenced_ones(profile):
    """Every rule carries run state now — an answer count — so a reset that skipped plain rules
    would leave the one boundary a test can draw unavailable to exactly the rules that need it."""
    _scenario(profile, "default", {"id": "plain", "mode": "replace", "status": 200})
    subject = store.Store()
    assert list((subject.reset_runtime() or {})["reset"]) == ["plain"]


def test_resetting_a_scenario_with_no_rules_reports_nothing_to_do(profile):
    subject = store.Store()
    assert subject.reset_runtime() == {"scenario": "default", "reset": {}}


def test_a_served_overrun_is_reported_even_when_the_cursor_cannot_move(profile):
    """A rule with an explicit `advanceOn` does not move when it answers, so it can serve the
    exhausted response repeatedly with the cursor sitting still. Deriving `hasOverrun` from the
    cursor alone therefore reported false while /recent recorded the overrun — two answers to the
    same question."""
    _scenario(
        profile,
        "default",
        {**SEQ, "sequence": {**SEQ["sequence"], "advanceOn": {"method": "DELETE", "path": "/api/items/*"}}},
    )
    subject = store.Store()
    for suffix in ("a", "b"):
        subject.advance_matching("DELETE", f"/api/items/{suffix}", {}, "")
    assert subject.sequence_states()[0]["hasOverrun"] is False, "exhausted, but nothing served past it"

    _, _, progress = subject.resolve_override(subject.find_override("GET", "/api/items", {}, ""))
    assert progress["overrun"] is True
    assert subject.sequence_states()[0]["hasOverrun"] is True, "and health must agree with /recent"


def test_serves_count_each_step_even_when_the_cursor_cannot_move(profile):
    """For an `advanceOn` rule the cursor stays put while it answers, and /recent is a bounded
    window — so the counter in live state is the only durable evidence a step was served.
    `sequence wait` reads it for exactly that reason."""
    _scenario(
        profile,
        "default",
        {**SEQ, "sequence": {**SEQ["sequence"], "advanceOn": {"method": "DELETE", "path": "/api/items/*"}}},
    )
    subject = store.Store()
    for _ in range(2):
        subject.resolve_override(subject.find_override("GET", "/api/items", {}, ""))
    state = subject.sequence_states()[0]
    assert state["serves"] == {"1": 2}
    assert state["nextStep"] == 1, "served twice, moved never"


def test_an_overrun_serve_is_not_counted_as_a_step(profile):
    """Past the last step there is no step being served; the overrun has its own flags."""
    _scenario(profile, "default", SEQ)
    subject = store.Store()
    for _ in range(3):
        _advanced_once(subject)
    assert subject.sequence_states()[0]["serves"] == {"1": 1, "2": 1}


def test_reset_clears_the_serve_counts(profile):
    _scenario(profile, "default", SEQ)
    subject = store.Store()
    _advanced_once(subject)
    assert subject.sequence_states()[0]["serves"] == {"1": 1}
    subject.reset_runtime()
    assert subject.sequence_states()[0]["serves"] == {}


def test_reset_clears_a_recorded_overrun(profile):
    _scenario(profile, "default", SEQ)
    subject = store.Store()
    for _ in range(3):
        _advanced_once(subject)
    assert subject.sequence_states()[0]["hasOverrun"] is True
    subject.reset_runtime()
    assert subject.sequence_states()[0]["hasOverrun"] is False


def test_reset_always_issues_a_different_run_id(profile, monkeypatch):
    """The token is what stops a retained event from an earlier run satisfying a wait, so a reset
    reissuing the same value has to be impossible rather than merely unlikely."""
    _scenario(profile, "default", SEQ)
    subject = store.Store()
    seen = {subject.sequence_states()[0]["runId"]}
    for _ in range(20):
        issued = subject.reset_runtime()["reset"]["seq"]
        assert issued not in seen
        seen.add(issued)

    # Random tokens never collide in twenty tries, so the retry that makes this a guarantee is only
    # exercised by handing the generator the collision: the first token it offers is the one the
    # rule already holds, and the reset has to reject it and ask again.
    held = subject.sequence_states()[0]["runId"]
    offered = iter([held, "second-token"])
    monkeypatch.setattr(store.secrets, "token_hex", lambda _: next(offered))
    issued = subject.reset_runtime()["reset"]["seq"]
    assert issued != held, "the token the rule already held was reissued"
    assert issued == "second-token"


# MARK: - Answer counts
#
# The evidence a test asserts on. Every failure below is one where the count would have said a mock
# was in play when it was not — the exact defect the assertion exists to catch.


def test_a_captured_slot_credits_nobody_after_the_scenario_is_switched(profile):
    """A patch is selected in the request hook and only answers a round trip later, in the response
    hook. Looking the rule up by id at that point would credit whatever rule the *now* active
    scenario happens to file under that id — a different rule, in a different scenario."""
    shared = {"id": "shared", "mode": "patch", "patch": {}}
    _scenario(profile, "default", shared)
    _scenario(profile, "other", shared)
    subject = store.Store()
    slot = subject.answer_slot("shared")

    subject.set_active("other")

    store.credit(slot)  # the in-flight patch from the previous scenario lands now
    assert subject.answer_states() == [{"id": "shared", "active": True, "count": 0, "runId": None}]


def test_a_captured_slot_credits_nobody_after_a_reset(profile):
    """The third way a run ends, and the only one an operator asks for by name: a rewind while a
    patch is in the air. The answer belongs to the run that was ended, so a reset has to hand the
    rule a *new* slot — reusing the object and clearing its fields would leave the in-flight answer
    counted into the fresh run the operator just drew a boundary around."""
    _scenario(profile, "default", {"id": "r", "mode": "patch", "patch": {}})
    subject = store.Store()
    slot = subject.answer_slot("r")
    issued = subject.reset_runtime("r")["reset"]["r"]

    store.credit(slot)  # the in-flight patch from the run that just ended lands now
    assert subject.answer_states() == [{"id": "r", "active": True, "count": 0, "runId": issued}]


def test_reading_answer_states_does_not_mint_run_state(profile):
    """Asking how many answers a rule has must not create the runtime entry that a reset issues a
    run id for — a question is not an event."""
    _scenario(profile, "default", {"id": "untouched", "mode": "replace", "status": 200})
    subject = store.Store()
    subject.answer_states()
    assert store._runtime(subject.active_scenario()) == {}


def test_switching_scenario_clears_answer_counts(profile):
    _scenario(profile, "default", {"id": "a", "mode": "replace", "status": 200})
    _scenario(profile, "scratch")
    subject = store.Store()
    store.credit(subject.answer_slot("a"))
    subject.set_active("scratch")
    subject.set_active("default")
    assert subject.answer_states() == [{"id": "a", "active": True, "count": 0, "runId": None}]


def test_reset_clears_one_rules_answer_count_and_leaves_the_others(profile):
    _scenario(
        profile, "default", {"id": "a", "mode": "replace", "status": 200}, {"id": "b", "mode": "replace", "status": 200}
    )
    subject = store.Store()
    store.credit(subject.answer_slot("a"))
    store.credit(subject.answer_slot("b"))
    subject.reset_runtime("a")
    assert [(s["id"], s["count"]) for s in subject.answer_states()] == [("a", 0), ("b", 1)]


def test_answer_states_report_an_inactive_rule_as_inactive(profile):
    """A rule that is switched off can never answer, so a wait on it should fail at once rather
    than burn its timeout."""
    _scenario(profile, "default", {"id": "off", "active": False, "mode": "replace", "status": 200})
    subject = store.Store()
    assert subject.answer_states() == [{"id": "off", "active": False, "count": 0, "runId": None}]


# MARK: - Which run the answers belong to
#
# A count on its own says "some run's". Every test here is a way another run's evidence could be
# handed to a caller asking about the boundary it drew — the same shape as a stale sequence event
# satisfying a wait, one step further out: the rule id survives everything that ends a run.


def test_reset_issues_the_run_id_the_following_answers_are_counted_under(profile):
    """The whole flow in one place: reset hands back a token, and what the rule answers afterwards
    is reported under exactly that token. Without this, a caller has nothing to compare against."""
    _scenario(profile, "default", {"id": "a", "mode": "replace", "status": 200})
    subject = store.Store()
    issued = subject.reset_runtime("a")["reset"]["a"]
    store.credit(subject.answer_slot("a"))
    assert subject.answer_states() == [{"id": "a", "active": True, "count": 1, "runId": issued}]


def test_a_reset_leaves_the_previous_run_id_unclaimable(profile):
    """A count is only ever evidence about the run it was taken in, so the run that produced it has
    to be nameable — and a reset has to make the previous name stop matching."""
    _scenario(profile, "default", {"id": "a", "mode": "replace", "status": 200})
    subject = store.Store()
    store.credit(subject.answer_slot("a"))
    before = subject.answer_states()[0]["runId"]
    subject.reset_runtime("a")
    store.credit(subject.answer_slot("a"))
    after = subject.answer_states()[0]
    assert after["count"] == 1, "the new run has its own answer"
    assert after["runId"] != before, "and cannot be mistaken for the run before it"


def test_a_scenario_switch_answers_in_a_different_run_under_the_same_id(profile):
    """Two scenarios can file a rule under one id. Reading a count from the second while holding the
    first's run token is the substitution this field exists to make visible."""
    shared = {"id": "shared", "mode": "replace", "status": 200}
    _scenario(profile, "default", shared)
    _scenario(profile, "other", shared)
    subject = store.Store()
    issued = subject.reset_runtime("shared")["reset"]["shared"]
    subject.set_active("other")
    store.credit(subject.answer_slot("shared"))
    assert subject.answer_states()[0]["runId"] != issued


def test_a_rule_that_has_no_run_reports_none_rather_than_a_run_with_no_answers(profile):
    """`null` says "there is no run here"; a count of zero says "there was a run and nothing
    answered". Collapsing the first into the second is how a caller believes a boundary it never
    drew — the reading-does-not-mint rule is what makes the distinction possible."""
    _scenario(profile, "default", {"id": "untouched", "mode": "replace", "status": 200})
    subject = store.Store()
    assert subject.answer_states() == [{"id": "untouched", "active": True, "count": 0, "runId": None}]


def test_answer_and_sequence_states_report_one_run_not_two(profile):
    """One identity per rule, reported by both views. Two tokens for the same boundary would let a
    caller bind a wait and an assertion to different things and never find out."""
    _scenario(profile, "default", SEQ)
    subject = store.Store()
    subject.reset_runtime("seq")
    answers = {state["id"]: state["runId"] for state in subject.answer_states()}
    assert answers["seq"] == subject.sequence_states()[0]["runId"]


# MARK: - Identity for the recent list
#
# `/recent` is polled, and a client keeps a selection across polls. Position cannot carry that — a
# new request pushes every row down — and neither can content, since the same request repeated is
# indistinguishable from itself. So the engine names each entry.


def test_recent_entries_get_increasing_ids(profile):
    """Unique and ordered, so a client can key a selection to a row and know which of two entries
    for the same request it holds. Two identical requests differ in nothing else."""
    subject = make_store(profile)
    for _ in range(3):
        subject.record_recent({"method": "GET", "path": "/api/v1/orders"})
    ids = [entry["id"] for entry in subject.recent_list()]
    assert ids == ["evt-3", "evt-2", "evt-1"], "newest first, as the list itself is"


def test_a_supplied_id_does_not_collide_with_the_counter(profile):
    """The id is the engine's to give. Honouring a caller's `evt-2` would put two rows in one list
    under one name the moment the counter reached it, and a client selecting by identity would hold
    whichever of them it happened to find first."""
    subject = make_store(profile)
    subject.record_recent({"id": "evt-2", "method": "GET", "path": "/api/v1/orders"})
    subject.record_recent({"method": "GET", "path": "/api/v1/orders"})
    ids = [entry["id"] for entry in subject.recent_list()]
    assert ids == ["evt-2", "evt-1"], "the counter's own numbering, not the caller's"


@pytest.mark.parametrize("supplied", [None, 7, {"nested": True}])
def test_a_recent_id_replaces_whatever_the_caller_supplied(profile, supplied):
    """`null` and a number are not names a client can select by — the first cannot be told from a
    row with no id and the second is not the type the API promises. Defaulting would have kept both.
    """
    subject = make_store(profile)
    subject.record_recent({"id": supplied, "method": "GET", "path": "/api/v1/orders"})
    assert subject.recent_list()[0]["id"] == "evt-1"


def test_a_recorded_entry_is_not_written_back_into_the_callers_dict(profile):
    """The store copies. `addon._record` builds one dict per request and hands it over; an id
    written into it would be state the caller did not ask for and cannot see the rules of."""
    subject = make_store(profile)
    entry = {"method": "GET", "path": "/api/v1/orders"}
    subject.record_recent(entry)
    assert entry == {"method": "GET", "path": "/api/v1/orders"}
    assert subject.recent_list()[0]["id"] == "evt-1"


def test_recent_ids_keep_increasing_when_the_list_rolls_over(profile):
    """The deque is bounded, and the counter is not: once `RECENT_CAP` entries have gone in, every
    further one drops the oldest. An id derived from the list's length or its position would start
    repeating here, and a client's stored selection would silently match a different request."""
    subject = make_store(profile)
    for _ in range(config.RECENT_CAP + 5):
        subject.record_recent({"method": "GET", "path": "/api/v1/orders"})
    ids = [entry["id"] for entry in subject.recent_list()]
    assert len(ids) == config.RECENT_CAP, "the list itself is still bounded"
    assert ids[0] == f"evt-{config.RECENT_CAP + 5}", "the newest keeps counting past the cap"
    assert ids[-1] == "evt-6", "and the oldest survivor is the sixth, not the first"
    assert len(set(ids)) == len(ids), "no id is reused"


def test_recent_ids_are_per_store(profile):
    """The counter belongs to the store, not to the module: a second proxy — or the next test —
    must not continue somebody else's numbering, and nothing here promises ids that outlive the
    process, which is why the README says they restart with it."""
    first = make_store(profile)
    first.record_recent({"method": "GET", "path": "/api/v1/orders"})
    second = make_store(profile)
    second.record_recent({"method": "GET", "path": "/api/v1/orders"})
    assert first.recent_list()[0]["id"] == second.recent_list()[0]["id"] == "evt-1"


# MARK: - Scenario groups
#
# One level of folder under `scenarios/`, and the path is the name. Everything here is a failure
# path: the ways a grouped profile can be misread, and the ways a move or a reload can half-happen.


def _group_file(profile, group, name, payload=None):
    directory = profile / "scenarios" / group
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(payload if payload is not None else {"overrides": []}))
    return path


@pytest.mark.parametrize(
    "name",
    ["../x", "a/../b", "a//b", "a/b/c", "a\\b", "/a", "a/", "", "%2e%2e/x", ".hidden/x", "a/.hidden"],
)
def test_qualified_names_refuse_traversal_separators_and_depth(profile, name):
    """Every component of a qualified name is still a path component, and nesting stops at one level.

    `scenario_parts` cannot delegate the whole string to `safe_component` — that pattern has no `/`
    in it — so the split is the one place a separator is ever accepted, and it has to refuse
    everything else itself. `scenario_path` is checked alongside it because a name that got past
    parsing but not containment would be a name the loader and a write disagreed about.
    """
    make_store(profile)
    with pytest.raises(store.UnsafeName):
        store.scenario_parts(name)
    with pytest.raises(store.UnsafeName):
        store.scenario_path(name)


def test_two_groups_may_each_hold_a_scenario_with_the_same_stem(profile):
    """The point of grouping. Keying scenarios by `file.stem` would let one silently replace the
    other, leaving a profile that lists one scenario where its author put two."""
    _group_file(profile, "checkout", "retry", {"overrides": [{"id": "a", "mode": "replace", "status": 200}]})
    _group_file(profile, "archive", "retry", {"overrides": [{"id": "b", "mode": "replace", "status": 500}]})

    subject = make_store(profile)

    assert "checkout/retry" in subject.scenarios and "archive/retry" in subject.scenarios
    assert [o["id"] for o in subject.scenarios["checkout/retry"]["overrides"]] == ["a"]
    assert [o["id"] for o in subject.scenarios["archive/retry"]["overrides"]] == ["b"]


def test_a_third_level_is_a_reported_load_problem_not_a_skipped_file(profile):
    """A file too deep to name is reported, never quietly absent: an operator who put it there is
    looking for it, and "no such scenario" would send them to the wrong question."""
    nested = profile / "scenarios" / "checkout" / "retries"
    nested.mkdir(parents=True)
    (nested / "slow.json").write_text(json.dumps({"overrides": []}))

    subject = make_store(profile)

    assert not any(name.startswith("checkout") for name in subject.scenarios)
    assert any("nest one level deep" in problem for problem in subject.load_problems)
    assert any("checkout/retries" in problem for problem in subject.load_problems)


def test_an_unreadable_group_is_a_reported_problem_not_an_empty_group(profile, monkeypatch):
    """ "There is nothing here" is a different claim from "I could not look". Returning an empty
    listing for a directory that raised would report a profile as whole while part of it was
    unreadable. Monkeypatched rather than chmod-ed: root ignores the mode bits."""
    _group_file(profile, "checkout", "orders-outage")
    real = store.Path.iterdir

    def refuse(self):
        if self.name == "checkout":
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(self)

    monkeypatch.setattr(store.Path, "iterdir", refuse)

    subject = make_store(profile)

    assert not any(name.startswith("checkout/") for name in subject.scenarios)
    assert any("cannot read checkout/" in problem for problem in subject.load_problems)


def test_an_unreadable_scenarios_directory_is_reported_rather_than_read_as_empty(profile, monkeypatch):
    real = store.Path.iterdir

    def refuse(self):
        if self.name == "scenarios":
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(self)

    monkeypatch.setattr(store.Path, "iterdir", refuse)

    files, problems = store.scenario_files()

    assert files == []
    assert problems and "cannot read" in problems[0][1]


@pytest.mark.parametrize("target", ["same-group", "other-group", "root", "outside"])
def test_a_grouped_file_is_held_to_the_containment_scenario_path_applies(profile, target, tmp_path):
    """One identity, one verdict. Checking the file against its own parent would only prove it sits
    in the directory it sits in; the rule that matters is where that directory is. A grouped file
    linked out of the profile used to load while `scenario_path` refused the very same name."""
    _group_file(profile, "checkout", "real")
    _group_file(profile, "archive", "elsewhere")
    _write(profile, "root", {"overrides": []})
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"overrides": []}))
    destinations = {
        "same-group": profile / "scenarios" / "checkout" / "real.json",
        "other-group": profile / "scenarios" / "archive" / "elsewhere.json",
        "root": profile / "scenarios" / "root.json",
        "outside": outside,
    }
    link = profile / "scenarios" / "checkout" / "link.json"
    link.symlink_to(destinations[target])

    make_store(profile)
    scenario, problems = store.load_scenario_file(link)
    refused_by_path = False
    try:
        store.scenario_path("checkout/link")
    except store.UnsafeName:
        refused_by_path = True

    assert (scenario is None) == refused_by_path, "loading and naming must agree about one identity"
    if refused_by_path:
        assert problems and "checkout/link.json" in problems[0]


# MARK: - Reloading


def test_reload_picks_up_a_hand_added_group(profile):
    subject = make_store(profile)
    _group_file(profile, "checkout", "orders-outage")

    result = subject.reload_scenarios()

    assert "checkout/orders-outage" in subject.scenarios
    assert result["active"] == "default"


@pytest.mark.parametrize("breakage", ["malformed", "dropped-rule", "over-nested"])
def test_reload_keeps_the_old_snapshot_on_any_problem(profile, breakage):
    """All or nothing. A reload that silently dropped the one file somebody had just edited would be
    indistinguishable from one that worked, and the proxy would answer from a profile nobody has."""
    _scenario(profile, "keeper")
    subject = make_store(profile)
    subject.set_active("keeper")
    before = dict(subject.scenarios)

    if breakage == "malformed":
        _write(profile, "broken", "{not json")
    elif breakage == "dropped-rule":
        _write(profile, "partial", {"overrides": [{"id": "bad", "mode": "nonsense"}]})
    else:
        nested = profile / "scenarios" / "checkout" / "retries"
        nested.mkdir(parents=True)
        (nested / "slow.json").write_text(json.dumps({"overrides": []}))

    with pytest.raises(store.ReloadRefused) as refusal:
        subject.reload_scenarios()

    assert refusal.value.problems
    assert subject.scenarios.keys() == before.keys()
    assert subject.active_name == "keeper"


def test_reload_refuses_when_the_active_scenario_left_the_disk(profile):
    """Falling back to `default` would answer a request to re-read the profile by quietly changing
    which scenario is live."""
    _write(profile, "orders-outage", {"overrides": []})
    subject = make_store(profile)
    subject.set_active("orders-outage")
    (profile / "scenarios" / "orders-outage.json").unlink()

    with pytest.raises(store.ReloadRefused) as refusal:
        subject.reload_scenarios()

    assert "not in this profile any more" in str(refusal.value)
    assert subject.active_name == "orders-outage"


def test_reload_refuses_when_a_real_default_file_is_gone(profile):
    """A `default.json` that was there and is now gone is a deletion, not the virtual default."""
    _write(profile, "default", {"overrides": []})
    subject = make_store(profile)
    (profile / "scenarios" / "default.json").unlink()

    with pytest.raises(store.ReloadRefused):
        subject.reload_scenarios()


def test_reload_keeps_serving_a_virtual_default_that_was_never_on_disk(profile):
    """The bundled examples ship no `default.json`, so this is the ordinary case — refusing it would
    make reload unusable in the profile `lyrebird init` writes."""
    _write(profile, "orders-outage", {"overrides": []})
    subject = make_store(profile)
    assert "default" not in subject._disk_names

    result = subject.reload_scenarios()

    assert result["active"] == "default"
    assert "default" in subject.scenarios


def test_reload_with_use_selects_the_replacement_and_writes_the_pointer(profile):
    """The hand-move a reload exists for: the file went into a group, so the name it had is gone and
    `--use` is how the operator says which scenario replaced it."""
    _write(profile, "orders-outage", {"overrides": []})
    subject = make_store(profile)
    subject.set_active("orders-outage")
    (profile / "scenarios" / "checkout").mkdir()
    (profile / "scenarios" / "orders-outage.json").rename(profile / "scenarios" / "checkout" / "orders-outage.json")

    result = subject.reload_scenarios(use="checkout/orders-outage")

    assert result["active"] == "checkout/orders-outage"


def test_reload_invalidates_prior_run_evidence(profile):
    """The scenarios are new objects with no runtime slots, so a slot captured before the reload
    credits nobody — the same rule `reset_runtime` relies on."""
    _scenario(profile, "default", {"id": "rule", "mode": "replace", "status": 200})
    subject = make_store(profile)
    slot = store._rule_runtime(subject.active_scenario(), "rule")
    before = slot["runId"]

    store.credit(slot)
    assert slot["answers"] == 1, "the slot is live before the reload"

    subject.reload_scenarios()

    after = store._rule_runtime(subject.active_scenario(), "rule")
    assert after["runId"] != before, "a fresh scenario carries a fresh run"
    store.credit(slot)
    # `credit` counts into `answers`; asserting on `serves` passed whatever the reload did.
    assert after["answers"] == 0, "a slot captured before the reload credits nobody"
    assert slot["answers"] == 2, "the orphaned slot is mutated, and nothing can reach it"


def test_a_directory_named_like_a_scenario_file_is_reported_against_that_name(profile):
    """`default.json/` is a valid *group* name to `safe_component`, so it was scanned for scenarios
    inside and an empty one produced no problem at all — while `up --use default` served the
    synthesised default and called it whole."""
    (profile / "scenarios" / "default.json").mkdir()

    subject = make_store(profile)

    assert subject.scenarios_not_whole.get("default"), "the name `up --use` would ask about"
    assert any("a directory, not a scenario file" in p for p in subject.load_problems)


def test_a_dangling_symlink_in_scenarios_is_reported_rather_than_vanishing(profile):
    """It is neither a file nor a directory, so it matched no branch and disappeared — the one shape
    where "there is nothing here" and "I could not follow it" looked identical."""
    (profile / "scenarios" / "checkout").symlink_to(profile / "scenarios" / "nowhere")

    subject = make_store(profile)

    assert any("symlink pointing at nothing" in problem for problem in subject.load_problems)
