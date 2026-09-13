"""The journal on disk: what counts as absent, what counts as unreadable, and what is durable.

The failure paths are the subject. A journal that cannot be read is the only record of what to put
back on somebody's network service, so "I could not read it" must never arrive as "there is
nothing here", a write must be on disk before anything acts on it, and an archive must never
replace an earlier one.
"""

import json
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

import ownership as own
import session as sess

SINCE = "20260912T101500Z"
OWNER = own.Owner(control_port=8088, profile_fingerprint="ab12cd34ef56", state_root="/path/to/state")
SERVICE = own.ServiceRef(name="Wi-Fi", device="en0")
BASELINE = own.Pac(url="http://proxy.example.com/corp.pac", enabled=True)
PROXY = own.Ref(pid=101, create_time=1000.5)
WATCHDOG = own.Ref(pid=102, create_time=1001.5)
RECORD = own.SessionRecord(
    version=1,
    since=SINCE,
    simulator=None,
    owner=OWNER,
    service=SERVICE,
    baseline=BASELINE,
    phase=own.Active(PROXY, WATCHDOG),
)


@pytest.fixture
def subject(tmp_path):
    return sess.Session(tmp_path / "root" / "session")


# MARK: - the isolation the whole suite depends on


def test_the_real_per_user_root_is_never_the_one_a_test_sees(tmp_path):
    """The autouse `_no_real_session_root` is what keeps every test off
    `~/Library/Application Support/Lyrebird/session`. Without this assertion, a fixture that
    stopped working would be invisible until a contributor's own session was overwritten."""
    root = sess.default_root()
    assert str(root).startswith(str(tmp_path.parent)), root
    assert "Application Support" not in str(root)


# MARK: - read()


def test_read_reports_an_absent_root_as_absent(subject):
    """A first-ever `down` on a clean machine must reach the absent-journal row, not an ENOENT —
    and reading must create nothing (the CI launcher smoke check runs `status` on a clean user)."""
    assert isinstance(subject.read(), own.Absent)
    assert not subject.root.exists()


def test_read_reports_an_absent_file_as_absent(subject):
    subject.ensure_root()
    assert isinstance(subject.read(), own.Absent)


def test_read_reports_a_dangling_symlink_as_unreadable(subject):
    """The entry is there and what it pointed at may come back: read as absent, `up` would acquire
    over a session that still holds the PAC."""
    subject.ensure_root()
    subject.journal_path.symlink_to(subject.root / "nothing.json")
    journal = subject.read()
    assert isinstance(journal, own.Unreadable) and "symlink" in journal.reason


def test_read_reports_a_directory_as_unreadable(subject):
    subject.ensure_root()
    subject.journal_path.mkdir()
    assert isinstance(subject.read(), own.Unreadable)


def test_read_reports_an_empty_file_as_unreadable(subject):
    subject.ensure_root()
    subject.journal_path.write_text("", encoding="utf-8")
    assert isinstance(subject.read(), own.Unreadable)


def test_read_reports_malformed_json_as_unreadable(subject):
    subject.ensure_root()
    subject.journal_path.write_text("{not json", encoding="utf-8")
    assert isinstance(subject.read(), own.Unreadable)


def test_read_reports_a_non_object_as_unreadable(subject):
    subject.ensure_root()
    subject.journal_path.write_text("[1, 2]", encoding="utf-8")
    assert isinstance(subject.read(), own.Unreadable)


def test_read_reports_a_record_the_decoder_refuses_as_unreadable(subject):
    subject.ensure_root()
    data = own.encode(RECORD)
    data["owner"]["controlPort"] = 0
    subject.journal_path.write_text(json.dumps(data), encoding="utf-8")
    journal = subject.read()
    assert isinstance(journal, own.Unreadable) and "port" in journal.reason


def test_read_reports_an_oversized_journal_as_unreadable(subject):
    """Refused on the `fstat`, before the bytes are read: the cap exists to bound what a `down`
    meeting a hand-edited file loads into memory."""
    subject.ensure_root()
    subject.journal_path.write_bytes(b"x" * (sess.JOURNAL_SIZE_CAP + 1))
    journal = subject.read()
    assert isinstance(journal, own.Unreadable) and "larger than" in journal.reason


def test_read_returns_the_record_that_was_written(subject):
    subject.ensure_root()
    subject.write(RECORD)
    assert subject.read() == RECORD


# MARK: - write() and durability


@pytest.fixture
def fsynced(monkeypatch):
    """Record what every `os.fsync` in the run was called on: a regular file or a directory, and
    which inode."""
    real = os.fsync
    seen = []

    def recording(descriptor):
        info = os.fstat(descriptor)
        seen.append((stat.S_ISDIR(info.st_mode), info.st_ino))
        return real(descriptor)

    monkeypatch.setattr(os, "fsync", recording)
    return seen


def test_write_fsyncs_the_file_and_the_directory(subject, fsynced):
    """A record that is visible is not yet durable: a crash between the rename and the flush leaves
    the journal naming a file that is not there, and the PAC pointing at a dead port with nothing
    left that says what to restore."""
    subject.ensure_root()
    fsynced.clear()
    subject.write(RECORD)
    assert (False, subject.journal_path.stat().st_ino) in fsynced
    assert (True, subject.root.stat().st_ino) in fsynced


def test_ensure_root_syncs_parents_it_did_not_create(subject, fsynced):
    """An actor that created `session/` and died before syncing its parent leaves a directory the
    next actor finds already there; a journal written into it is only durable if the directory
    entry naming it is too."""
    subject.ensure_root()
    # The *parent* of each component is what carries its name, so those are the entries that have
    # to reach the disk; the root's own entry is synced by the write that puts the journal in it.
    wanted = {subject.root.parent.stat().st_ino, subject.root.parent.parent.stat().st_ino}
    assert wanted <= {inode for is_dir, inode in fsynced if is_dir}

    fsynced.clear()
    subject.ensure_root()  # everything exists this time, and is synced all the same
    assert wanted <= {inode for is_dir, inode in fsynced if is_dir}


def test_first_use_directories_are_synced(subject, fsynced):
    """`archive/` is created the first time something is archived, and the entry that *names* it
    lives in the root — so the root has to reach the disk too, not only the new directory.

    An archive is the last copy of a baseline the session has given up on: published into a
    directory whose own name was never synced, a crash takes the directory and the file with it,
    and `down` has already reported the obligation discharged.
    """
    subject.ensure_root()
    assert not subject.archive_dir.exists()
    fsynced.clear()

    path = subject.archive("20260912T101500Z", "unreadable", b"{}")

    directories = {inode for is_dir, inode in fsynced if is_dir}
    assert subject.root.stat().st_ino in directories, "the entry naming archive/ is durable"
    assert subject.archive_dir.stat().st_ino in directories, "and so is the one naming the file"
    assert path.is_file()


def test_ensure_root_creates_the_root_privately(subject):
    subject.ensure_root()
    assert stat.S_IMODE(subject.root.stat().st_mode) == 0o700


def test_barrier_fsyncs_the_published_record(subject, fsynced):
    subject.ensure_root()
    subject.write(RECORD)
    fsynced.clear()
    subject.barrier()
    assert (False, subject.journal_path.stat().st_ino) in fsynced
    assert (True, subject.root.stat().st_ino) in fsynced


def test_barrier_raises_when_there_is_nothing_to_prove(subject):
    """`down` discharges only at a durable checkpoint; a barrier over a missing file must not be
    read as "the checkpoint is safe"."""
    subject.ensure_root()
    with pytest.raises(OSError):
        subject.barrier()


def test_write_refuses_what_read_would_reject(subject):
    """The writer proves this module's own reader accepts the record *before* the atomic replace,
    which is what lets `up` refuse an unjournallable PAC with nothing spawned and nothing
    installed."""
    subject.ensure_root()
    unreadable = own.SessionRecord(
        version=1,
        since=SINCE,
        simulator=None,
        owner=own.Owner(8088, "a" * (own.MAX_STRING + 1), "/path/to/state"),
        service=SERVICE,
        baseline=BASELINE,
        phase=own.Acquiring(None),
    )
    with pytest.raises(sess.Unrepresentable):
        subject.write(unreadable)
    assert isinstance(subject.read(), own.Absent)


def test_write_measures_the_cap_in_bytes(subject):
    """Characters are not bytes, and `read()` measures bytes with `fstat`: a record accepted by
    character count would be written and then read back as `Unreadable` for ever."""
    subject.ensure_root()
    wide = "あ" * own.MAX_STRING
    record = own.SessionRecord(
        version=1,
        since=SINCE,
        simulator=own.Simulator(udid=wide, name=wide),
        owner=own.Owner(8088, wide, wide),
        service=own.ServiceRef(name=wide, device=wide),
        baseline=own.Pac(url=wide, enabled=False),
        phase=own.Acquiring(None),
    )
    text = json.dumps(own.encode(record), ensure_ascii=False)
    assert len(text) < sess.JOURNAL_SIZE_CAP < len(text.encode("utf-8"))
    with pytest.raises(sess.Unrepresentable, match="larger than"):
        subject.write(record)
    assert isinstance(subject.read(), own.Absent)


def test_unlink_is_quiet_about_a_journal_that_is_not_there(subject):
    subject.ensure_root()
    subject.unlink()  # a clean absent-journal `down` releases nothing and still exits 0


# MARK: - the archive


def test_archive_creates_the_directory_and_writes_privately(subject):
    subject.ensure_root()
    path = subject.archive(SINCE, "unreadable", b"{ the bytes as they were")
    assert path.parent == subject.archive_dir
    assert stat.S_IMODE(subject.archive_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == b"{ the bytes as they were"
    assert path.name.startswith(f"{SINCE}-unreadable-") and path.suffix == ".json"
    assert not list(subject.archive_dir.glob("*.tmp"))


def test_archive_record_writes_the_record_it_was_given(subject):
    subject.ensure_root()
    path = subject.archive_record(SINCE, "displaced", RECORD)
    assert own.decode(json.loads(path.read_text(encoding="utf-8"))) == RECORD


def test_archive_file_copies_the_whole_journal(subject):
    """Streamed, never loaded: the journal this is called for may be the oversize one `read()`
    refused, and `down` must not spend the memory the cap exists to bound."""
    subject.ensure_root()
    payload = b"x" * (sess.JOURNAL_SIZE_CAP + 1024)
    subject.journal_path.write_bytes(payload)
    path = subject.archive_file(SINCE, "unreadable")
    assert path.read_bytes() == payload


def test_archive_file_raises_when_the_bytes_cannot_be_read(subject):
    """`down` must not replace a journal whose bytes it failed to keep: an archive holding an error
    string instead of the file would be the loss it exists to prevent."""
    subject.ensure_root()
    with pytest.raises(OSError):
        subject.archive_file(SINCE, "unreadable")


def test_archive_never_replaces_an_earlier_archive(subject, monkeypatch):
    """`mkstemp` can hand back a name an earlier publication freed; a `rename` would then replace
    that archive silently. Publication is `link`, which fails on a name already taken."""
    subject.ensure_root()
    first = subject.archive(SINCE, "displaced", b"the first")

    reused = subject.archive_dir / (first.name + ".tmp")
    handed_out = []

    real_mkstemp = sess.tempfile.mkstemp

    def colliding(*args, **kwargs):
        if not handed_out:
            handed_out.append(reused)
            return (os.open(reused, os.O_RDWR | os.O_CREAT, 0o600), str(reused))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(sess.tempfile, "mkstemp", colliding)
    second = subject.archive(SINCE, "displaced", b"the second")

    assert second != first
    assert first.read_bytes() == b"the first"
    assert second.read_bytes() == b"the second"
    assert not list(subject.archive_dir.glob("*.tmp"))


@pytest.mark.parametrize("since", ["../../etc/passwd", "nope", "20260912T101500"])
def test_archive_refuses_a_since_that_is_not_a_timestamp(subject, since):
    subject.ensure_root()
    with pytest.raises(ValueError):
        subject.archive(since, "displaced", b"x")


# MARK: - the lock


def test_locked_requires_the_root_to_exist(subject):
    """Every actor calls `ensure_root()` first; if one stops, this is what says so, rather than a
    `down` that fails with ENOENT on a machine that simply has no session yet."""
    with pytest.raises(OSError):
        with subject.locked(timeout=0.1):
            pass


def test_locked_times_out_with_lock_busy(subject):
    subject.ensure_root()
    with subject.locked():
        started = time.monotonic()
        with pytest.raises(sess.LockBusy):
            with subject.locked(timeout=0.2):
                pass
        assert time.monotonic() - started < 5


def test_locked_without_a_timeout_waits_for_the_holder(subject):
    """The watchdog's mode: an idempotent `up` can hold the lock for the length of a `simctl`
    relaunch, and a watchdog that retired over contention would leave the session unwatched."""
    subject.ensure_root()
    released = threading.Event()
    acquired = threading.Event()

    def hold():
        with subject.locked():
            acquired.set()
            time.sleep(0.3)
        released.set()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert acquired.wait(5)
    with sess.Session(subject.root).locked(timeout=None):
        assert released.is_set(), "the blocking lock returned while another holder still had it"
    holder.join(5)


def test_the_lock_is_held_by_a_child_that_outlives_its_parent(subject):
    """flock is per open-file-description, so the descriptor a `networksetup` inherits through
    `pass_fds` keeps the session locked even if the command that started it is killed. Required,
    not optional: without it a half-applied PAC write races the next `up`.
    """
    subject.ensure_root()
    descriptor = os.open(subject.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(descriptor,), close_fds=True
    )
    try:
        sess.fcntl.flock(descriptor, sess.fcntl.LOCK_EX)
        os.close(descriptor)  # the parent is gone; only the child still holds the description
        with pytest.raises(sess.LockBusy):
            with subject.locked(timeout=0.3):
                pass
    finally:
        child.kill()
        child.wait(10)
    with subject.locked(timeout=5):
        pass  # the child exited, the lock went with it
