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


# MARK: - Sessions

def test_deleting_the_active_session_switches_to_default(profile):
    subject = make_store(profile)
    subject.create_session("scratch")
    subject.set_active("scratch")
    assert subject.delete_session("scratch") is True
    assert subject.active_name == "default"


def test_default_session_cannot_be_deleted(profile):
    subject = make_store(profile)
    assert subject.delete_session("default") is False


def test_a_malformed_session_file_does_not_block_startup(profile):
    """One bad file used to raise AttributeError and stop the proxy from starting at all."""
    (profile / "sessions" / "bad.json").write_text("[]")
    (profile / "sessions" / "good.json").write_text(json.dumps({"name": "good", "overrides": []}))
    subject = make_store(profile)
    assert "good" in subject.sessions
    assert any("bad.json" in problem for problem in subject.load_problems)


def test_unparseable_json_does_not_block_startup(profile):
    (profile / "sessions" / "broken.json").write_text("{not json")
    subject = make_store(profile)
    assert subject.active_name == "default"
    assert any("broken.json" in problem for problem in subject.load_problems)


def test_startup_does_not_rewrite_existing_session_files(profile):
    """A profile kept in git must not go dirty just because the proxy started."""
    path = profile / "sessions" / "kept.json"
    original = json.dumps({"name": "kept", "overrides": []})  # deliberately not indent=2
    path.write_text(original)
    make_store(profile)
    assert path.read_text() == original


# MARK: - Overrides

def test_add_override_tolerates_a_session_whose_overrides_lack_ids(profile):
    (profile / "sessions" / "hand.json").write_text(
        json.dumps({"name": "hand", "overrides": [{"match": {"path": "/a"}, "mode": "replace"}]}))
    subject = make_store(profile)
    subject.set_active("hand")
    added = subject.add_override({"match": {"path": "/b"}, "mode": "replace"})
    assert added["id"].startswith("ovr_")


def test_add_override_tolerates_a_session_with_no_overrides_key(profile):
    (profile / "sessions" / "bare.json").write_text(json.dumps({"name": "bare"}))
    subject = make_store(profile)
    subject.set_active("bare")
    assert subject.add_override({"match": {"path": "/b"}, "mode": "replace"})["id"]


def test_add_override_rejects_an_invalid_rule(profile):
    subject = make_store(profile)
    with pytest.raises(ValueError):
        subject.add_override({"mode": "replace", "delayMs": "1s"})


def test_persisted_sessions_are_private(profile):
    subject = make_store(profile)
    subject.create_session("scratch")
    mode = (profile / "sessions" / "scratch.json").stat().st_mode
    assert mode & 0o077 == 0


def test_containment_rejects_a_symlinked_sessions_directory(profile, tmp_path):
    """Proving a path sits under sessions/ is not enough if sessions/ is itself a symlink out."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sessions = profile / "sessions"
    for child in sessions.iterdir():
        child.unlink()
    sessions.rmdir()
    sessions.symlink_to(outside, target_is_directory=True)
    with pytest.raises(store.UnsafeName):
        store._contained(config.SESSIONS_DIR, "escaped.json")


def test_override_id_cannot_be_nulled_by_the_payload(profile):
    """A payload id of null used to overwrite the generated fallback (the spread came last), so the
    override persisted with a null id and was silently dropped on the next startup."""
    subject = make_store(profile)

    # null reads as "no id given" and must produce a generated one — it used to survive as null.
    nulled = subject.add_override({"id": None, "mode": "replace", "match": {"path": "/a"}})
    assert nulled["id"].startswith("ovr_")

    # An explicit but unusable id is a mistake worth reporting, not silently replacing.
    for bad in ("", "   ", 123, []):
        with pytest.raises(ValueError):
            subject.add_override({"id": bad, "mode": "replace", "match": {"path": "/a"}})

    generated = subject.add_override({"mode": "replace", "match": {"path": "/a"}})
    assert generated["id"].startswith("ovr_")
    kept = subject.add_override({"id": "chosen", "mode": "replace", "match": {"path": "/b"}})
    assert kept["id"] == "chosen"


def test_import_session_normalises_and_persists(profile):
    """The only path that merges an outside payload into a session."""
    subject = make_store(profile)
    name = subject.import_session({"session": {
        "name": "imported",
        "overrides": [{"id": "keep", "mode": "replace", "match": {"path": "/a"}}],
    }})
    assert name == "imported"
    assert [o["id"] for o in subject.sessions["imported"]["overrides"]] == ["keep"]
    saved = json.loads((profile / "sessions" / "imported.json").read_text())
    assert "_problems" not in saved


@pytest.mark.parametrize("overrides", [
    pytest.param([{"id": "keep", "mode": "replace"}, {"id": "drop", "mode": "nonsense"}],
                 id="invalid-override"),
    pytest.param([{"id": "dup", "mode": "replace", "match": {"path": "/a"}},
                  {"id": "dup", "mode": "replace", "match": {"path": "/b"}}], id="duplicate-id"),
])
def test_import_refuses_a_payload_it_cannot_keep_whole(profile, overrides):
    """A file on disk is reported-and-dropped: it is in front of you, and the proxy must still
    start. An import is an API call, and answering "imported" to a payload whose second override was
    discarded tells the caller their rule is installed when it is not."""
    subject = make_store(profile)
    with pytest.raises(rules.ValidationError):
        subject.import_session({"session": {"name": "imported", "overrides": overrides}})
    assert "imported" not in subject.sessions, "nothing may be persisted from a refused import"
    assert not (profile / "sessions" / "imported.json").exists()


@pytest.mark.parametrize("overrides", [
    pytest.param({"keep": {"mode": "replace"}}, id="object"),
    pytest.param(None, id="null"),
    pytest.param("[]", id="string"),
])
def test_import_refuses_overrides_that_are_not_a_list(profile, overrides):
    """Substituting [] for a malformed `overrides` would persist an empty session and answer
    "imported" — success reported for a payload none of whose rules were kept."""
    subject = make_store(profile)
    with pytest.raises(rules.ValidationError, match="overrides must be a list"):
        subject.import_session({"session": {"name": "imported", "overrides": overrides}})
    assert "imported" not in subject.sessions
    assert not (profile / "sessions" / "imported.json").exists()


def test_import_refuses_a_name_that_is_already_taken(profile):
    """The same refusal `create_session` makes — silently replacing a session someone else may be
    using is a delete without a `delete`."""
    subject = make_store(profile)
    subject.create_session("taken")
    subject.set_active("taken")
    kept = subject.add_override({"id": "keep", "mode": "replace", "match": {"path": "/a"}})
    with pytest.raises(FileExistsError):
        subject.import_session({"session": {"name": "taken", "overrides": []}})
    assert [o["id"] for o in subject.sessions["taken"]["overrides"]] == [kept["id"]], \
        "the refused import must not have touched the existing session"


def test_import_session_rejects_an_unsafe_name(profile):
    subject = make_store(profile)
    assert subject.import_session({"name": "../escape", "overrides": []}) is None


def test_import_session_rejects_a_non_object(profile):
    subject = make_store(profile)
    assert subject.import_session({"session": []}) is None


def test_cloning_an_unknown_session_is_an_error(profile):
    """Handing back an empty session instead is a false success the caller cannot see."""
    subject = make_store(profile)
    with pytest.raises(KeyError):
        subject.create_session("copy", clone_from="does-not-exist")
    assert "copy" not in subject.sessions


def test_cloning_copies_the_overrides(profile):
    subject = make_store(profile)
    subject.add_override({"mode": "replace", "match": {"path": "/a"}})
    subject.create_session("copy", clone_from="default")
    assert len(subject.sessions["copy"]["overrides"]) == 1
    assert subject.sessions["copy"]["name"] == "copy"


def test_starting_does_not_write_into_the_profile(profile):
    """A profile kept in git must not go dirty just because the proxy started. The in-memory
    `default` session used to be persisted, which created a file on first load."""
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    before = sorted(p.name for p in (profile / "sessions").iterdir())
    subject = store.Store()
    after = sorted(p.name for p in (profile / "sessions").iterdir())
    assert before == after, "startup wrote a file into the profile"
    assert subject.active_name == "default"
    assert "default" in subject.sessions, "default must still exist in memory"


# MARK: - Sequence cursors
#
# The cursor is runtime state living on the session under a leading underscore, so the invariants
# worth pinning are: it moves when it should, it does NOT move when it should not, and it never
# escapes into a profile someone keeps in git.

SEQ = {"id": "seq", "mode": "replace", "match": {"method": "GET", "path": "/api/items"},
       "sequence": {"steps": [{"status": 201}, {"status": 202}]}}


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
    subject = store.Store()
    subject.add_override(dict(SEQ))
    assert subject.sequence_states()[0]["nextStep"] == 1
    _advanced_once(subject)
    assert subject.sequence_states()[0]["nextStep"] == 2


def test_a_shadowed_sequenced_rule_does_not_advance(profile):
    """`find_override` returns only the most specific match, so a rule whose matcher fits may still
    not be the rule that answered. Advancing it anyway would spend a step it never served — and the
    next request it *does* answer would serve the wrong one, leaving the scenario off by one for the
    rest of its run. This is why `self` means 'answered' and not 'matched'."""
    subject = store.Store()
    subject.add_override({"id": "seq", "mode": "replace", "match": {"path": "/api/orders/*"},
                          "sequence": {"steps": [{"status": 201}, {"status": 202}]}})
    subject.add_override({"id": "specific", "mode": "replace", "status": 404,
                          "match": {"path": "/api/orders/42"}})

    picked = subject.find_override("GET", "/api/orders/42", {}, "")
    assert picked["id"] == "specific", "the rule with fewer wildcards answers"

    subject.bump_selected(picked)
    subject.advance_matching("GET", "/api/orders/42", {}, "")

    state = next(s for s in subject.sequence_states() if s["id"] == "seq")
    assert state["nextStep"] == 1, "the shadowed sequence must not have moved"


def test_an_advance_on_sequence_ignores_its_own_calls(profile):
    """The property the delete-then-refresh scenario depends on: a screen may fetch the list any
    number of times without consuming a step."""
    subject = store.Store()
    subject.add_override({**SEQ, "sequence": {**SEQ["sequence"],
                                              "advanceOn": {"method": "DELETE", "path": "/api/items/*"}}})
    for _ in range(3):
        _advanced_once(subject)
    assert subject.sequence_states()[0]["nextStep"] == 1

    assert subject.advance_matching("DELETE", "/api/items/b", {}, "") == ["seq"]
    assert subject.sequence_states()[0]["nextStep"] == 2


def test_advance_matching_ignores_a_rule_whose_matcher_does_not_fit(profile):
    subject = store.Store()
    subject.add_override({**SEQ, "sequence": {**SEQ["sequence"],
                                              "advanceOn": {"method": "DELETE", "path": "/api/items/*"}}})
    assert subject.advance_matching("DELETE", "/api/other/b", {}, "") == []


def test_an_inactive_sequenced_rule_is_never_advanced(profile):
    """A disabled rule that still moved on the wire would be a rule doing something while off."""
    subject = store.Store()
    subject.add_override({**SEQ, "active": False,
                          "sequence": {**SEQ["sequence"],
                                       "advanceOn": {"method": "DELETE", "path": "/api/items/*"}}})
    assert subject.sequenced_overrides() == []
    assert subject.advance_matching("DELETE", "/api/items/b", {}, "") == []


def test_exhausted_and_overrun_are_reported_separately(profile):
    """The clamp at n+1 exists for this: at n, a second and a third advance event land on the same
    value and the two flags collapse into one."""
    subject = store.Store()
    subject.add_override(dict(SEQ))

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
    subject = store.Store()
    subject.add_override(dict(SEQ))
    override = subject.find_override("GET", "/api/items", {}, "")
    for _ in range(3):
        action, view, progress = subject.resolve_override(override)
        assert (action, view["status"], progress["selectedStep"]) == (rules.APPLY, 201, 1)


# MARK: - Runtime never escapes

def test_sequence_cursors_never_reach_the_session_file(profile):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    subject.add_override({"id": "other", "mode": "replace", "status": 200})  # forces another write

    raw = (profile / "sessions" / "default.json").read_text(encoding="utf-8")
    assert "_ruleRuntime" not in raw
    assert "runId" not in raw


def test_a_clone_starts_its_sequences_fresh(profile):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    subject.create_session("copy", clone_from="default")
    assert "_ruleRuntime" not in subject.sessions["copy"]


def test_switching_sessions_restarts_the_scenario(profile):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    subject.create_session("other")
    subject.set_active("other")
    subject.set_active("default")
    assert subject.sequence_states()[0]["nextStep"] == 1, "a scenario always begins at its first step"


def test_deleting_the_active_session_resets_the_destination(profile):
    """`delete_session` falls back to `default` without going through `set_active`, so cursor
    cleanup hung off `set_active` alone would let a scenario resume mid-run."""
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    subject.create_session("work")
    subject.set_active("work")
    subject.delete_session("work")

    assert subject.active_name == "default"
    assert subject.sequence_states()[0]["nextStep"] == 1


def test_replacing_a_rule_by_id_drops_its_cursor(profile):
    """The rule at that id is now a different rule; its old cursor describes steps that may not
    exist any more."""
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    subject.add_override({**SEQ, "sequence": {"steps": [{"status": 500}]}})
    assert subject.sequence_states()[0]["nextStep"] == 1


def test_removing_a_rule_drops_its_cursor(profile):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    assert subject.remove_override("seq") is True
    subject.add_override(dict(SEQ))
    assert subject.sequence_states()[0]["nextStep"] == 1


# MARK: - Reset

def test_reset_rewinds_and_issues_a_new_run_id(profile):
    subject = store.Store()
    subject.add_override(dict(SEQ))
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
    subject = store.Store()
    subject.add_override(dict(SEQ))
    assert subject.reset_runtime("nope") is None
    assert subject.reset_runtime()["reset"] != {}


def test_reset_covers_plain_rules_not_just_sequenced_ones(profile):
    """Every rule carries run state now — an answer count — so a reset that skipped plain rules
    would leave the one boundary a test can draw unavailable to exactly the rules that need it."""
    subject = store.Store()
    subject.add_override({"id": "plain", "mode": "replace", "status": 200})
    assert list((subject.reset_runtime() or {})["reset"]) == ["plain"]


def test_resetting_a_session_with_no_rules_reports_nothing_to_do(profile):
    subject = store.Store()
    assert subject.reset_runtime() == {"session": "default", "reset": {}}


def test_a_served_overrun_is_reported_even_when_the_cursor_cannot_move(profile):
    """A rule with an explicit `advanceOn` does not move when it answers, so it can serve the
    exhausted response repeatedly with the cursor sitting still. Deriving `hasOverrun` from the
    cursor alone therefore reported false while /recent recorded the overrun — two answers to the
    same question."""
    subject = store.Store()
    subject.add_override({**SEQ, "sequence": {**SEQ["sequence"],
                                              "advanceOn": {"method": "DELETE", "path": "/api/items/*"}}})
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
    subject = store.Store()
    subject.add_override({**SEQ, "sequence": {**SEQ["sequence"],
                                              "advanceOn": {"method": "DELETE", "path": "/api/items/*"}}})
    for _ in range(2):
        subject.resolve_override(subject.find_override("GET", "/api/items", {}, ""))
    state = subject.sequence_states()[0]
    assert state["serves"] == {"1": 2}
    assert state["nextStep"] == 1, "served twice, moved never"


def test_an_overrun_serve_is_not_counted_as_a_step(profile):
    """Past the last step there is no step being served; the overrun has its own flags."""
    subject = store.Store()
    subject.add_override(dict(SEQ))
    for _ in range(3):
        _advanced_once(subject)
    assert subject.sequence_states()[0]["serves"] == {"1": 1, "2": 1}


def test_reset_clears_the_serve_counts(profile):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    assert subject.sequence_states()[0]["serves"] == {"1": 1}
    subject.reset_runtime()
    assert subject.sequence_states()[0]["serves"] == {}


def test_reset_clears_a_recorded_overrun(profile):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    for _ in range(3):
        _advanced_once(subject)
    assert subject.sequence_states()[0]["hasOverrun"] is True
    subject.reset_runtime()
    assert subject.sequence_states()[0]["hasOverrun"] is False


def test_reset_always_issues_a_different_run_id(profile):
    """The token is what stops a retained event from an earlier run satisfying a wait, so a reset
    reissuing the same value has to be impossible rather than merely unlikely."""
    subject = store.Store()
    subject.add_override(dict(SEQ))
    seen = {subject.sequence_states()[0]["runId"]}
    for _ in range(20):
        issued = subject.reset_runtime()["reset"]["seq"]
        assert issued not in seen
        seen.add(issued)


# MARK: - Answer counts
#
# The evidence a test asserts on. Every failure below is one where the count would have said a mock
# was in play when it was not — the exact defect the assertion exists to catch.

def test_a_captured_slot_credits_nobody_after_the_session_is_switched(profile):
    """A patch is selected in the request hook and only answers a round trip later, in the response
    hook. Looking the rule up by id at that point would credit whatever rule the *now* active
    session happens to file under that id — a different rule, in a different scenario."""
    subject = store.Store()
    subject.add_override({"id": "shared", "mode": "patch", "patch": {}})
    slot = subject.answer_slot("shared")

    subject.create_session("other")
    subject.set_active("other")
    subject.add_override({"id": "shared", "mode": "patch", "patch": {}})

    store.credit(slot)   # the in-flight patch from the previous session lands now
    assert subject.answer_states() == [{"id": "shared", "active": True, "count": 0}]


def test_a_captured_slot_credits_nobody_after_the_rule_is_replaced(profile):
    """Same shape, one session: `override add` on an existing id installs a different rule, and the
    in-flight answer belongs to the definition that was consulted, not the one that replaced it."""
    subject = store.Store()
    subject.add_override({"id": "r", "mode": "patch", "patch": {}})
    slot = subject.answer_slot("r")
    subject.add_override({"id": "r", "mode": "replace", "status": 200})
    store.credit(slot)
    assert subject.answer_states() == [{"id": "r", "active": True, "count": 0}]


def test_reading_answer_states_does_not_mint_run_state(profile):
    """Asking how many answers a rule has must not create the runtime entry that a reset issues a
    run id for — a question is not an event."""
    subject = store.Store()
    subject.add_override({"id": "untouched", "mode": "replace", "status": 200})
    subject.answer_states()
    assert store._runtime(subject.active_session()) == {}


def test_switching_session_clears_answer_counts(profile):
    subject = store.Store()
    subject.add_override({"id": "a", "mode": "replace", "status": 200})
    store.credit(subject.answer_slot("a"))
    subject.create_session("scratch")
    subject.set_active("scratch")
    subject.set_active("default")
    assert subject.answer_states() == [{"id": "a", "active": True, "count": 0}]


def test_reset_clears_one_rules_answer_count_and_leaves_the_others(profile):
    subject = store.Store()
    subject.add_override({"id": "a", "mode": "replace", "status": 200})
    subject.add_override({"id": "b", "mode": "replace", "status": 200})
    store.credit(subject.answer_slot("a"))
    store.credit(subject.answer_slot("b"))
    subject.reset_runtime("a")
    assert subject.answer_states() == [{"id": "a", "active": True, "count": 0},
                                       {"id": "b", "active": True, "count": 1}]


def test_answer_states_report_an_inactive_rule_as_inactive(profile):
    """A rule that is switched off can never answer, so a wait on it should fail at once rather
    than burn its timeout."""
    subject = store.Store()
    subject.add_override({"id": "off", "active": False, "mode": "replace", "status": 200})
    assert subject.answer_states() == [{"id": "off", "active": False, "count": 0}]


# MARK: - Write then publish
#
# Every mutator writes the file that records its change before the change becomes visible in
# memory. These tests inject a failing write and assert the three things that used to drift apart:
# the exception reaches the caller, live state is untouched, and the file is untouched. Before the
# fix each one left the proxy answering with a rule no profile contained.

def _refuse_writes(monkeypatch):
    """Make every profile write fail the way a full disk does.

    Monkeypatched rather than chmodded: a read-only directory does not stop root, which is how CI
    containers run, and chmod on a tmp_path is flaky on macOS.
    """
    def refuse(path, text):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(config, "atomic_write", refuse)


def test_a_rule_whose_write_fails_is_not_added(profile, monkeypatch):
    subject = store.Store()
    subject.add_override({"id": "kept", "mode": "replace", "status": 200})
    before = (profile / "sessions" / "default.json").read_bytes()

    _refuse_writes(monkeypatch)
    with pytest.raises(OSError):
        subject.add_override({"id": "new", "mode": "replace", "status": 500})

    assert [o["id"] for o in subject.active_overrides()] == ["kept"]
    assert (profile / "sessions" / "default.json").read_bytes() == before


def test_a_replacement_whose_write_fails_leaves_the_old_rule_live_with_its_cursor(profile, monkeypatch):
    """The cursor belongs to the rule that is still answering. Dropping it for a replacement that
    never reached the disk would rewind a scenario mid-run."""
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    before = (profile / "sessions" / "default.json").read_bytes()

    _refuse_writes(monkeypatch)
    with pytest.raises(OSError):
        subject.add_override({**SEQ, "sequence": {"steps": [{"status": 500}]}})

    state = subject.sequence_states()[0]
    assert state["stepCount"] == 2, "the two-step rule must still be the live one"
    assert state["nextStep"] == 2, "its cursor must survive a replacement that did not happen"
    assert (profile / "sessions" / "default.json").read_bytes() == before


def test_a_replacement_whose_write_fails_leaves_a_captured_slot_crediting_the_live_rule(profile, monkeypatch):
    """A captured slot is orphaned by a successful replacement, on purpose. If the replacement did
    not happen, the rule that handed the slot out is still the live one, so its answers must still
    land — otherwise a patch in flight during a failed write is silently uncounted."""
    subject = store.Store()
    subject.add_override({"id": "r", "mode": "patch", "patch": {}})
    slot = subject.answer_slot("r")
    store.credit(slot)
    run_id = slot["runId"]

    _refuse_writes(monkeypatch)
    with pytest.raises(OSError):
        subject.add_override({"id": "r", "mode": "replace", "status": 200})

    assert subject.answer_slot("r") is slot, "the live rule kept its runtime entry"
    assert slot["answers"] == 1 and slot["runId"] == run_id, "and the entry itself is untouched"
    store.credit(slot)
    assert subject.answer_states() == [{"id": "r", "active": True, "count": 2}]


def test_a_rule_whose_removal_cannot_be_written_stays_live(profile, monkeypatch):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    before = (profile / "sessions" / "default.json").read_bytes()

    _refuse_writes(monkeypatch)
    with pytest.raises(OSError):
        subject.remove_override("seq")

    assert [o["id"] for o in subject.active_overrides()] == ["seq"]
    assert subject.sequence_states()[0]["nextStep"] == 2, "the cursor belongs to a rule still live"
    assert (profile / "sessions" / "default.json").read_bytes() == before


def test_overrides_stay_live_when_the_clear_cannot_be_written(profile, monkeypatch):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    before = (profile / "sessions" / "default.json").read_bytes()

    _refuse_writes(monkeypatch)
    with pytest.raises(OSError):
        subject.clear_overrides()

    assert [o["id"] for o in subject.active_overrides()] == ["seq"]
    assert subject.sequence_states()[0]["nextStep"] == 2
    assert (profile / "sessions" / "default.json").read_bytes() == before


@pytest.mark.parametrize("clone_from", [None, "default"], ids=["empty", "clone"])
def test_a_session_whose_write_fails_does_not_exist(profile, monkeypatch, clone_from):
    subject = store.Store()

    _refuse_writes(monkeypatch)
    with pytest.raises(OSError):
        subject.create_session("scratch", clone_from)

    assert "scratch" not in subject.sessions
    assert not (profile / "sessions" / "scratch.json").exists()


def test_an_import_whose_write_fails_does_not_exist(profile, monkeypatch):
    subject = store.Store()

    _refuse_writes(monkeypatch)
    with pytest.raises(OSError):
        subject.import_session({"session": {
            "name": "imported",
            "overrides": [{"id": "keep", "mode": "replace", "match": {"path": "/a"}}],
        }})

    assert "imported" not in subject.sessions
    assert not (profile / "sessions" / "imported.json").exists()


def test_a_switch_whose_pointer_write_fails_does_not_happen(profile, monkeypatch):
    """`_activate` rewinds the destination's cursors. Doing that for a switch the state file never
    recorded would restart a scenario that the next proxy start puts back where it was."""
    subject = store.Store()
    subject.create_session("other")
    subject.set_active("other")
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    subject.set_active("default")
    before = config.STATE_FILE.read_bytes()

    _refuse_writes(monkeypatch)
    with pytest.raises(OSError):
        subject.set_active("other")

    assert subject.active_name == "default"
    assert store._runtime(subject.sessions["other"])["seq"]["cursor"] == 1, \
        "the destination's cursors were rewound for a switch that did not happen"
    assert config.STATE_FILE.read_bytes() == before


def test_deleting_the_active_session_raises_when_the_pointer_cannot_be_written(profile, monkeypatch):
    """The fallback to `default` is a pointer write like any other. Failing it must raise rather
    than return False: False is the answer for a delete refused by policy, and an operator who
    reads it as that will never look at the disk."""
    subject = store.Store()
    subject.create_session("work")
    subject.set_active("work")
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    before_session = (profile / "sessions" / "work.json").read_bytes()
    before_state = config.STATE_FILE.read_bytes()

    _refuse_writes(monkeypatch)
    with pytest.raises(OSError):
        subject.delete_session("work")

    assert subject.active_name == "work"
    assert "work" in subject.sessions
    assert subject.sequence_states()[0]["nextStep"] == 2
    assert (profile / "sessions" / "work.json").read_bytes() == before_session
    assert config.STATE_FILE.read_bytes() == before_state
