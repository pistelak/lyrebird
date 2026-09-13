"""The session journal: one fixed per-user root, one lock, one record.

A PAC belongs to a network service, and a Mac has one user setting it. So the record of "who holds
the PAC and what was there before" is keyed by the user, not by a control port or a state
directory: two instances that both snapshot the PAC and both install is the failure this replaces
(`LYREBIRD_STATE_DIR` still moves everything else).

The only seam is the root: `Session(root=...)`, defaulting to `default_root()`, which the test
suite monkeypatches so nothing here can reach a contributor's real `~/Library`.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pwd
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import config
import ownership
from ownership import Journal, SessionRecord

SESSION_VERSION = ownership.SESSION_VERSION

# Another holder finishes on its own, so `up` and `down` wait rather than refusing the moment they
# meet one.
LOCK_WAIT_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.5

# Big enough for any record this writes, small enough that a `down` meeting a hand-edited file does
# not read it into memory — see test_read_reports_an_oversized_journal_as_unreadable.
JOURNAL_SIZE_CAP = 64 * 1024


class LockBusy(RuntimeError):
    """Another actor holds the session lock. Not "there is no session"."""


def default_root() -> Path:
    """The fixed per-user root. `pwd` rather than `$HOME`, so a command run under `sudo -E` or a
    launcher with a rewritten environment still finds the one journal this user's sessions share."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / "Library" / "Application Support" / "Lyrebird" / "session"


class Session:
    """The per-user journal: where it lives, how it is locked, and how it is read and written."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.lock_path = self.root / "session.lock"
        self.journal_path = self.root / "session.json"

    def ensure_root(self) -> None:
        """Create the root. Every actor calls this before `locked()`: a first-ever `down` on a clean
        machine must reach the absent-journal row, not `ENOENT` — see
        test_locked_requires_the_root_to_exist."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def locked(self, timeout: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
        """Hold the session lock for the length of the block, or raise `LockBusy`.

        The descriptor is never handed to a child: a `networksetup` that inherited it would hold the
        lock past this command's own exit (test_the_lock_is_not_inherited_by_children).
        """
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
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
            yield
        finally:
            os.close(descriptor)

    def read(self) -> Journal:
        """What the journal says, without taking the lock.

        `Absent` is a claim — there is no record — and only "no such file" under a root that holds
        no entry there earns it. Everything else that cannot be turned into a record is
        `Unreadable`, because a record that cannot be read may still be holding somebody's PAC.
        """
        try:
            # `O_NOFOLLOW`: a link at the journal path — dangling or pointing at a valid record
            # somewhere else — is something somebody put there, not a record `up` published; opened
            # through, `down` restored from and unlinked a file that was never the journal — see
            # test_read_reports_a_symlink_to_a_valid_record_as_unreadable.
            descriptor = os.open(self.journal_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return ownership.Absent()
        except OSError as error:
            if error.errno == errno.ELOOP:
                return ownership.Unreadable("the journal path is a symlink")
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

    def write(self, record: SessionRecord) -> None:
        """Publish the record. Raises `OSError`; a caller whose write failed has no session."""
        config.atomic_write(
            self.journal_path, json.dumps(ownership.encode(record), indent=2, sort_keys=True, ensure_ascii=False)
        )

    def unlink(self) -> None:
        """Remove the journal. `missing_ok`: a clean absent-journal `down` releases nothing and must
        still exit 0. Any other `OSError` propagates — a journal that could not be removed is not
        one that is gone."""
        self.journal_path.unlink(missing_ok=True)
