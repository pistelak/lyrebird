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


def test_binary_junk_in_the_sessions_directory_does_not_block_startup(profile):
    """Regression: `read_text` raises UnicodeDecodeError, which is a ValueError but neither an
    OSError nor a JSONDecodeError — so a file of binary junk escaped every arm of the loader and
    took the proxy down at startup, from the one directory operators are told to hand-edit."""
    (profile / "sessions" / "junk.json").write_bytes(b"\xff\xfe\x00binary")
    subject = make_store(profile)
    assert subject.active_name == "default"
    assert any("junk.json" in problem for problem in subject.load_problems)


def test_an_unsupported_schema_version_is_skipped_and_named(profile):
    (profile / "sessions" / "future.json").write_text(
        json.dumps({"schemaVersion": 2, "name": "future", "overrides": []}))
    subject = make_store(profile)
    assert "future" not in subject.sessions, "a session this engine cannot read must not load"
    assert any("future.json" in problem and "schemaVersion" in problem
               for problem in subject.load_problems)


# MARK: - The shared session loader
#
# `load_session_file` is what startup loads with, so an offline inspection command reports what the
# proxy would do rather than a second opinion about it. These tests pin that it is the same answer.

def _write(profile, name, payload):
    path = profile / "sessions" / f"{name}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def test_load_session_file_keeps_the_good_rules_and_names_the_dropped_ones(profile):
    path = _write(profile, "partial", {
        "name": "partial",
        "overrides": [
            {"id": "keep", "mode": "replace", "status": 200, "match": {"path": "/a"}},
            {"id": "typo", "mode": "replace", "status": 200, "match": {"paths": "/b"}},
            {"id": "keep", "mode": "replace", "status": 204, "match": {"path": "/c"}},
        ],
    })
    session, problems = store.load_session_file(path)
    assert [o["id"] for o in session["overrides"]] == ["keep"]
    assert any("override[1]" in p and "paths" in p for p in problems), "name the rule and the field"
    assert any("override[2]" in p and "duplicate id" in p for p in problems)
    assert all(p.startswith("partial.json:") for p in problems), "every problem names its file"


def test_load_session_file_reports_nothing_kept_as_a_none_session(profile):
    """None and an empty session are different answers: one says the scenario is not loaded, the
    other says it loaded and has no rules in it."""
    session, problems = store.load_session_file(_write(profile, "broken", "{not json"))
    assert session is None
    assert problems and problems[0].startswith("skipped broken.json:")


def test_load_session_file_refuses_a_file_whose_name_could_escape_the_profile(profile):
    """The stem becomes a session name, and a session name becomes a path component."""
    path = profile / "sessions" / "..json"
    path.write_text("{}")
    session, problems = store.load_session_file(path)
    assert session is None
    assert "invalid session name" in problems[0]


def test_load_session_file_creates_nothing(profile):
    """An offline inspection must leave the profile exactly as it found it — no directories, no
    synthesised `default`, no active-session pointer."""
    path = _write(profile, "s", {"name": "s", "overrides": []})
    before = sorted(p.name for p in (profile / "sessions").iterdir())
    store.load_session_file(path)
    store.load_session_file(profile / "sessions" / "absent.json")
    assert sorted(p.name for p in (profile / "sessions").iterdir()) == before
    assert not config.STATE_FILE.exists()


def test_startup_reports_exactly_what_the_shared_loader_reports(profile):
    """The point of the extraction: an offline verdict that could differ from startup's would be a
    second opinion, and the operator would have no way to know which one the proxy acts on."""
    files = [
        _write(profile, "good", {"name": "good", "overrides": []}),
        _write(profile, "broken", "{not json"),
        _write(profile, "partial", {"name": "partial", "overrides": [{"mode": "nonsense"}]}),
        _write(profile, "future", {"schemaVersion": 7, "name": "future", "overrides": []}),
    ]
    offline = [problem for file in sorted(files) for problem in store.load_session_file(file)[1]]
    assert make_store(profile).load_problems == offline


def test_session_path_refuses_a_name_that_would_escape_the_sessions_directory(profile):
    with pytest.raises(store.UnsafeName):
        store.session_path("../../etc/passwd")


def test_a_session_file_symlinked_out_of_the_profile_is_not_loaded(profile, tmp_path):
    """Startup reaches its files through a glob, so nothing used to check them: a session symlinked
    out of the profile loaded into the proxy while `session_path` refused that same session by
    name — one file, two verdicts, and the permissive one was the one that ran."""
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "outside", "overrides": []}))
    link = profile / "sessions" / "sneaky.json"
    link.symlink_to(outside)

    session, problems = store.load_session_file(link)
    assert session is None
    assert "escapes" in problems[0]
    assert "sneaky" not in make_store(profile).sessions, "and startup refuses it for the same reason"


def test_the_loader_holds_a_file_to_the_same_containment_as_writing_it(profile, tmp_path):
    """The rule is `session_path`'s, exactly: resolve inside `sessions/`. A link that leaves it,
    even into the same profile, was already unwritable — `override add` on such a session raises
    `UnsafeName` from `_write_session` — so loading it left a session the proxy would serve and
    could never save."""
    elsewhere = profile / "shared.json"
    elsewhere.write_text(json.dumps({"name": "shared", "overrides": []}))
    link = profile / "sessions" / "kept.json"
    link.symlink_to(elsewhere)
    with pytest.raises(store.UnsafeName):
        store.session_path("kept")
    session, problems = store.load_session_file(link)
    assert session is None and "escapes" in problems[0]


def test_a_session_file_that_points_at_itself_does_not_stop_the_proxy_starting(profile):
    """`Path.resolve()` raises `RuntimeError("Symlink loop from …")`, which is not an OSError. The
    containment check runs before the read, so an uncaught one is a single self-referencing file in
    a hand-edited directory stopping the proxy from starting at all — where before the check
    existed, `read_text` raised OSError and the file was simply skipped."""
    loop = profile / "sessions" / "loop.json"
    loop.symlink_to(loop)
    (profile / "sessions" / "good.json").write_text(json.dumps({"name": "good", "overrides": []}))

    session, problems = store.load_session_file(loop)
    assert session is None
    assert "cannot resolve path" in problems[0] and "loop.json" in problems[0]

    subject = make_store(profile)
    assert "good" in subject.sessions, "one unresolvable file must not cost the others"
    assert any("loop.json" in problem for problem in subject.load_problems)


def test_a_rule_whose_delay_is_not_a_finite_number_is_a_reported_problem(profile):
    """`json.loads` turns `1e309` into `inf`, and `int(inf)` raises OverflowError — not a
    ValidationError, and not even a ValueError — from inside validation. The loader's `except
    ValidationError` never saw it, so one such rule took the whole file's diagnostics with it."""
    path = _write(profile, "wild", {"name": "wild", "overrides": [
        {"id": "ovr_slow", "mode": "replace", "status": 200, "delayMs": 1e309},
        {"id": "ovr_ok", "mode": "replace", "status": 200, "match": {"path": "/a"}},
    ]})
    session, problems = store.load_session_file(path)
    assert [o["id"] for o in session["overrides"]] == ["ovr_ok"], "the good rule still loads"
    assert any("override[0]" in p and "finite" in p for p in problems)


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


def test_create_refuses_a_name_that_is_already_taken(profile):
    """Silently replacing a session someone else may be using is a delete without a `delete`."""
    subject = make_store(profile)
    subject.create_session("taken")
    subject.set_active("taken")
    kept = subject.add_override({"id": "keep", "mode": "replace", "match": {"path": "/a"}})
    with pytest.raises(FileExistsError):
        subject.create_session("taken")
    assert [o["id"] for o in subject.sessions["taken"]["overrides"]] == [kept["id"]], \
        "the refused create must not have touched the existing session"


@pytest.mark.parametrize("overrides", [
    pytest.param({"keep": {"mode": "replace"}}, id="object"),
    pytest.param(None, id="null"),
    pytest.param("[]", id="string"),
])
def test_a_session_whose_overrides_are_not_a_list_is_reported_not_emptied(profile, overrides):
    """`normalise_session` substitutes [] for a malformed `overrides`, so the file loads — and the
    problem has to be on the record, or a scenario with every rule lost reads as one with none."""
    (profile / "sessions" / "broken.json").write_text(json.dumps(
        {"name": "broken", "overrides": overrides}), encoding="utf-8")
    session, problems = store.load_session_file(profile / "sessions" / "broken.json")
    assert session is not None and session["overrides"] == []
    assert problems == ["broken.json: overrides must be a list"]


def test_a_created_session_does_not_inherit_the_problems_of_the_file_it_replaces(profile):
    """`sessions_not_whole` says what is wrong with the session under a name *now*. A file that
    would not load leaves no session, so creating one under that name is a recovery — and a stale
    entry makes `up --use NAME` refuse to launch against rules that are all present."""
    (profile / "sessions" / "orders-outage.json").write_text("{ not json", encoding="utf-8")
    subject = make_store(profile)
    assert "orders-outage" in subject.sessions_not_whole

    subject.create_session("orders-outage")

    assert subject.sessions_not_whole == {}
    assert subject.load_problems, "the record of what startup found is not rewritten"


def test_a_recreated_session_does_not_inherit_the_problems_of_the_one_deleted(profile):
    """The other order: delete the half-loaded session, then make a new one under its name. The
    entry has to go with the session, not linger for whatever takes the name next."""
    (profile / "sessions" / "orders-outage.json").write_text(json.dumps({
        "name": "orders-outage",
        "overrides": [{"match": {"path": "/a"}, "mode": "replace", "status": 200},
                      {"match": {"path": "/b"}, "mode": "replace", "statsu": 200}],
    }), encoding="utf-8")
    subject = make_store(profile)
    assert "orders-outage" in subject.sessions, "it loaded, without one of its rules"
    assert "orders-outage" in subject.sessions_not_whole

    assert subject.delete_session("orders-outage")
    assert subject.sessions_not_whole == {}
    subject.create_session("orders-outage")
    assert subject.sessions_not_whole == {}


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
    assert subject.answer_states() == [
        {"id": "shared", "active": True, "count": 0, "runId": None}]


def test_a_captured_slot_credits_nobody_after_the_rule_is_replaced(profile):
    """Same shape, one session: `override add` on an existing id installs a different rule, and the
    in-flight answer belongs to the definition that was consulted, not the one that replaced it."""
    subject = store.Store()
    subject.add_override({"id": "r", "mode": "patch", "patch": {}})
    slot = subject.answer_slot("r")
    subject.add_override({"id": "r", "mode": "replace", "status": 200})
    store.credit(slot)
    assert subject.answer_states() == [{"id": "r", "active": True, "count": 0, "runId": None}]


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
    assert subject.answer_states() == [{"id": "a", "active": True, "count": 0, "runId": None}]


def test_reset_clears_one_rules_answer_count_and_leaves_the_others(profile):
    subject = store.Store()
    subject.add_override({"id": "a", "mode": "replace", "status": 200})
    subject.add_override({"id": "b", "mode": "replace", "status": 200})
    store.credit(subject.answer_slot("a"))
    store.credit(subject.answer_slot("b"))
    subject.reset_runtime("a")
    assert [(s["id"], s["count"]) for s in subject.answer_states()] == [("a", 0), ("b", 1)]


def test_answer_states_report_an_inactive_rule_as_inactive(profile):
    """A rule that is switched off can never answer, so a wait on it should fail at once rather
    than burn its timeout."""
    subject = store.Store()
    subject.add_override({"id": "off", "active": False, "mode": "replace", "status": 200})
    assert subject.answer_states() == [
        {"id": "off", "active": False, "count": 0, "runId": None}]


# MARK: - Which run the answers belong to
#
# A count on its own says "some run's". Every test here is a way another run's evidence could be
# handed to a caller asking about the boundary it drew — the same shape as a stale sequence event
# satisfying a wait, one step further out: the rule id survives everything that ends a run.

def test_reset_issues_the_run_id_the_following_answers_are_counted_under(profile):
    """The whole flow in one place: reset hands back a token, and what the rule answers afterwards
    is reported under exactly that token. Without this, a caller has nothing to compare against."""
    subject = store.Store()
    subject.add_override({"id": "a", "mode": "replace", "status": 200})
    issued = subject.reset_runtime("a")["reset"]["a"]
    store.credit(subject.answer_slot("a"))
    assert subject.answer_states() == [{"id": "a", "active": True, "count": 1, "runId": issued}]


def test_a_reset_leaves_the_previous_run_id_unclaimable(profile):
    """A count is only ever evidence about the run it was taken in, so the run that produced it has
    to be nameable — and a reset has to make the previous name stop matching."""
    subject = store.Store()
    subject.add_override({"id": "a", "mode": "replace", "status": 200})
    store.credit(subject.answer_slot("a"))
    before = subject.answer_states()[0]["runId"]
    subject.reset_runtime("a")
    store.credit(subject.answer_slot("a"))
    after = subject.answer_states()[0]
    assert after["count"] == 1, "the new run has its own answer"
    assert after["runId"] != before, "and cannot be mistaken for the run before it"


def test_a_rule_replaced_under_the_same_id_answers_in_a_different_run(profile):
    """Rule ids are reused — `override add` on an existing id is the documented way to change a
    rule. The id therefore cannot carry the identity a caller holds; only the run token can."""
    subject = store.Store()
    subject.add_override({"id": "a", "mode": "replace", "status": 200})
    issued = subject.reset_runtime("a")["reset"]["a"]
    subject.add_override({"id": "a", "mode": "replace", "status": 500})
    store.credit(subject.answer_slot("a"))
    state = subject.answer_states()[0]
    assert state["count"] == 1, "the new definition really did answer"
    assert state["runId"] != issued, "but not in the run the caller was told about"


def test_a_session_switch_answers_in_a_different_run_under_the_same_id(profile):
    """Two sessions can file a rule under one id. Reading a count from the second while holding the
    first's run token is the substitution this field exists to make visible."""
    subject = store.Store()
    subject.add_override({"id": "shared", "mode": "replace", "status": 200})
    issued = subject.reset_runtime("shared")["reset"]["shared"]
    subject.create_session("other")
    subject.set_active("other")
    subject.add_override({"id": "shared", "mode": "replace", "status": 200})
    store.credit(subject.answer_slot("shared"))
    assert subject.answer_states()[0]["runId"] != issued


def test_a_rule_that_has_no_run_reports_none_rather_than_a_run_with_no_answers(profile):
    """`null` says "there is no run here"; a count of zero says "there was a run and nothing
    answered". Collapsing the first into the second is how a caller believes a boundary it never
    drew — the reading-does-not-mint rule is what makes the distinction possible."""
    subject = store.Store()
    subject.add_override({"id": "untouched", "mode": "replace", "status": 200})
    assert subject.answer_states() == [
        {"id": "untouched", "active": True, "count": 0, "runId": None}]


def test_answer_and_sequence_states_report_one_run_not_two(profile):
    """One identity per rule, reported by both views. Two tokens for the same boundary would let a
    caller bind a wait and an assertion to different things and never find out."""
    subject = store.Store()
    subject.add_override(dict(SEQ))
    subject.reset_runtime("seq")
    answers = {state["id"]: state["runId"] for state in subject.answer_states()}
    assert answers["seq"] == subject.sequence_states()[0]["runId"]


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
    assert subject.answer_states() == [{"id": "r", "active": True, "count": 2, "runId": run_id}],\
        "and the run the caller was told about is still the one being counted"


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
