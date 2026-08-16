"""Store persistence, path containment, and the crash paths that used to take the proxy down."""

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


def test_preset_operation_id_cannot_escape(profile):
    subject = make_store(profile)
    with pytest.raises(store.UnsafeName):
        subject.save_preset("../../..", "pwn", {})


def test_preset_read_cannot_escape(profile):
    """Containment must cover reads and listings, not only writes."""
    subject = make_store(profile)
    with pytest.raises(store.UnsafeName):
        subject.get_preset("..", "profile")
    with pytest.raises(store.UnsafeName):
        subject.list_presets("..")


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
    assert "_sequenceRuntime" not in raw
    assert "runId" not in raw


def test_a_clone_starts_its_sequences_fresh(profile):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    _advanced_once(subject)
    subject.create_session("copy", clone_from="default")
    assert "_sequenceRuntime" not in subject.sessions["copy"]


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

    result = subject.reset_sequences()
    assert list(result["reset"]) == ["seq"]
    after = subject.sequence_states()[0]
    assert after["nextStep"] == 1
    assert after["runId"] != before, "a new run so a stale event cannot satisfy a wait"


def test_resetting_an_unknown_id_is_distinguishable_from_resetting_nothing(profile):
    """None rather than an empty result: a caller that cannot tell them apart believes a typo
    worked."""
    subject = store.Store()
    subject.add_override(dict(SEQ))
    assert subject.reset_sequences("nope") is None
    assert subject.reset_sequences()["reset"] != {}


def test_resetting_a_session_with_no_sequences_reports_nothing_to_do(profile):
    subject = store.Store()
    subject.add_override({"id": "plain", "mode": "replace", "status": 200})
    assert subject.reset_sequences() == {"session": "default", "reset": {}}


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
    subject.reset_sequences()
    assert subject.sequence_states()[0]["serves"] == {}


def test_reset_clears_a_recorded_overrun(profile):
    subject = store.Store()
    subject.add_override(dict(SEQ))
    for _ in range(3):
        _advanced_once(subject)
    assert subject.sequence_states()[0]["hasOverrun"] is True
    subject.reset_sequences()
    assert subject.sequence_states()[0]["hasOverrun"] is False


def test_reset_always_issues_a_different_run_id(profile):
    """The token is what stops a retained event from an earlier run satisfying a wait, so a reset
    reissuing the same value has to be impossible rather than merely unlikely."""
    subject = store.Store()
    subject.add_override(dict(SEQ))
    seen = {subject.sequence_states()[0]["runId"]}
    for _ in range(20):
        issued = subject.reset_sequences()["reset"]["seq"]
        assert issued not in seen
        seen.add(issued)
