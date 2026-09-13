"""The journal on disk: what `read()` claims, what `write()` publishes, and what the lock refuses.

`Absent` is a claim — *there is no record* — and `up` acts on it by taking the PAC. Everything that
is there and cannot be turned into a record has to be `Unreadable` instead, because such a file may
still be describing somebody's proxy settings.
"""

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

import config
import ownership
import session
from cli_doubles import WIFI, owner, record
from ownership import Absent, Pac, Ref, SessionRecord, Unreadable


@pytest.fixture
def store(tmp_path):
    place = session.Session(tmp_path / "session")
    place.ensure_root()
    return place


RECORD = record(Ref(pid=4321, create_time=1000.5), baseline=Pac("http://proxy.example.com/corp.pac", True))


def test_the_real_per_user_root_is_never_the_one_a_test_sees(_no_real_session_root):
    """The suite patches `default_root`; this is the one test that looks at what it patched, so a
    rename of the real path cannot go unnoticed."""
    real = _no_real_session_root()
    assert real.parts[-3:] == ("Application Support", "Lyrebird", "session")
    assert real != session.default_root()


def test_read_reports_an_absent_root_as_absent(tmp_path):
    """A first-ever `down` on a clean machine: no root, no record, nothing to stop."""
    assert isinstance(session.Session(tmp_path / "nowhere").read(), Absent)


def test_read_reports_an_absent_file_as_absent(store):
    assert isinstance(store.read(), Absent)


def test_read_reports_a_dangling_symlink_as_unreadable(store):
    """A link whose target is gone raises the same `FileNotFoundError` as no entry at all, and is a
    different thing: something put it there."""
    store.journal_path.symlink_to(store.root / "gone.json")
    journal = store.read()
    assert isinstance(journal, Unreadable) and "symlink" in journal.reason


def test_read_reports_a_symlink_to_a_valid_record_as_unreadable(store):
    """A link that resolves is worse than one that dangles: followed, `down` restored from and then
    unlinked a record `up` never published at the journal path."""
    elsewhere = store.root / "elsewhere.json"
    elsewhere.write_text(json.dumps(ownership.encode(RECORD)), encoding="utf-8")
    store.journal_path.symlink_to(elsewhere)
    journal = store.read()
    assert isinstance(journal, Unreadable) and "symlink" in journal.reason
    assert elsewhere.exists(), "and the target is not touched"


def test_read_reports_a_directory_as_unreadable(store):
    store.journal_path.mkdir()
    journal = store.read()
    assert isinstance(journal, Unreadable) and "regular file" in journal.reason


def test_read_reports_an_empty_file_as_unreadable(store):
    store.journal_path.write_text("", encoding="utf-8")
    assert isinstance(store.read(), Unreadable)


def test_read_reports_an_oversized_journal_as_unreadable(store):
    """Refused on the `fstat`, before any of it is read: a hand-edited megabyte must not become a
    megabyte-long error message."""
    store.journal_path.write_text("x" * (session.JOURNAL_SIZE_CAP + 1), encoding="utf-8")
    journal = store.read()
    assert isinstance(journal, Unreadable) and "larger than" in journal.reason


@pytest.mark.parametrize("text", ["[]", '"a record"', "null", "{", "{not json}"])
def test_read_reports_a_payload_that_is_not_a_record_as_unreadable(store, text):
    store.journal_path.write_text(text, encoding="utf-8")
    assert isinstance(store.read(), Unreadable)


def test_read_reports_a_record_the_decoder_refuses_as_unreadable(store):
    payload = ownership.encode(RECORD)
    payload["proxy"] = {"pid": 0, "createTime": 1.0}
    store.journal_path.write_text(json.dumps(payload), encoding="utf-8")
    journal = store.read()
    assert isinstance(journal, Unreadable) and "pid" in journal.reason


def test_read_reports_undecodable_bytes_as_unreadable(store):
    store.journal_path.write_bytes(b"\xff\xfe not utf-8")
    assert isinstance(store.read(), Unreadable)


def test_write_then_read_round_trips(store):
    store.write(RECORD)
    assert store.read() == RECORD


def test_write_leaves_the_journal_private(store):
    store.write(RECORD)
    assert oct(store.journal_path.stat().st_mode)[-3:] == "600"


def test_write_is_readable_json(store):
    store.write(RECORD)
    payload = json.loads(store.journal_path.read_text(encoding="utf-8"))
    assert payload["owner"]["controlPort"] == owner().control_port
    assert payload["service"] == {"name": WIFI.name, "device": WIFI.device}


def test_unlink_removes_the_journal_and_tolerates_its_absence(store):
    store.write(RECORD)
    store.unlink()
    store.unlink()
    assert isinstance(store.read(), Absent)


def test_ensure_root_creates_the_root_privately(tmp_path):
    place = session.Session(tmp_path / "a" / "b" / "session")
    place.ensure_root()
    assert place.root.is_dir()
    assert oct(place.root.stat().st_mode)[-3:] == "700"


def test_locked_raises_lock_busy_after_the_timeout(store):
    """Another actor holds it and did not finish. Not "there is no session": a caller that read a
    contended lock as an absent one would take the PAC from under a live `up`."""
    started = time.monotonic()
    with store.locked():
        with pytest.raises(session.LockBusy):
            with session.Session(store.root).locked(timeout=0.5):
                pass
    assert time.monotonic() - started < 30


_HOLDER = textwrap.dedent(
    """
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    sys.stdout.write("held\\n")
    sys.stdout.flush()
    time.sleep(30)
    """
)


def test_a_real_process_holding_the_lock_makes_locked_raise(store):
    """flock is per open-file description and across processes: the timeout has to be reached
    against a real holder, not only against this interpreter."""
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(store.lock_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(session.LockBusy):
            with store.locked(timeout=0.5):
                pass
    finally:
        holder.kill()
        holder.wait()


def test_the_lock_is_not_inherited_by_children(store):
    """`O_CLOEXEC`: a `networksetup` that inherited the descriptor would hold the session lock past
    this command's own exit, and the next `lyrebird down` would wait a minute for nothing."""
    with store.locked():
        held = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)",
                "3",
            ],
            capture_output=True,
            text=True,
        )
    # Descriptor 3 is not the lock in the child, so the call fails on a bad descriptor rather than
    # succeeding on an inherited lock.
    assert held.returncode != 0


def test_write_raises_when_the_root_cannot_be_written(store, monkeypatch):
    """A caller whose write failed has no session, and must not carry on as though it had."""

    def refuse(*_args, **_kwargs):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(config, "atomic_write", refuse)
    with pytest.raises(OSError):
        store.write(RECORD)


def test_a_session_defaults_to_the_per_user_root(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "default_root", lambda: tmp_path / "elsewhere")
    assert session.Session().root == tmp_path / "elsewhere"


def test_read_is_a_session_record_for_what_write_published(store):
    store.write(RECORD)
    journal = store.read()
    assert isinstance(journal, SessionRecord)
    assert journal.proxy == Ref(pid=4321, create_time=1000.5)
    assert os.fspath(store.journal_path).endswith("session.json")
