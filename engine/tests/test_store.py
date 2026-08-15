"""Store persistence, path containment, and the crash paths that used to take the proxy down."""

import json

import pytest

import config
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
        "overrides": [
            {"id": "keep", "mode": "replace", "match": {"path": "/a"}},
            {"id": "drop", "mode": "nonsense"},
        ],
    }})
    assert name == "imported"
    assert [o["id"] for o in subject.sessions["imported"]["overrides"]] == ["keep"]
    saved = json.loads((profile / "sessions" / "imported.json").read_text())
    assert "_problems" not in saved


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
