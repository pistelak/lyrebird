"""The session journal: one fixed per-user root, one lock, one record, and an archive beside it.

A PAC belongs to a network service, and a Mac has one user setting it. So the record of "who holds
the PAC and what was there before" is keyed by the user, not by a control port or a state
directory: two instances that both snapshot the PAC and both install is the failure this replaces
(`LYREBIRD_STATE_DIR` still moves everything else).

Everything here is durable on purpose. A journal entry that is visible but not yet on disk is a
promise a power cut can withdraw, and what it promises is somebody's proxy settings: the write
fsyncs the file *and* the directory entry that names it, and `barrier()` exists for the callers
that have to know a record survived before they act on it.

The only seam is the root: `Session(root=...)`, defaulting to `default_root()`, which the test
suite monkeypatches so nothing here can reach a contributor's real `~/Library`.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import pwd
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import config
import ownership
from ownership import Archived, Journal, Reason, SessionRecord

SESSION_VERSION = ownership.SESSION_VERSION

# Today's `_LOCK_WAIT_SECONDS`: another holder finishes on its own, so `up` and `down` wait rather
# than refusing the moment they meet one.
LOCK_WAIT_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.5

# Big enough for any record this writes, small enough that a `down` meeting a hand-edited file does
# not read it into memory — see test_read_reports_an_oversized_journal_as_unreadable.
JOURNAL_SIZE_CAP = 64 * 1024

_ARCHIVE_ATTEMPTS = 3


class LockBusy(RuntimeError):
    """Another actor holds the session lock. Not "there is no session"."""


class Unrepresentable(RuntimeError):
    """A record this writer's own reader would reject. Raised before anything is written."""


def default_root() -> Path:
    """The fixed per-user root. `pwd` rather than `$HOME`, so a command run under `sudo -E` or a
    launcher with a rewritten environment still finds the one journal this user's sessions share."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / "Library" / "Application Support" / "Lyrebird" / "session"


def _mkdir_durable(path: Path) -> None:
    """Create `path` and its ancestors 0700, and fsync the parent of every component — including
    the ones this call found already there.

    An actor that created `session/` and died before syncing its parent leaves a directory the next
    actor finds with `EEXIST`; a journal written into it before the PAC is touched is only durable
    if the directory entry naming it is too — see test_ensure_root_syncs_parents_it_did_not_create.
    """
    for component in reversed([path, *path.parents]):
        parent = component.parent
        if component == parent:  # the filesystem root: nothing to create and nothing to sync
            continue
        if not component.is_dir():
            # `exist_ok` covers the race with another actor doing this at the same time; a
            # *non-directory* here still raises, because the caller is named for writing into it.
            component.mkdir(mode=0o700, exist_ok=True)
        _fsync_dir(parent)


def _dumps(record: SessionRecord | Archived) -> str:
    """The journal's text. `ensure_ascii=False` keeps a service name in the operator's own script
    readable in the file — and makes the size cap a claim about bytes, which is the unit `read()`
    measures with `fstat` (test_write_measures_the_cap_in_bytes)."""
    return json.dumps(ownership.encode(record), indent=2, sort_keys=True, ensure_ascii=False)


def _write_all(descriptor: int, payload: bytes) -> None:
    """`os.write` may write less than it was given; a short write would truncate the copy of the
    bytes an archive exists to keep — see test_archive_file_copies_the_whole_journal."""
    written = 0
    while written < len(payload):
        written += os.write(descriptor, payload[written:])


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Session:
    """The per-user journal: where it lives, how it is locked, and how it is read and written."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.lock_path = self.root / "session.lock"
        self.journal_path = self.root / "session.json"
        self.archive_dir = self.root / "archive"

    # MARK: - the root and the lock

    def ensure_root(self) -> None:
        """Durably create the root. Every actor calls this before `locked()`: a first-ever `down`
        on a clean machine must reach the absent-journal row, not `ENOENT` — see
        test_locked_requires_the_root_to_exist."""
        _mkdir_durable(self.root)

    @contextmanager
    def locked(self, timeout: float | None = LOCK_WAIT_SECONDS) -> Iterator[int]:
        """Hold the session lock, yielding the lock fd.

        `timeout=None` blocks — the watchdog's mode: an idempotent `up` can hold the lock for the
        length of a `simctl` relaunch, and a watchdog that retired over contention would leave the
        session unwatched (test_locked_without_a_timeout_waits_for_the_holder).

        The fd number is what `netproxy`'s mutators pass to `subprocess.run(pass_fds=...)`: flock is
        per open-file-description, so a `networksetup` that outlives a killed parent keeps the lock
        until it exits (test_the_lock_is_held_by_a_child_that_outlives_its_parent).
        """
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if timeout is None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise LockBusy(
                                f"another Lyrebird command is holding {self.lock_path} (waited {timeout:g}s)"
                            ) from None
                        time.sleep(min(_LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            yield descriptor
        finally:
            os.close(descriptor)

    # MARK: - reading

    def read(self) -> Journal:
        """What the journal says, without taking the lock.

        `Absent` is a claim — there is no record — and only "no such file" under a root that holds
        no entry there earns it. Everything else that cannot be turned into a record is
        `Unreadable`, because a record that cannot be read may still be holding somebody's PAC.
        """
        try:
            descriptor = os.open(self.journal_path, os.O_RDONLY)
        except FileNotFoundError as error:
            if self.journal_path.is_symlink():
                # A link whose target is gone raises the same error as no entry at all, and is a
                # different thing — see test_read_reports_a_dangling_symlink_as_unreadable.
                return ownership.Unreadable(f"dangling symlink: {error}")
            return ownership.Absent()
        except NotADirectoryError as error:
            return ownership.Unreadable(str(error))
        except OSError as error:
            return ownership.Unreadable(str(error))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                return ownership.Unreadable("the journal path is not a regular file")
            if info.st_size > JOURNAL_SIZE_CAP:
                return ownership.Unreadable(f"the journal is larger than {JOURNAL_SIZE_CAP} bytes")
            raw = os.read(descriptor, JOURNAL_SIZE_CAP + 1)
        except OSError as error:
            return ownership.Unreadable(str(error))
        finally:
            os.close(descriptor)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError, UnicodeDecodeError) as error:
            # `ValueError` rather than `JSONDecodeError` (the decoder raises the plain one for a
            # number too long to convert) and `RecursionError` for nesting too deep: both used to
            # traceback out of `down`.
            return ownership.Unreadable(str(error))
        try:
            return ownership.decode(data)
        except ownership.DecodeError as error:
            return ownership.Unreadable(str(error))

    # MARK: - writing

    def write(self, record: SessionRecord | Archived) -> None:
        """Publish a record, durably, having first proved this module's own reader accepts it.

        A record its reader would reject is a session nothing can recover — so it raises
        `Unrepresentable` and writes nothing, which is what lets `up` refuse an observed PAC it
        cannot journal *before* it spawns or installs anything
        (test_write_refuses_what_read_would_reject).
        """
        text = _dumps(record)
        encoded = text.encode("utf-8")
        if len(encoded) > JOURNAL_SIZE_CAP:
            # Bytes, the unit `read()` measures with `fstat` — a service name in Japanese is three
            # bytes a character (test_write_measures_the_cap_in_bytes).
            raise Unrepresentable(f"the record is larger than {JOURNAL_SIZE_CAP} bytes")
        try:
            ownership.decode(json.loads(text))
        except (ValueError, RecursionError) as error:
            raise Unrepresentable(str(error)) from None
        config.atomic_write(self.journal_path, text, durable=True)

    def barrier(self) -> None:
        """fsync the journal file and the directory that names it, raising on failure.

        `atomic_write(durable=True)` already does this for what it wrote; this is for the caller
        that has to *prove* a record it read back is on disk before acting on it — `down`
        discharges only at a durable checkpoint, and a crash after an unlink over an un-synced
        `Restored` could resurrect the earlier `Active` and restore across a closed boundary.
        """
        descriptor = os.open(self.journal_path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_dir(self.journal_path.parent)

    def unlink(self) -> None:
        """Remove the journal. `missing_ok`: a clean absent-journal `down` releases nothing and must
        still exit 0. Any other `OSError` propagates — a journal that could not be removed is not
        one that is gone."""
        self.journal_path.unlink(missing_ok=True)

    # MARK: - the archive

    def archive(self, since: str, reason: Reason, payload: bytes) -> Path:
        """Write `payload` into `archive/` under a name that cannot displace an earlier one.

        Publication is `link` + `unlink`, not `rename`: `mkstemp` can hand out a name an earlier
        publication freed, and `rename` would then silently replace that archive — the one thing an
        archive may never do (test_archive_never_replaces_an_earlier_archive).
        """
        if not ownership.SINCE_PATTERN.match(since):
            raise ValueError(f"since must be YYYYMMDDTHHMMSSZ, not {since!r}")
        if reason not in ownership.REASONS:
            raise ValueError(f"unknown archive reason {reason!r}")
        _mkdir_durable(self.archive_dir)

        def write_payload(descriptor: int) -> None:
            _write_all(descriptor, payload)

        return self._publish(since, reason, write_payload)

    def archive_record(self, since: str, reason: Reason, record: SessionRecord) -> Path:
        return self.archive(since, reason, _dumps(record).encode())

    def archive_file(self, since: str, reason: Reason) -> Path:
        """Copy the journal's raw bytes into the archive without loading them.

        The journal this is called for is the one `read()` refused, which may be refused *for being
        oversize*: reading it whole would cost `down` the memory the cap exists to bound
        (test_read_reports_an_oversized_journal_as_unreadable). Raises `OSError` when the
        source cannot be opened or read — the caller must not replace a file whose bytes it failed
        to keep.
        """
        if not ownership.SINCE_PATTERN.match(since):
            raise ValueError(f"since must be YYYYMMDDTHHMMSSZ, not {since!r}")
        if reason not in ownership.REASONS:
            raise ValueError(f"unknown archive reason {reason!r}")
        _mkdir_durable(self.archive_dir)

        def copy(descriptor: int) -> None:
            source = os.open(self.journal_path, os.O_RDONLY)
            try:
                while True:
                    chunk = os.read(source, 64 * 1024)
                    if not chunk:
                        break
                    _write_all(descriptor, chunk)
            finally:
                os.close(source)

        return self._publish(since, reason, copy)

    def _publish(self, since: str, reason: str, fill: Callable[[int], None]) -> Path:
        last: OSError | None = None
        for _ in range(_ARCHIVE_ATTEMPTS):
            descriptor, name = tempfile.mkstemp(dir=self.archive_dir, prefix=f"{since}-{reason}-", suffix=".json.tmp")
            tmp = Path(name)
            try:
                os.fchmod(descriptor, 0o600)
                fill(descriptor)
                os.fsync(descriptor)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise
            os.close(descriptor)
            final = tmp.with_suffix("")  # drop the `.tmp`, keeping `<since>-<reason>-<unique>.json`
            try:
                os.link(tmp, final)
            except FileExistsError as error:
                # The one failure a retry answers: this name is already an archive, so a new unique
                # one is drawn rather than replacing it.
                last = error
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
                continue
            tmp.unlink(missing_ok=True)
            _fsync_dir(self.archive_dir)
            return final
        raise OSError(
            errno.EEXIST, f"could not publish an archive under {self.archive_dir} after {_ARCHIVE_ATTEMPTS} attempts"
        ) from last
