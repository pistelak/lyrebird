"""Starting and stopping the proxy: the session journal, the PAC, the watchdog, and what `up` must achieve.

Four executors — `up`, `down`, `status` and the watchdog — and every one of them has the same
shape: **observe, decide, act**. The observation is a set of facts, each of which may say "I could
not be read"; the decision is `ownership.decide_*`, which is pure and exhaustively tested; the act
is one of the four recipes below, each of which reads the PAC back and refuses anything it cannot
account for. Nothing consequential is decided here — an executor that branched on a fact itself
would be a fifth place the rule "uncertainty acts on nothing" has to be got right.

The journal is the authority: one record per user, under one lock, saying who holds the PAC and
what was there before. `down` is the only recovery command, and it finds that record from any
profile, port or directory.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn

import click

import api
import config
import netproxy
import ownership
import procs
import session
import simulator as sim
import store
import ui
from ownership import (
    Absent,
    Acquiring,
    Active,
    AlreadyRestored,
    Answering,
    Archive,
    Archived,
    Complete,
    Configured,
    DownObs,
    Incomplete,
    Known,
    Liveness,
    Marked,
    NotObserved,
    Off,
    On,
    Owner,
    Pac,
    PacClass,
    PacUnreadable,
    Present,
    Preserve,
    Proceed,
    Ref,
    ReleaseNow,
    ReleaseObs,
    RepairOwnPac,
    Restore,
    Restored,
    ServiceRef,
    SessionRecord,
    SweepClean,
    SweepFailed,
    SweepLeft,
    Unknown,
    Unreadable,
    UpObs,
    WatchdogObs,
)

MITMDUMP = config.ROOT / ".venv" / "bin" / "mitmdump"
_DOWN_WAIT_SECONDS = 5.0  # how long `down` waits for the control port to go quiet
_WATCHDOG_RESTORE_ATTEMPTS = 5  # `networksetup` fails transiently; one try is not a restore
_WATCHDOG_POLL_SECONDS = 2.0
_STARTUP_DEADLINE_SECONDS = 12.0
_READY_TIMEOUT_SECONDS = 10.0

_DOWN = "run `lyrebird down`"


# MARK: - the session, the owner, the clock


def _session() -> session.Session:
    """Every construction in this module goes through here, so the test suite's one patch of
    `session.default_root` reaches all of them."""
    return session.Session()


def _owner() -> Owner:
    """Who this invocation would be. All three fields: a port, a profile and a state directory each
    repeat on their own."""
    return Owner(
        control_port=config.CONTROL_PORT,
        profile_fingerprint=config.PROFILE_FINGERPRINT,
        state_root=str(config.STATE_ROOT),
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _red(message: str) -> None:
    click.echo(f"{ui.RED}✗ {message}{ui.R}")


# MARK: - Observation helpers
#
# Phase-dependent on purpose: a terminal or unreadable journal has its PAC neither read nor
# touched, and a service that is gone has no PAC to read. What a phase does not consult is
# `NotObserved`, which every `decide_*` refuses to act on where the row does consult it.


def _observe_route() -> ownership.Route:
    try:
        found = netproxy.active_service()
    except netproxy.RouteAmbiguous:
        return ownership.RouteAmbiguous()
    except netproxy.NetworkSetupError as error:
        return ownership.RouteFailed(str(error))
    return ownership.NoRoute() if found is None else On(found)


def _observe_service(ref: ServiceRef) -> ownership.Service:
    try:
        return netproxy.resolve(ref)
    except netproxy.NetworkSetupError as error:
        # A table that could not be read is not a service that is gone: `Gone` archives a session,
        # and this must only preserve it — see test_down_preserves_when_the_service_table_cannot_be_read.
        return ownership.ServiceFailed(str(error))


def _read_pac(name: str) -> Pac | PacUnreadable:
    try:
        return netproxy.pac_status(name)
    except netproxy.NetworkSetupError as error:
        return PacUnreadable(str(error))


def _observe_pac(service: ownership.Service, port: int, baseline: Pac) -> PacClass | NotObserved:
    if not isinstance(service, Present):
        return NotObserved()
    return ownership.classify(_read_pac(service.name), port, baseline)


def _named_refs(journal: ownership.Journal) -> tuple[tuple[Ref, ownership.Marker], ...]:
    """Every process the journal names, with the marker its argv must carry. The only source of a
    ref this module ever signals through: a pid from a fresh scan never re-authorises acting on a
    process the journal already names."""
    if isinstance(journal, SessionRecord):
        phase = journal.phase
        if isinstance(phase, Active):
            return ((phase.proxy, "proxy"), (phase.watchdog, "watchdog"))
        if phase.proxy is not None:
            return ((phase.proxy, "proxy"),)
        return ()
    if isinstance(journal, Archived) and isinstance(journal.context, Known) and journal.context.proxy is not None:
        return ((journal.context.proxy, "proxy"),)
    return ()


def _journal_port(journal: ownership.Journal) -> int | None:
    if isinstance(journal, SessionRecord):
        return journal.owner.control_port
    if isinstance(journal, Archived) and isinstance(journal.context, Known):
        return journal.context.owner.control_port
    return None


def _observe_refs(journal: ownership.Journal) -> tuple[Liveness | NotObserved, Liveness | NotObserved]:
    """(proxy, watchdog) liveness for the refs this journal names, `NotObserved` for the rest."""
    port = _journal_port(journal)
    if port is None:
        return NotObserved(), NotObserved()
    proxy: Liveness | NotObserved = NotObserved()
    watchdog: Liveness | NotObserved = NotObserved()
    for ref, marker in _named_refs(journal):
        if marker == "proxy":
            proxy = procs.liveness(ref, marker, port)
        else:
            watchdog = procs.liveness(ref, marker, port)
    return proxy, watchdog


def _observe_release(
    journal: ownership.Journal, port: int | None, sweep: ownership.SweepResult | NotObserved, scan: ownership.Scan
) -> ReleaseObs:
    """Fresh liveness, fresh health on the row's port (and nothing on a portless row), and the scan
    the caller has just taken."""
    proxy, watchdog = _observe_refs(journal)
    health: ownership.Health | NotObserved = NotObserved()
    if port is not None:
        health = api.observe_health(port).health
    return ReleaseObs(journal=journal, proxy=proxy, watchdog=watchdog, health=health, sweep=sweep, scan=scan)


def _observe_down(journal: ownership.Journal, scan: ownership.Scan) -> DownObs:
    """`down`'s observation for the journal it found, phase by phase."""
    service: ownership.Service | NotObserved = NotObserved()
    pac: PacClass | NotObserved = NotObserved()
    health: ownership.Health | NotObserved = NotObserved()
    proxy: Liveness | NotObserved = NotObserved()
    watchdog: Liveness | NotObserved = NotObserved()
    holding = isinstance(journal, SessionRecord) and not isinstance(journal.phase, Restored)
    if isinstance(journal, SessionRecord) and holding and journal.phase.proxy is not None:
        port = journal.owner.control_port
        proxy, watchdog = _observe_refs(journal)
        service = _observe_service(journal.service)
        pac = _observe_pac(service, port, journal.baseline)
        health = api.observe_health(port).health
    return DownObs(journal=journal, service=service, pac=pac, health=health, proxy=proxy, watchdog=watchdog, scan=scan)


# MARK: - The recipes
#
# A recipe is called only after the executor has observed and decided. Its own pre-write read is a
# last check that the state is still one it may act on, never a decision: anything it cannot
# account for is `UnexpectedPac`, and what that means is fixed per caller — the obligation always
# stays open. A recipe never archives and never writes the journal.


def _install(ref: ServiceRef, port: int, expected: Pac, lock_fd: int) -> None:
    """Put our PAC on, over exactly the reading the acquisition observed.

    The pre-write read must equal `expected` verbatim — disabled Lyrebird residue included, so
    `up → down → up` installs over what `down` leaves — or nothing is written at all
    (test_up_refuses_to_install_over_a_pac_that_changed_since_it_looked).
    """
    ours = ownership.our_url(port)
    found = netproxy.pac_status(netproxy.resolved_name(ref))
    if found != expected:
        raise netproxy.UnexpectedPac(
            f"the PAC changed while this `up` was deciding: expected {expected}, found {found}"
        )
    netproxy.write_pac_url(netproxy.resolved_name(ref), ours, lock_fd=lock_fd)
    back = netproxy.pac_status(netproxy.resolved_name(ref))
    if back == Pac(ours, True):
        return
    if back == Pac(ours, False):
        netproxy.write_pac_state(netproxy.resolved_name(ref), True, lock_fd=lock_fd)
        back = netproxy.pac_status(netproxy.resolved_name(ref))
        if back == Pac(ours, True):
            return
    raise netproxy.UnexpectedPac(f"the PAC on '{ref.name}' did not become ours (now: {back})")


def _restore(ref: ServiceRef, port: int, baseline: Pac, lock_fd: int) -> Pac:
    """Put the baseline back, in as few `networksetup` commands as the target needs.

    A target that is already satisfied costs zero writes and is still a success: `satisfies` is the
    one predicate for "the obligation is met", and it accepts the disabled residue an Off restore
    leaves (test_restore_issues_exactly_the_commands_the_target_needs).
    """
    target = ownership.restore_target(baseline)
    found = netproxy.pac_status(netproxy.resolved_name(ref))
    cls = ownership.classify(found, port, baseline)
    if ownership.satisfies(cls, target):
        return found
    if cls is PacClass.RESUMABLE:
        # The URL is already the baseline's and only the flag is wrong — one command.
        netproxy.write_pac_state(netproxy.resolved_name(ref), _target_flag(target), lock_fd=lock_fd)
    elif cls is PacClass.OURS_ENABLED or cls is PacClass.OURS_DISABLED:
        _restore_from_ours(ref, target, lock_fd)
    else:
        raise netproxy.UnexpectedPac(f"the PAC on '{ref.name}' is not one this session may restore over (now: {found})")
    final = netproxy.pac_status(netproxy.resolved_name(ref))
    if not ownership.satisfies(ownership.classify(final, port, baseline), target):
        # `networksetup` exits 0 without applying its change; read back, that is an open obligation
        # kept, never one abandoned — see test_down_preserves_when_a_setter_exits_0_and_changes_nothing.
        raise netproxy.UnexpectedPac(f"the PAC on '{ref.name}' did not restore (wanted {target}, now: {final})")
    return final


def _target_flag(target: ownership.Target) -> bool:
    return target.enabled if isinstance(target, Configured) else False


def _restore_from_ours(ref: ServiceRef, target: ownership.Target, lock_fd: int) -> None:
    if isinstance(target, Off):
        # macOS rejects an empty URL, so the only move for "there was no PAC" is the flag.
        netproxy.write_pac_state(netproxy.resolved_name(ref), False, lock_fd=lock_fd)
        return
    netproxy.write_pac_url(netproxy.resolved_name(ref), target.url, lock_fd=lock_fd)
    back = netproxy.pac_status(netproxy.resolved_name(ref))
    if target.enabled:
        # `-setautoproxyurl` switches the PAC on as a side effect, so the flag is only touched when
        # the read-back says it did not.
        if back == Pac(target.url, True):
            return
        if back != Pac(target.url, False):
            raise netproxy.UnexpectedPac(f"the PAC on '{ref.name}' is not the URL just written (now: {back})")
        netproxy.write_pac_state(netproxy.resolved_name(ref), True, lock_fd=lock_fd)
        return
    if back != Pac(target.url, True):
        raise netproxy.UnexpectedPac(f"the PAC on '{ref.name}' is not the URL just written (now: {back})")
    netproxy.write_pac_state(netproxy.resolved_name(ref), False, lock_fd=lock_fd)


def _repair(ref: ServiceRef, port: int, lock_fd: int) -> None:
    """Switch our own PAC back on — the one thing macOS does to it while the proxy is alive."""
    ours = ownership.our_url(port)
    found = netproxy.pac_status(netproxy.resolved_name(ref))
    if found == Pac(ours, True):
        return
    if found != Pac(ours, False):
        raise netproxy.UnexpectedPac(f"the PAC on '{ref.name}' is not this session's to repair (now: {found})")
    netproxy.write_pac_state(netproxy.resolved_name(ref), True, lock_fd=lock_fd)
    back = netproxy.pac_status(netproxy.resolved_name(ref))
    if back != Pac(ours, True):
        raise netproxy.UnexpectedPac(f"the PAC on '{ref.name}' did not come back on (now: {back})")


@dataclass(frozen=True, slots=True)
class SwitchedOff:
    pass


@dataclass(frozen=True, slots=True)
class NothingToSwitchOff:
    pass


@dataclass(frozen=True, slots=True)
class Skipped:
    """The PAC is a Lyrebird one on a port this pass never asked about."""

    port_now: int


def _switch_off(name: str, port: int, lock_fd: int) -> SwitchedOff | NothingToSwitchOff | Skipped:
    """The sweep's only write: switch off a Lyrebird PAC on the exact port whose health was just
    read as silent. The port is re-read here because between the health check and this call the
    service's PAC may have become a *different* Lyrebird's, whose port nobody has asked about."""
    found = netproxy.pac_status(name)
    now = ownership.lyrebird_port(found.url)
    if now is None or not found.enabled:
        return NothingToSwitchOff()
    if now != port:
        return Skipped(now)
    netproxy.write_pac_state(name, False, lock_fd=lock_fd)
    back = netproxy.pac_status(name)
    if back.enabled:
        raise netproxy.UnexpectedPac(f"the PAC on '{name}' is still enabled after switching it off (now: {back})")
    return SwitchedOff()


# MARK: - Process accounting
#
# One rule, shared by `down`, the unwind and the watchdog: a marked process the journal does not
# name is an orphan and is stopped through the ref the scan handed back; a pid the journal *does*
# name is left to its persisted ref while that ref is ALIVE or UNKNOWN. A fresh scan never
# re-authorises signalling such a pid — after a clock step its fresh ref differs from the persisted
# one while nothing about it is proven.


def _excluded_pids(
    journal: ownership.Journal, proxy: Liveness | NotObserved, watchdog: Liveness | NotObserved
) -> set[int]:
    excluded: set[int] = set()
    for ref, marker in _named_refs(journal):
        state = proxy if marker == "proxy" else watchdog
        if state is Liveness.ALIVE or state is Liveness.UNKNOWN or isinstance(state, NotObserved):
            excluded.add(ref.pid)
    return excluded


def _extras(scan: ownership.Scan, excluded: set[int]) -> tuple[Marked, ...]:
    if isinstance(scan, Incomplete):
        return ()
    return tuple(found for found in scan.found if found.ref.pid not in excluded)


def _stop_marked(found: tuple[Marked, ...]) -> list[str]:
    """Stop each marked process through its own ref, and name the ones that did not stop."""
    failures = []
    for marked in found:
        try:
            outcome = procs.terminate(marked.ref, marked.kind, marked.port)
        except procs.ProcessCheckError as error:
            failures.append(f"pid {marked.ref.pid} could not be checked: {error}")
            continue
        if outcome is procs.Termination.STILL_RUNNING:
            failures.append(f"pid {marked.ref.pid} is still running")
    return failures


def _wait_for_quiet(port: int) -> None:
    deadline = time.time() + _DOWN_WAIT_SECONDS
    while time.time() < deadline and isinstance(api.observe_health(port).health, Answering):
        time.sleep(0.2)


# MARK: - `down`


@dataclass(frozen=True, slots=True)
class DownOutcome:
    """What a `down` did, for the in-process callers that must tell the two apart: what became of
    the PAC obligation, and whether the journal was released."""

    exit_code: int
    disposition: Literal["restored", "archived", "none"]
    released: bool
    archive: Path | None
    refused: str | None


def _outcome(
    code: int,
    *,
    disposition: Literal["restored", "archived", "none"] = "none",
    released: bool = False,
    archive: Path | None = None,
    refused: str | None = None,
) -> DownOutcome:
    return DownOutcome(exit_code=code, disposition=disposition, released=released, archive=archive, refused=refused)


def _owner_precondition(journal: ownership.Journal, only_owner: Owner) -> str | None:
    """The in-process caller's gate: proceed only over a journal that provably carries this owner.

    `Unreadable` and `Archived(Unknown)` carry no owner evidence at all, so ownership is unproven
    and the archive-then-sweep an ordinary `down` would run could stop a successor whose record
    merely failed to decode — see test_acceptance_finalizer_writes_nothing_without_proof_of_ownership.
    """
    if isinstance(journal, Absent):
        return None
    if isinstance(journal, Unreadable):
        return f"the session journal cannot be read ({journal.reason}); whose it is cannot be proven"
    if isinstance(journal, Archived):
        if isinstance(journal.context, Unknown):
            return "the archived session names no owner; whose it is cannot be proven"
        if journal.context.owner != only_owner:
            return _foreign(journal.context.owner)
        return None
    if journal.owner != only_owner:
        return _foreign(journal.owner)
    return None


def _foreign(owner: Owner) -> str:
    return (
        f"a Lyrebird session on port {owner.control_port} (profile {owner.profile_fingerprint}) "
        f"owns the PAC; not this run's"
    )


def down_session(sess: session.Session, only_owner: Owner | None = None) -> DownOutcome:
    """The `down` executor, for the CLI and for in-process callers.

    `only_owner` is a precondition evaluated *inside the lock*: without it `down` tears down
    whichever session exists, which is right for the command and wrong for a cleanup that may be
    running after its own session was released — see deviation 16.
    """
    sess.ensure_root()
    with sess.locked() as lock_fd:
        return _down_locked(sess, lock_fd, only_owner)


def _down_locked(sess: session.Session, lock_fd: int, only_owner: Owner | None) -> DownOutcome:
    journal = sess.read()
    if only_owner is not None:
        refusal = _owner_precondition(journal, only_owner)
        if refusal is not None:
            _red(f"{refusal} — nothing was changed")
            return _outcome(1, refused=refusal)

    scan = procs.scan_marked()
    decision = ownership.decide_down(_observe_down(journal, scan))

    if isinstance(journal, Unreadable):
        return _down_unreadable(sess, lock_fd, journal, decision)
    if isinstance(journal, Absent) or (isinstance(journal, Archived) and isinstance(journal.context, Unknown)):
        return _down_portless(sess, lock_fd, journal, decision, scan, from_disk=True)
    if isinstance(journal, Archived) or isinstance(journal.phase, Restored):
        return _down_terminal(sess, journal, decision, scan, barrier=True)
    if isinstance(journal.phase, Acquiring) and journal.phase.proxy is None:
        return _down_acquiring_nothing(sess, journal, decision, scan, spawned=())
    return _down_holding(sess, lock_fd, journal, decision, scan, spawned=(), unwinding=False)


def _down_unreadable(
    sess: session.Session, lock_fd: int, journal: Unreadable, decision: ownership.Decision
) -> DownOutcome:
    """The bytes first, then the record that replaces them, then this run decides again from it.

    Copied rather than parsed: the journal `read()` refused may be refused *for being oversize*,
    and an archive holding an error string instead of the bytes is the loss it exists to prevent.
    """
    if not isinstance(decision, Archive):  # pragma: no cover - decide_down answers Archive here
        raise AssertionError(f"an unreadable journal decides to {decision}")
    since = _now()
    try:
        path = sess.archive_file(since, "unreadable")
    except OSError as error:
        _red(
            f"the session journal at {sess.journal_path} can neither be read ({journal.reason}) nor copied "
            f"({error}) — nothing was changed"
        )
        return _outcome(1)
    record = Archived(
        version=ownership.SESSION_VERSION, since=since, reason="unreadable", path=str(path), context=Unknown()
    )
    try:
        sess.write(record)
    except (OSError, session.Unrepresentable) as error:
        _red(f"the journal's bytes are kept at {path}, but the archive record could not be written ({error})")
        return _outcome(1)
    click.echo(f"{ui.YELLOW}the session journal could not be read; its bytes are kept at {path}{ui.R}")
    scan = procs.scan_marked()
    decision = ownership.decide_down(_observe_down(record, scan))
    outcome = _down_portless(sess, lock_fd, record, decision, scan, from_disk=False)
    return dataclasses.replace(outcome, disposition="archived", archive=path)


def _down_terminal(
    sess: session.Session,
    journal: SessionRecord | Archived,
    decision: ownership.Decision,
    scan: ownership.Scan,
    *,
    barrier: bool,
) -> DownOutcome:
    """`Restored` / `Archived(Known)`: the PAC is never touched again — only the accounting is left.

    The barrier comes first for a record this run *found* on disk: a write can fail after
    `tmp.replace` made the record visible, and an unlink over a visible-but-undurable checkpoint
    could let a crash resurrect the earlier `Active` — see
    test_down_does_not_release_a_restored_record_it_could_not_make_durable.
    """
    if not isinstance(decision, AlreadyRestored):  # pragma: no cover - decide_down answers AlreadyRestored here
        raise AssertionError(f"a terminal record decides to {decision}")
    if barrier and not _barrier(sess):
        return _outcome(1)
    archived = isinstance(journal, Archived)
    released, reason = _account_and_release(sess, journal, scan, spawned=())
    if archived:
        path = journal.path if isinstance(journal, Archived) else None
        click.echo(f"{ui.YELLOW}this session was archived; its baseline is at {path}{ui.R}")
        if not released:
            _red(f"the journal is kept: {reason}")
        return _outcome(1, disposition="archived", released=released, archive=Path(path) if path else None)
    if not released:
        _red(f"the previous settings are back, but the journal is kept: {reason}")
        return _outcome(1, disposition="restored", released=False)
    click.echo(f"{ui.GREEN}stopped{ui.R}")
    return _outcome(0, disposition="restored", released=True)


def _barrier(sess: session.Session) -> bool:
    try:
        sess.barrier()
    except OSError as error:
        _red(f"the checkpoint on disk could not be made durable ({error}) — the journal is kept, try again")
        return False
    return True


def _down_acquiring_nothing(
    sess: session.Session,
    journal: SessionRecord,
    decision: ownership.Decision,
    scan: ownership.Scan,
    *,
    spawned: tuple[tuple[Ref, ownership.Marker], ...],
) -> DownOutcome:
    """`Acquiring(None)`: an `up` that recorded no proxy. Nothing was installed, so there is no PAC
    to read and no terminal record to write — one complete scan, stop what is found, ask the port
    once, and release. Exit 1 either way: an interrupted acquisition is not a clean teardown."""
    port = journal.owner.control_port
    if isinstance(decision, Preserve):
        _red(f"an interrupted `lyrebird up` recorded no proxy, and {decision.reason} — the journal is kept")
        _report(_stop_marked(_extras(scan, set())))
        _report(_stop_spawned(spawned))
        return _outcome(1)
    stopped = _extras(scan, set())
    failures = _stop_marked(stopped) + _stop_spawned(spawned)
    _report(failures)
    health = api.observe_health(port).health
    if isinstance(health, Answering):
        _red(f"a proxy answers on port {port} that no record names (pid {health.pid}) — the journal is kept")
        return _outcome(1)
    remaining = Incomplete() if isinstance(scan, Incomplete) else Complete(())
    if failures:
        remaining = scan
    release = ownership.decide_release(
        ReleaseObs(
            journal=journal,
            proxy=NotObserved(),
            watchdog=NotObserved(),
            health=health,
            sweep=NotObserved(),
            scan=remaining,
        )
    )
    if isinstance(release, ReleaseNow):
        sess.unlink()
        click.echo(
            f"{ui.YELLOW}an interrupted `lyrebird up` recorded no proxy; {_found(stopped)} and the journal is "
            f"released — nothing was installed{ui.R}"
        )
    else:
        _red(f"an interrupted `lyrebird up` recorded no proxy, and the journal is kept: {release.reason}")
    return _outcome(1, released=isinstance(release, ReleaseNow))


def _found(stopped: tuple[Marked, ...]) -> str:
    if not stopped:
        return "no Lyrebird process was found"
    return f"{len(stopped)} Lyrebird process(es) were stopped"


def _stop_spawned(spawned: tuple[tuple[Ref, ownership.Marker], ...]) -> list[str]:
    """Stop the children this run started, through the ref it recorded for each."""
    failures = []
    for ref, marker in spawned:
        try:
            outcome = procs.terminate(ref, marker, config.CONTROL_PORT)
        except procs.ProcessCheckError as error:
            failures.append(f"the {marker} this run started (pid {ref.pid}) could not be checked: {error}")
            continue
        if outcome is procs.Termination.STILL_RUNNING:
            failures.append(f"the {marker} this run started (pid {ref.pid}) is still running")
    return failures


def _report(failures: list[str]) -> None:
    for failure in failures:
        _red(failure)


def _down_holding(
    sess: session.Session,
    lock_fd: int,
    journal: SessionRecord,
    decision: ownership.Decision,
    scan: ownership.Scan,
    *,
    spawned: tuple[tuple[Ref, ownership.Marker], ...],
    unwinding: bool,
) -> DownOutcome:
    """`Active` / `Acquiring(ref)`: fence the watchdog, then decide again on what the PAC is *now*.

    Preserve comes before the fence: an uncertain observation must not cost a live session its
    watchdog (test_down_preserves_before_fencing_when_the_pac_is_unreadable).
    """
    port = journal.owner.control_port
    if isinstance(decision, Preserve):
        _red(f"{decision.reason} — the journal is kept and nothing was changed")
        return _outcome(1)
    if isinstance(decision, ownership.Refuse):
        _red(f"{decision.reason}")
        _report(_stop_spawned(spawned))
        return _outcome(1)

    fence = _fence(journal, spawned, port)
    if fence is not None:
        _red(fence)
        return _outcome(1)

    scan = procs.scan_marked()
    second = ownership.decide_down(_observe_down(journal, scan))
    if isinstance(second, (Preserve, ownership.Refuse)):
        _red(
            f"{second.reason} — the watchdog was already stopped, so the journal keeps its phase; "
            f"{_DOWN} again once it can be settled"
        )
        return _outcome(1)

    return _apply_terminal(sess, lock_fd, journal, second, scan, spawned=spawned, unwinding=unwinding)


def _fence(journal: SessionRecord, spawned: tuple[tuple[Ref, ownership.Marker], ...], port: int) -> str | None:
    """Stop the watchdog, and go no further until it is proven gone: left alive it re-enables the
    PAC this command is about to restore. Returns the sentence to print, or None."""
    watchdog = next((ref for ref, marker in _named_refs(journal) if marker == "watchdog"), None)
    if watchdog is None:
        watchdog = next((ref for ref, marker in spawned if marker == "watchdog"), None)
    if watchdog is None:
        return None
    try:
        outcome = procs.terminate(watchdog, "watchdog", port)
    except procs.ProcessCheckError as error:
        return f"the watchdog (pid {watchdog.pid}) could not be checked: {error} — the journal is kept"
    if outcome is procs.Termination.STILL_RUNNING:
        return (
            f"the watchdog (pid {watchdog.pid}) is still running — nothing was changed; stop it by hand "
            f"(`kill -9 {watchdog.pid}`) and {_DOWN} again"
        )
    return None


def _apply_terminal(
    sess: session.Session,
    lock_fd: int,
    journal: SessionRecord,
    decision: ownership.Decision,
    scan: ownership.Scan,
    *,
    spawned: tuple[tuple[Ref, ownership.Marker], ...],
    unwinding: bool,
) -> DownOutcome:
    """Act on the post-fence decision and checkpoint it, then account and release."""
    port = journal.owner.control_port
    proxy = journal.phase.proxy if not isinstance(journal.phase, Restored) else None
    archive: Path | None = None
    disposition: Literal["restored", "archived", "none"] = "none"

    if isinstance(decision, Restore):
        try:
            _restore(journal.service, port, journal.baseline, lock_fd)
        except netproxy.NetworkSetupError as error:
            _red(f"could not restore the previous settings on '{journal.service.name}': {error} — the journal is kept")
            return _outcome(1)
        click.echo(f"✓ {_restored_line(journal)}")
        disposition = "restored"
    elif isinstance(decision, AlreadyRestored):
        click.echo(f"{ui.DIM}the previous settings are already in place on '{journal.service.name}'{ui.R}")
        disposition = "restored"
    elif isinstance(decision, Archive):
        since = _now()
        try:
            archive = sess.archive_record(since, decision.reason, journal)
        except OSError as error:
            _red(f"could not archive this session's baseline ({error}) — the journal is kept")
            return _outcome(1)
        record = Archived(
            version=ownership.SESSION_VERSION,
            since=since,
            reason=decision.reason,
            path=str(archive),
            context=Known(owner=journal.owner, service=journal.service, proxy=proxy),
        )
        if not _checkpoint(sess, record):
            return _outcome(1)
        _red(
            f"the PAC on '{journal.service.name}' is {_why(decision.reason)} — this session's baseline is at {archive}"
        )
        released, reason = _account_and_release(sess, record, scan, spawned=spawned)
        if not released:
            _red(f"the journal is kept: {reason}")
        return _outcome(1, disposition="archived", released=released, archive=archive)
    else:  # pragma: no cover - decide_down answers nothing else after a fence
        raise AssertionError(f"a fenced session decides to {decision}")

    checkpoint = dataclasses.replace(journal, phase=Restored(proxy))
    if not _checkpoint(sess, checkpoint):
        return _outcome(1)
    released, reason = _account_and_release(sess, checkpoint, scan, spawned=spawned)
    if not released:
        _red(f"the previous settings are back, but the journal is kept: {reason}")
        return _outcome(1, disposition=disposition, released=False)
    if not unwinding:
        click.echo(f"{ui.GREEN}stopped{ui.R}")
    return _outcome(0, disposition=disposition, released=True)


def _restored_line(journal: SessionRecord) -> str:
    target = ownership.restore_target(journal.baseline)
    if isinstance(target, Off):
        return f"PAC removed from '{journal.service.name}' — direct networking restored"
    return f"restored the previous PAC on '{journal.service.name}': {target.url}"


def _why(reason: ownership.Reason) -> str:
    return "not this session's any more" if reason == "displaced" else "on a network service that is gone"


def _checkpoint(sess: session.Session, record: SessionRecord | Archived) -> bool:
    try:
        sess.write(record)
    except (OSError, session.Unrepresentable) as error:
        _red(f"the checkpoint could not be written ({error}) — the journal keeps its phase, {_DOWN} again")
        return False
    return True


def _account_and_release(
    sess: session.Session,
    journal: SessionRecord | Archived,
    scan: ownership.Scan,
    *,
    spawned: tuple[tuple[Ref, ownership.Marker], ...],
) -> tuple[bool, str]:
    """Stop what the journal names and what the scan found beside it, wait for the port to go
    quiet, then ask `decide_release` whether the record may go."""
    port = _journal_port(journal)
    proxy_state, watchdog_state = _observe_refs(journal)
    failures: list[str] = []
    for ref, marker in _named_refs(journal):
        state = proxy_state if marker == "proxy" else watchdog_state
        if state is Liveness.PROVEN_DEAD:
            continue
        try:
            outcome = procs.terminate(ref, marker, port or config.CONTROL_PORT)
        except procs.ProcessCheckError as error:
            failures.append(f"the recorded {marker} (pid {ref.pid}) could not be checked: {error}")
            continue
        if outcome is procs.Termination.STILL_RUNNING:
            failures.append(f"the recorded {marker} (pid {ref.pid}) is still running")
    failures += _stop_spawned(spawned)
    failures += _stop_marked(_extras(scan, _excluded_pids(journal, proxy_state, watchdog_state)))
    _report(failures)
    if port is not None:
        _wait_for_quiet(port)

    fresh = procs.scan_marked()
    obs = _observe_release(journal, port, NotObserved(), fresh)
    extra = _extras(fresh, _excluded_pids(journal, obs.proxy, obs.watchdog))
    if extra:
        # One extra pass, not a loop: a scan that finds an orphan stops it and looks once more, and
        # anything still there after that is what `decide_release` keeps the journal for.
        _report(_stop_marked(extra))
        fresh = procs.scan_marked()
        obs = _observe_release(journal, port, NotObserved(), fresh)
    release = ownership.decide_release(obs)
    if isinstance(release, ReleaseNow):
        sess.unlink()
        return True, ""
    return False, release.reason


# MARK: - `down`'s portless rows: the sweep


def _down_portless(
    sess: session.Session,
    lock_fd: int,
    journal: ownership.Journal,
    decision: ownership.Decision,
    scan: ownership.Scan,
    *,
    from_disk: bool,
) -> DownOutcome:
    """`Absent` / `Archived(Unknown)`: no ref to fence, no port to ask, no baseline to restore.

    What is left is a machine-wide sweep — every marked process stopped, every enabled Lyrebird PAC
    switched off once its own port has been proved silent — and `found_any`, a sticky fact about
    what was *observed*, which is what decides between exit 0 and exit 1.
    """
    archived = isinstance(journal, Archived)
    path = Path(journal.path) if isinstance(journal, Archived) else None
    if archived and from_disk and not _barrier(sess):
        return _outcome(1, disposition="archived", archive=path)
    if isinstance(decision, Preserve):
        _red(f"{decision.reason} — nothing was changed")
        return _outcome(1)

    found_any = bool(isinstance(scan, Complete) and scan.found)
    stopped = _extras(scan, set())
    _report(_stop_marked(stopped))

    sweep, left, swept_any = _sweep_services(lock_fd)
    found_any = found_any or swept_any
    found_any = _report_legacy_records() or found_any

    fresh = procs.scan_marked()
    extra = _extras(fresh, set())
    if extra:
        found_any = True
        _report(_stop_marked(extra))
        fresh = procs.scan_marked()

    # Built after the last scan, so `found_any` carries every observation this run made — including
    # one that was gone again by the time anything could act on it.
    result: ownership.SweepResult
    if sweep is not None:
        result = sweep
    elif left:
        result = SweepLeft(tuple(sorted(set(left))))
    else:
        result = SweepClean(found_any)

    release = ownership.decide_release(_observe_release(journal, None, result, fresh))
    released = isinstance(release, ReleaseNow)
    kept = "" if isinstance(release, ReleaseNow) else release.reason
    if released:
        sess.unlink()
    if archived:
        _red(f"this session was archived ({path}); its previous settings were never put back")
        if not released:
            _red(f"the journal is kept: {kept}")
        return _outcome(1, disposition="archived", released=released, archive=path)
    if isinstance(result, SweepClean) and not result.found_any and released:
        click.echo(f"{ui.DIM}nothing to stop — no Lyrebird session, no Lyrebird process and no Lyrebird PAC{ui.R}")
        return _outcome(0, released=True)
    if not released:
        _red(f"the previous settings are unknown: {kept}")
    else:
        _red("something of a Lyrebird was found and dealt with, but the previous settings are unknown")
    return _outcome(1, released=released)


def _sweep_services(lock_fd: int) -> tuple[ownership.SweepResult | None, list[int], bool]:
    """Switch off every enabled Lyrebird PAC whose port is silent. Returns
    (a failure, the ports left enabled, whether anything was found at all)."""
    found_any = False
    left: list[int] = []
    try:
        names = netproxy.list_all_services()
    except netproxy.NetworkSetupError as error:
        return SweepFailed(str(error)), left, found_any
    for name in names:
        try:
            pac = netproxy.pac_status(name)
        except netproxy.NetworkSetupError as error:
            # The services already switched off stay off — each was its own read-back — and nothing
            # else is touched: see test_down_sweep_keeps_the_archive_when_a_service_cannot_be_read.
            return SweepFailed(str(error)), left, found_any
        port = ownership.lyrebird_port(pac.url)
        if port is None or not pac.enabled:
            continue
        found_any = True
        # At most one re-evaluation: a PAC that keeps naming a port this pass has not checked is
        # reported as left rather than switched off unasked.
        for _ in range(2):
            if isinstance(api.observe_health(port).health, Answering):
                click.echo(f"{ui.YELLOW}a Lyrebird answers on port {port} and no session owns it — left alone{ui.R}")
                left.append(port)
                break
            try:
                outcome = _switch_off(name, port, lock_fd)
            except netproxy.NetworkSetupError as error:
                return SweepFailed(str(error)), left, found_any
            if isinstance(outcome, SwitchedOff):
                click.echo(f"✓ switched Lyrebird's PAC off on '{name}'")
                break
            if isinstance(outcome, NothingToSwitchOff):
                break
            port = outcome.port_now
            found_any = True
        else:
            left.append(port)
    return None, left, found_any


def _report_legacy_records() -> bool:
    """Print what a pre-protocol `runtime-*.json` holds, and count it as a finding.

    Printed, never imported: a record written before this protocol says nothing about whether its
    PAC is still installed, and acting on it would restore across a boundary nobody can see — see
    test_down_absent_exits_1_over_a_legacy_runtime_record.
    """
    try:
        legacy = sorted(config.STATE_ROOT.glob("runtime-*.json"))
    except OSError:
        return False
    found = False
    for path in legacy:
        found = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            data = {}
        previous = data.get("previousPac") if isinstance(data, dict) else None
        service = data.get("service") if isinstance(data, dict) else None
        click.echo(
            f"{ui.YELLOW}a record from an older Lyrebird is still here: {path}{ui.R}\n"
            f"   service: {service or 'unknown'}   previous PAC: {previous or 'unknown'}\n"
            f"   it was not imported — put those settings back by hand if they are still wanted, then delete it"
        )
    return found


@click.command()
def down() -> None:
    """Stop the proxy and restore the previous proxy settings.

    From anywhere: no profile, no port and no state directory are needed, because the session
    journal is the authority and there is one per user.
    """
    sess = _session()
    try:
        outcome = down_session(sess, None)
    except session.LockBusy as error:
        raise SystemExit(f"{ui.RED}{error}{ui.R}") from None
    if outcome.exit_code:
        raise SystemExit(outcome.exit_code)


# MARK: - `up`


@dataclass
class _Spawned:
    """The refs this run created. Extra accounting evidence for the unwind — a child the journal
    never recorded is still stopped and proven — never a substitute for the journal."""

    proxy: Ref | None = None
    watchdog: Ref | None = None

    def refs(self) -> tuple[tuple[Ref, ownership.Marker], ...]:
        found: list[tuple[Ref, ownership.Marker]] = []
        if self.proxy is not None:
            found.append((self.proxy, "proxy"))
        if self.watchdog is not None:
            found.append((self.watchdog, "watchdog"))
        return tuple(found)


class _Unwind(Exception):
    """A fresh acquisition met a failure: everything it did comes back off under the same lock."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WatchdogSpawnFailed(RuntimeError):
    """The watchdog did not report itself ready. Carries the ref the parent holds, so the unwind can
    still stop the child a failed wait left running."""

    def __init__(self, reason: str, ref: Ref | None) -> None:
        super().__init__(reason)
        self.ref = ref


def _require_profile() -> None:
    """Read the profile, for the one command that needs its contents.

    `up` is where a malformed profile aborts. Every other command — `down` above all — works from
    the session journal and the live API, so a broken profile.json cannot stop you restoring the
    network.
    """
    config.reload_profile()
    if not config.PROFILE.exists:
        raise SystemExit(
            f"{ui.RED}no profile at {config.PROFILE_DIR}{ui.R}\n"
            f"  create one:   lyrebird init {config.PROFILE_DIR}\n"
            f"  or point at an existing one:  lyrebird --profile /path/to/profile ...\n"
            f"  (a profile is a directory containing profile.json and scenarios/)"
        )
    # Refused, not warned about: with no hosts there is nothing `up` could achieve, and it
    # used to trust the CA, install a DIRECT-only PAC, relaunch the app, print INTERCEPT ACTIVE
    # and exit 0 — every step a success, the postcondition not met. The addon still honours an
    # empty list as "intercept nothing"; this is the command named for intercepting declining
    # to claim it did.
    if not config.INTERCEPT_HOSTS:
        raise SystemExit(
            f"{ui.RED}profile at {config.PROFILE_DIR} lists no hosts — nothing would be intercepted{ui.R}\n"
            f"  add the hostname your app calls to `hosts` in {config.PROFILE_FILE}"
        )


@click.command()
@click.argument("path", type=click.Path(), required=False)
def init(path: str | None) -> None:
    """Create a profile from the bundled examples."""
    target = Path(path).expanduser().resolve() if path else config.PROFILE_DIR
    if (target / "profile.json").exists():
        raise SystemExit(f"{ui.RED}{target}/profile.json already exists — refusing to overwrite{ui.R}")
    # Checked on the target, not on `config.PROFILE_DIR`: `init PATH` writes somewhere else. Copying
    # the examples into a profile that still has `sessions/` would leave two directories of scenarios
    # with only one of them read — see test_init_refuses_a_legacy_sessions_layout.
    try:
        store.refuse_legacy_layout(target)
    except store.LegacyProfileLayout as error:
        raise SystemExit(f"{ui.RED}{error}{ui.R}") from None
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(config.EXAMPLES_DIR, target, dirs_exist_ok=True)
    click.echo(f"✓ profile created at {ui.BOLD}{target}{ui.R}")
    click.echo(
        f"  Edit {target}/profile.json: set `hosts` to the API your app calls, and\n"
        f"  `simBundleId` to your app's bundle identifier. The examples are a schema\n"
        f"  template, not a runnable demo — api.example.com serves none of these paths."
    )
    click.echo(f"  Then:  lyrebird --profile {target} up")


@click.command()
@click.option(
    "--relaunch",
    "bundle_id",
    default=None,
    help="Terminate + relaunch this app bundle id after up (strongly recommended).",
)
@click.option(
    "--no-relaunch",
    is_flag=True,
    default=False,
    help="Launch nothing; the caller starts the app once `up` has exited 0.",
)
@click.option(
    "--use", "use_name", default=None, help="Select this scenario before the app is relaunched, so the launch meets it."
)
@click.option(
    "--simulator",
    "simulator_selector",
    default=None,
    metavar="UDID-OR-NAME",
    help="Which simulator to trust the CA in and relaunch on. Defaults to the booted "
    "one; required when more than one is booted.",
)
def up(bundle_id: str | None, no_relaunch: bool, use_name: str | None, simulator_selector: str | None) -> None:
    """Start the proxy, trust the CA in the simulator, and install the host-scoped PAC.

    With `--use NAME` the scenario is selected — and its sequences rewound — before the app is
    relaunched, so the app's launch requests are answered by that scenario rather than by
    whichever scenario was last active. That is what a `use` afterwards is too late to fix for an
    app that caches its launch response.

    With `--no-relaunch` nothing is launched: pass it when a UI runner owns the app, and start it
    yourself once `up` has exited 0.

    `--simulator` binds the CA and the relaunch to one device, which is what a UI-test runner that
    already picked a simulator needs. It does not confine interception to that device: the PAC is
    installed on a network service and scoped by hostname, so every simulator on this Mac follows
    it for the profile's hosts.
    """
    # Before `_require_profile`, and so before anything is started: "which of these two did you
    # mean" is not a question to ask after the network has been rewired.
    if no_relaunch and bundle_id:
        raise click.UsageError("--relaunch and --no-relaunch contradict each other: pass one.")
    if use_name is not None and not use_name.strip():
        raise click.UsageError("--use needs a scenario name.")

    _require_profile()
    sess = _session()
    sess.ensure_root()
    try:
        with sess.locked() as lock_fd:
            _up_locked(sess, lock_fd, bundle_id, no_relaunch, use_name, simulator_selector)
    except session.LockBusy as error:
        raise SystemExit(f"{ui.RED}{error}{ui.R}") from None


def _up_locked(
    sess: session.Session,
    lock_fd: int,
    bundle_id: str | None,
    no_relaunch: bool,
    use_name: str | None,
    simulator_selector: str | None,
) -> None:
    requested = _owner()
    port = requested.control_port
    journal = sess.read()
    scan = procs.scan_marked()
    # Kept whole: `.health` feeds every decision and the final look, `.raw` is what `--use` reads
    # `scenariosNotWhole` from.
    reading = api.observe_health()

    route: ownership.Route | NotObserved = NotObserved()
    pac: PacClass | ownership.UnownedClass | NotObserved = NotObserved()
    proxy_state: Liveness | NotObserved = NotObserved()
    watchdog_state: Liveness | NotObserved = NotObserved()
    observed: Pac | PacUnreadable | None = None
    service: ServiceRef | None = None

    if isinstance(journal, Absent):
        route = _observe_route()
        if isinstance(route, On):
            service = route.service
            observed = _read_pac(route.service.name)
            pac = ownership.classify_unowned(observed)
    elif isinstance(journal, SessionRecord) and isinstance(journal.phase, Active) and journal.owner == requested:
        route = _observe_route()
        proxy_state, watchdog_state = _observe_refs(journal)
        # The PAC is read only when the route is still on the journalled device: `active_service()`
        # names the *new* route's service, and an unreadable PAC there must not turn a moved-route
        # refusal into a preserve — see test_up_refuses_a_moved_route_even_when_the_new_services_pac_is_unreadable.
        if isinstance(route, On) and route.service.device == journal.service.device:
            service = journal.service
            pac = ownership.classify(_read_pac(route.service.name), journal.owner.control_port, journal.baseline)

    decision = ownership.decide_up(
        UpObs(
            journal=journal,
            requested=requested,
            route=route,
            pac=pac,
            health=reading.health,
            proxy=proxy_state,
            watchdog=watchdog_state,
            scan=scan,
        )
    )
    if isinstance(decision, (ownership.Refuse, Preserve)):
        # Nothing else is observed: `sim._run` has no timeout, and an `up` refused on the journal
        # alone must not hold the session lock for the length of a hanging `simctl` — see
        # test_up_refuses_on_the_journal_without_touching_simctl.
        _red(decision.reason)
        raise SystemExit(1)

    failures: list[str] = []
    try:
        simulator: sim.Simulator | None = sim.resolve_simulator(simulator_selector)
    except sim.SimulatorError as error:
        simulator = None
        _red(f"simulator: {error}")
        click.echo("   the CA was NOT trusted and nothing was relaunched.")
        failures.append(f"no simulator to work on: {str(error).splitlines()[0]}")

    spawned = _Spawned()
    if isinstance(decision, Proceed):
        assert service is not None and isinstance(observed, Pac)  # decide_up proceeds only on a read PAC
        try:
            record, reading = _acquire(sess, lock_fd, requested, service, observed, pac, spawned)
        except _Unwind as unwound:
            _red(unwound.reason)
            _unwind(sess, lock_fd, spawned, port)
        journal = record
    else:
        assert isinstance(journal, SessionRecord)
        if isinstance(decision, RepairOwnPac):
            try:
                _repair(journal.service, port, lock_fd)
            except netproxy.NetworkSetupError as error:
                _red(f"could not switch this session's PAC back on: {error} — the session is kept")
                raise SystemExit(1) from None
            click.echo("✓ this session's PAC was switched back on")
        assert isinstance(journal.phase, Active)  # decide_up is idempotent only over an Active journal
        click.echo(f"{ui.YELLOW}proxy already running{ui.R} (pid {journal.phase.proxy.pid})")

    _up_finish(sess, journal, simulator, bundle_id, no_relaunch, use_name, reading, failures)
    if failures:
        if len(failures) > 1:
            click.echo(f"\n{ui.RED}✗ up did not finish cleanly:{ui.R}")
            for failure in failures:
                click.echo(f"   · {failure.splitlines()[0]}")
        else:
            click.echo(f"{ui.RED}✗ up did not finish cleanly.{ui.R}")
        if isinstance(decision, Proceed):
            # plan-v6: any failure unwinds. A fresh acquisition that could not achieve its
            # postcondition puts the network back rather than leaving a proxy the operator must
            # remember to stop. An idempotent run never does: the obligation is an earlier `up`'s
            # and this one never took it (test_up_idempotent_failure_keeps_active).
            _unwind(sess, lock_fd, spawned, port)
        raise SystemExit(1)


def _acquire(
    sess: session.Session,
    lock_fd: int,
    requested: Owner,
    service: ServiceRef,
    observed: Pac,
    pac: PacClass | ownership.UnownedClass | NotObserved,
    spawned: _Spawned,
) -> tuple[SessionRecord, api.HealthObservation]:
    """Take the PAC: journal the intent, start the proxy, install, start the watchdog, journal it.

    Every failure from the first journal write on raises `_Unwind`, which runs `down`'s own row for
    whatever is on disk.
    """
    port = requested.control_port
    # EMPTY and disabled Lyrebird residue both mean "there was nothing here": one canonical
    # baseline, so the Off target cannot be read back as RESUMABLE forever.
    empty = pac in (ownership.UnownedClass.EMPTY, ownership.UnownedClass.LYREBIRD_DISABLED)
    record = SessionRecord(
        version=ownership.SESSION_VERSION,
        since=_now(),
        simulator=None,
        owner=requested,
        service=service,
        baseline=Pac("", False) if empty else observed,
        phase=Acquiring(None),
    )
    try:
        sess.write(record)
    except session.Unrepresentable as error:
        # Nothing has been started or installed: `write` proves its own reader first, so a PAC that
        # cannot be journalled verbatim refuses acquisition outright — see
        # test_up_refuses_when_the_observed_pac_cannot_be_journalled.
        _red(f"the PAC on '{service.name}' cannot be recorded ({error}) — nothing was started")
        raise SystemExit(1) from None
    except OSError as error:
        raise _Unwind(f"the session journal could not be written ({error})") from None

    try:
        _start_fresh_log()
        with open(config.LOG_FILE, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                _proxy_argv(),
                cwd=str(config.ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=_child_env(),
            )
    except OSError as error:
        raise _Unwind(f"the proxy could not be started ({error})") from None
    try:
        spawned.proxy = procs.ref_of(proc.pid)
    except procs.ProcessCheckError as error:
        click.echo(f"{ui.RED}proxy exited on startup — last log lines:{ui.R}\n{ui._tail_log(20)}")
        raise _Unwind(f"the proxy this run started could not be inspected ({error})") from None
    record = dataclasses.replace(record, phase=Acquiring(spawned.proxy))
    try:
        sess.write(record)
    except (OSError, session.Unrepresentable) as error:
        raise _Unwind(f"the proxy this run started could not be recorded ({error})") from None

    reading = _wait_for_startup(spawned.proxy, port)
    try:
        _install(service, port, observed, lock_fd)
    except netproxy.NetworkSetupError as error:
        raise _Unwind(f"could not install the PAC on '{service.name}': {error}") from None
    click.echo(f"✓ PAC installed on '{service.name}' (configured hosts → proxy, everything else DIRECT)")

    try:
        spawned.watchdog = _spawn_watchdog(port)
    except procs.ProcessCheckError as error:
        raise _Unwind(f"the watchdog could not be inspected ({error})") from None
    except WatchdogSpawnFailed as error:
        spawned.watchdog = error.ref
        raise _Unwind(f"the watchdog did not report itself ready ({error})") from None
    record = dataclasses.replace(record, phase=Active(spawned.proxy, spawned.watchdog))
    try:
        sess.write(record)
    except (OSError, session.Unrepresentable) as error:
        raise _Unwind(f"the session could not be recorded as active ({error})") from None
    return record, reading


def _wait_for_startup(ref: Ref, port: int) -> api.HealthObservation:
    """Wait for *this* proxy to answer on the port. A reading from another pid is the startup port
    race, and adopting it would install a PAC for a proxy this run did not start."""
    deadline = time.time() + _STARTUP_DEADLINE_SECONDS
    while True:
        reading = api.observe_health(port)
        health = reading.health
        if isinstance(health, Answering):
            if health.pid == ref.pid:
                return reading
            raise _Unwind(f"another proxy (pid {health.pid}) answered on port {port} while this one was starting")
        if procs.liveness(ref, "proxy", port) is Liveness.PROVEN_DEAD:
            click.echo(f"{ui.RED}proxy exited on startup — last log lines:{ui.R}\n{ui._tail_log(20)}")
            raise _Unwind("the proxy exited on startup")
        if time.time() >= deadline:
            click.echo(
                f"{ui.RED}proxy did not become healthy in time — last log lines:{ui.R}\n"
                f"{ui._tail_log(20)}\n   full log: {config.LOG_FILE}"
            )
            raise _Unwind("the proxy did not become healthy in time")
        time.sleep(0.3)


def _up_finish(
    sess: session.Session,
    journal: SessionRecord,
    simulator: sim.Simulator | None,
    bundle_id: str | None,
    no_relaunch: bool,
    use_name: str | None,
    reading: api.HealthObservation,
    failures: list[str],
) -> None:
    """The CA, the scenario selection, the relaunch, and the final look."""
    if simulator is not None:
        ca_ok, message = sim.trust_ca_in_sim(simulator)
        click.echo(f"{'✓' if ca_ok else '✗'} CA: {message}")
        if ca_ok:
            # Only now: the journal's `simulator` means "the device holding the CA", and a failed
            # trust must not leave the session bound to a device without one — see
            # test_up_records_the_simulator_only_after_trusting_it.
            record = dataclasses.replace(journal, simulator=ownership.Simulator(simulator.udid, simulator.name))
            with contextlib.suppress(OSError, session.Unrepresentable):
                sess.write(record)
        else:
            failures.append(f"CA not trusted in the simulator: {message}")

    refused = _select_before_relaunch(use_name, reading.raw) if use_name else None
    if refused:
        failures.append(refused)
    elif no_relaunch:
        click.echo("↷ relaunch skipped (--no-relaunch): the caller launches the app")
    elif simulator is not None and (target := (bundle_id or config.PROFILE.sim_bundle_id)):
        launched, detail = sim._relaunch(target, simulator)
        click.echo(f"{'✓' if launched else '✗'} relaunch {target}: {detail}")
        if not launched:
            failures.append(f"could not relaunch {target}: {detail}")
    elif simulator is not None:
        click.echo(
            f"{ui.BOLD}{ui.YELLOW}⚠ RELAUNCH THE APP NOW{ui.R} — URLSession caches the proxy config, so an "
            f"already-running app won't use the PAC until it's relaunched.\n"
            f"   lyrebird relaunch <bundleid>   # on {simulator}, the device this run used\n"
            f"   (or set simBundleId in profile.json and it happens here)"
        )

    _final_look(journal, failures)


def _final_look(journal: SessionRecord, failures: list[str]) -> None:
    """The postcondition itself — this proxy answering, this watchdog alive, this PAC routing to it,
    on the service the route still carries — observed once, at the end. A step that succeeded a
    moment ago is no defence if the observation says otherwise now."""
    port = journal.owner.control_port
    phase = journal.phase
    assert isinstance(phase, Active)
    reading = api.observe_health(port)
    health = reading.health
    if not isinstance(health, Answering):
        _red(f"the proxy stopped answering on port {port} during startup")
        failures.append("the proxy stopped answering during startup")
    else:
        if health.pid != phase.proxy.pid:
            _red(f"the proxy answering on port {port} is now pid {health.pid}, not {phase.proxy.pid}")
            failures.append(f"the port changed hands during startup (pid {phase.proxy.pid} → {health.pid})")
        if health.journal_error is not None:
            # The proxy reads the journal too, and it saying the journal is broken means the `down`
            # this `up` promises cannot restore from it.
            _red(f"the proxy reports the session journal unreadable: {health.journal_error}")
            failures.append("the session journal is unreadable")
        if health.fingerprint is not None and health.fingerprint != journal.owner.profile_fingerprint:
            _red(f"the proxy on port {port} runs profile {health.fingerprint}, not {journal.owner.profile_fingerprint}")
            failures.append("another profile answers on the control port")
    if procs.liveness(phase.watchdog, "watchdog", port) is not Liveness.ALIVE:
        # UNKNOWN is a miss, not a pass: automatic restoration is only promised by a watchdog that
        # was seen to be alive.
        _red(f"the watchdog (pid {phase.watchdog.pid}) is not running — automatic restoration is not in place")
        failures.append("the watchdog is not running")
    service = _observe_service(journal.service)
    cls = _observe_pac(service, port, journal.baseline)
    intercepting = cls is PacClass.OURS_ENABLED
    if not intercepting:
        _red(f"the PAC on '{journal.service.name}' is not routing to this proxy ({_pac_word(cls)})")
        failures.append(f"PAC on '{journal.service.name}' is not routing to the proxy")
    route = _observe_route()
    if isinstance(route, On) and route.service.device != journal.service.device:
        _red(f"the default route moved to '{route.service.name}' during startup")
        failures.append("the default route moved during startup")
    elif not isinstance(route, On):
        _red("the active network service could not be confirmed")
        failures.append("the active network service could not be confirmed")
    if not failures:
        ui._banner(reading.raw, journal.service.name, intercepting)


def _pac_word(cls: PacClass | NotObserved) -> str:
    if isinstance(cls, NotObserved):
        return "the service could not be resolved"
    return {
        PacClass.UNREADABLE: "it could not be read",
        PacClass.OURS_DISABLED: "it is ours but disabled",
        PacClass.RESUMABLE: "it is the previous URL",
        PacClass.AT_TARGET: "it is the previous setting",
        PacClass.FOREIGN: "it is somebody else's",
        PacClass.OURS_ENABLED: "it is ours",
    }[cls]


def _unwind(sess: session.Session, lock_fd: int, spawned: _Spawned, port: int) -> NoReturn:
    """`down`'s own row for the journal *as it is on disk*, run in-process on the lock this `up`
    already holds — with two differences: the refs in `spawned` are additional process-accounting
    evidence, and the exit is always 1.

    The phase on disk is never promoted: a write that failed after `tmp.replace` leaves a record
    this run did not intend, and the unwind runs the matching row for what it finds rather than for
    what it meant to write — see test_up_unwind_follows_the_phase_actually_on_disk_when_a_write_fails_after_replace.
    """
    journal = sess.read()
    if isinstance(journal, Absent):
        # The very first write failed before `tmp.replace`: nothing was spawned and nothing
        # installed, and `down`'s Absent row — the machine-wide sweep — is not an `up`'s to run.
        _red("nothing was started and the network was not touched")
        raise SystemExit(1)
    scan = procs.scan_marked()
    if isinstance(journal, SessionRecord) and isinstance(journal.phase, Acquiring) and journal.phase.proxy is None:
        decision = ownership.decide_down(_observe_down(journal, scan))
        _down_acquiring_nothing(sess, journal, decision, scan, spawned=spawned.refs())
        raise SystemExit(1)
    if not isinstance(journal, SessionRecord) or isinstance(journal.phase, Restored):
        # Only a concurrent writer could produce this; the journal is left exactly as found.
        _red(f"the session journal is no longer this run's to unwind — {_DOWN}")
        raise SystemExit(1)
    decision = ownership.decide_down(_observe_down(journal, scan))
    _down_holding(sess, lock_fd, journal, decision, scan, spawned=spawned.refs(), unwinding=True)
    raise SystemExit(1)


# MARK: - children


def _child_env() -> dict:
    return {**os.environ, "LYREBIRD_PROFILE": str(config.PROFILE_DIR)}


def _watchdog_argv(port: int, ready_fd: int) -> list[str]:
    # `--control-port` immediately after `_watchdog` is what `procs.marked_as` reads back out of
    # the process table, and the only way a watchdog says which session it belongs to.
    return [
        sys.executable,
        str(config.ROOT / "cli.py"),
        "_watchdog",
        "--control-port",
        str(port),
        "--ready-fd",
        str(ready_fd),
    ]


def _proxy_argv() -> list[str]:
    return [
        str(MITMDUMP),
        "--set",
        f"lyrebird_control_port={config.CONTROL_PORT}",  # first, and adjacent: see `procs.marked_as`
        "--listen-host",
        config.PROXY_LISTEN_HOST,
        "--listen-port",
        str(config.PROXY_PORT),
        "--set",
        f"confdir={config.mitmproxy_confdir()}",
        "-s",
        str(config.ROOT / "addon.py"),
    ]


def _spawn_watchdog(port: int) -> Ref:
    """Start the watchdog and return the `Ref` *it reported for itself*.

    The readiness message is the child's own serialised ref rather than a bare byte: the journal
    records that value, so what the child later compares itself against is by construction what it
    sent, and a clock step between two independent reads cannot make them disagree — see
    test_up_journals_the_watchdog_ref_the_child_reports.
    """
    read_fd, write_fd = os.pipe()
    ref: Ref | None = None
    try:
        proc = subprocess.Popen(
            _watchdog_argv(port, write_fd),
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_child_env(),
        )
        # Before the readiness wait, so a wait that fails still knows what to stop.
        ref = procs.ref_of(proc.pid)
        os.close(write_fd)
        write_fd = -1
        reported = _read_ready_line(read_fd)
        if reported is None:
            raise WatchdogSpawnFailed("nothing was read from the readiness pipe", ref)
        if reported.pid != proc.pid:
            raise WatchdogSpawnFailed(f"pid {reported.pid} reported ready, not {proc.pid}", ref)
        return reported
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)
        if write_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(write_fd)


def _read_ready_line(read_fd: int, timeout: float = _READY_TIMEOUT_SECONDS) -> Ref | None:
    deadline = time.time() + timeout
    buffer = b""
    while b"\n" not in buffer:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([read_fd], [], [], remaining)
        if not ready:
            return None
        chunk = os.read(read_fd, 128)
        if not chunk:
            return None
        buffer += chunk
    pid_text, _, rest = buffer.split(b"\n", 1)[0].decode("utf-8", "replace").partition(" ")
    try:
        return Ref(pid=int(pid_text), create_time=float(rest))
    except ValueError:
        return None


def _start_fresh_log() -> None:
    """Begin each run on a new inode rather than truncating the old one.

    The log names every host and path that came through, so it is 0600 — but a log left by an
    older version may be 0644, and `chmod` cannot revoke a descriptor somebody already holds.
    Truncating in place keeps that inode, so a reader who opened it while it was readable goes on
    seeing new traffic. Renaming a fresh private file over it leaves them holding the old one.
    """
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(config.LOG_FILE, "")


# MARK: - scenario selection, shared by `use` and `up --use`


def _activate_scenario(name: str) -> None:
    """Make `name` the active scenario, and report what it displaced.

    Shared by `use` and `up --use` so the two cannot drift into describing the same switch
    differently. Activation is also what rewinds the scenario's sequences, so the scenario starts
    from its first step rather than resuming where the last run left it — which is why `--use` on
    the scenario that is already active is not a no-op.
    """
    result = api._control("/__mock__/scenarios/active", "PUT", {"name": name})
    previous = result.get("previous")
    if previous and previous["name"] != result["active"]:
        click.echo(
            f"switched: {previous['name']} ({previous['overrideCount']} override(s)) → "
            f"{ui.BOLD}{result['active']}{ui.R}"
        )
    else:
        click.echo(f"active: {result['active']}")


def _load_problems_for(health: dict | None, name: str) -> list[str] | None:
    """What stopped scenario `name` loading whole, or None if nobody could say.

    None is not an empty list. A proxy older than this CLI does not report the field at all, and
    reading that silence as "there were none" would let `up --use` relaunch the app against a
    scenario it never checked, while printing what it prints when the scenario is whole.

    The answer comes from `scenariosNotWhole`, which the proxy keys by scenario, rather than from
    the `loadProblems` strings beside it. Those cannot be matched back: a file named
    `orders-outage.json: backup.json` produces `"skipped orders-outage.json: backup.json: …"`,
    which begins exactly like a problem with `orders-outage` — and refusing to launch a scenario
    because a *differently named file* is broken is the same wrong answer in the other direction.
    """
    reported = (health or {}).get("scenariosNotWhole")
    if not isinstance(reported, dict):
        return None
    return [str(problem) for problem in reported.get(name) or []]


def _select_before_relaunch(name: str, health: dict | None) -> str | None:
    """Select `name`, or return the failure that means the app must not be relaunched.

    Three refusals, each of which would otherwise put the app in front of a scenario that is not
    the one the caller named:

    * the running proxy cannot report what loaded — it predates the field. Unchecked is not the
      same as whole, and this is the one refusal a restart fixes outright.
    * the scenario is there but did not load whole — overrides dropped for failing validation, or a
      malformed `default.json` replaced by an empty in-memory `default`. Neither is visible in the
      scenario list and both activate happily, so the PUT is not what can tell you.
    * the PUT was refused — no such scenario, or the proxy stopped answering. `_control` has
      already printed the API's own sentence and turned it into a `SystemExit`.

    Health is the reading `up` already has rather than a fresh one: what a scenario failed to load
    is settled while the store is built and nothing afterwards adds to it, so asking again would
    only widen the window in which the rest of the reading moves under us.
    """
    problems = _load_problems_for(health, name)
    if problems is None:
        click.echo(
            f"{ui.RED}✗ the running proxy is older than this CLI and cannot say whether "
            f"'{name}' loaded whole — the app was NOT relaunched against a scenario "
            f"nothing could check.{ui.R}\n"
            f"   restart the proxy: `lyrebird down && lyrebird up --use {name}`."
        )
        return f"the running proxy did not report whether '{name}' loaded whole"
    if problems:
        detail = "; ".join(problems)
        click.echo(
            f"{ui.RED}✗ scenario '{name}' did not load whole: {detail}{ui.R}\n"
            f"   the app was NOT relaunched — fix the file, then "
            f"`lyrebird down && lyrebird up --use {name}`."
        )
        return f"scenario '{name}' did not load whole: {detail}"

    try:
        _activate_scenario(name)
    except SystemExit:
        # `_control` has already printed why on its way out. What it could not know is what the
        # failure costs here: the relaunch is off, because launching now would put the app in
        # front of some scenario other than the one the caller named.
        known = [str(scenario) for scenario in (health or {}).get("scenarios") or []]
        listing = f"\n   scenarios in this profile: {', '.join(known)}" if known else ""
        click.echo(f"{ui.RED}   could not select '{name}' — the app was NOT relaunched.{ui.R}{listing}")
        return f"could not select scenario '{name}'"
    return None


# MARK: - `status`


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable state on stdout.")
def status(as_json: bool) -> None:
    """Show intercept state (honest about whether the PAC is actually enabled).

    The exit code reports the state, not the formatting: 0 only when the proxy is up *and*
    intercepting *this* profile, whichever way you asked. `--json` changes what is printed and
    never what it means — a flag that decides how output is rendered must not also decide what
    success is, or `lyrebird status && …` silently proceeds against a proxy that is mocking
    nothing.

    It answers for the *requested* owner — this profile, this port — and reports the journal
    beside it: a closed control port is exit 1 whatever the journal says, which is what the CI
    launcher check and the subprocess test both depend on.
    """
    # One fetch. A status taken from one reading and fields printed from another can disagree about
    # which proxy answered — see test_status_output_and_exit_code_come_from_one_reading.
    reading = api.observe_health(config.CONTROL_PORT)
    health, raw = reading.health, reading.raw
    answering = isinstance(health, Answering)
    running = health.fingerprint if isinstance(health, Answering) else None
    foreign = bool(running) and running != config.PROFILE_FINGERPRINT

    journal = _session().read()
    journal_error = journal.reason if isinstance(journal, Unreadable) else None
    if journal_error is None and not foreign and isinstance(health, Answering):
        journal_error = health.journal_error

    # The route is read once and used twice — for the service to report when no journal names one,
    # and for "has the route moved off the journalled device" below.
    route = _observe_route()
    service_ref = _reported_service(journal)
    service: str | None = None
    pac_error: str | None = None
    if service_ref is not None:
        resolved = _observe_service(service_ref)
        if isinstance(resolved, Present):
            service = resolved.name
        else:
            service, pac_error = service_ref.name, _service_problem(resolved, service_ref)
    elif isinstance(route, On):
        service = route.service.name
    elif isinstance(route, ownership.RouteFailed):
        pac_error = route.reason
    elif isinstance(route, ownership.RouteAmbiguous):
        pac_error = "two network services carry the device holding the default route"

    pac: Pac | None = None
    if service is not None and pac_error is None:
        found = _read_pac(service)
        if isinstance(found, PacUnreadable):
            pac_error = found.reason
        else:
            pac = found
    ours = pac is not None and pac.url == ownership.our_url(config.CONTROL_PORT)
    intercepting = pac is not None and pac.enabled and ours and not foreign

    reasons = _status_reasons(journal, health, route, pac, foreign, journal_error)
    simulator = _reported_simulator(journal)
    if as_json:
        mine = {} if foreign else (raw or {})
        click.echo(
            json.dumps(
                {
                    "proxyUp": answering,
                    "intercepting": intercepting,
                    "profileMismatch": foreign,
                    "profileFingerprint": config.PROFILE_FINGERPRINT,
                    "runningProfileFingerprint": running,
                    "pacError": pac_error,
                    # Renamed from `runtimeError`: the per-port record it named is gone, and this is
                    # the session journal — reported from the local read *and* from the proxy's own.
                    "journalError": journal_error,
                    "activeScenario": mine.get("activeScenario"),
                    "overrideCount": mine.get("overrideCount"),
                    "scenarios": None if foreign else (raw or {}).get("scenarios", []),
                    # `null` when the running engine did not supply the field, never `[]`. A current
                    # engine always sends both, with one entry per rule — so `[]` is a real state ("no
                    # rules here") and a missing key is a capability signal ("this proxy cannot tell
                    # you"). Defaulting to `[]` collapsed those into the claim that nothing has answered.
                    "sequences": mine.get("sequences"),
                    "answers": mine.get("answers"),
                    "simBundleId": mine.get("simBundleId"),
                    "profile": str(config.PROFILE_DIR),
                    "service": service,
                    "simulator": simulator,
                    "pac": {"url": pac.url, "enabled": pac.enabled, "ours": ours} if pac else None,
                    "session": _session_json(journal),
                },
                indent=2,
            )
        )
    else:
        click.echo(f"{ui.DIM}profile: {config.PROFILE_DIR}{ui.R}")
        if foreign:
            click.echo(
                f"{ui.BOLD}{ui.YELLOW}🟠 PROXY UP, ANOTHER PROFILE{ui.R} — nothing is intercepting for this profile."
            )
            click.echo(api._profile_mismatch(str(running)))
        elif pac_error and answering:
            where = f" on '{service}'" if service else ""
            click.echo(
                f"{ui.BOLD}{ui.YELLOW}🟠 PROXY UP, PAC UNREADABLE{ui.R} — could not read the PAC{where}: {pac_error}"
            )
        else:
            ui._banner(raw, service, intercepting)
        if raw and not foreign:
            click.echo(f"  scenarios: {', '.join(raw['scenarios'])}")
            for state in raw.get("sequences", []):
                position = (
                    f"next step {state['nextStep']}/{state['stepCount']}"
                    if state["nextStep"]
                    else f"{ui.RED}exhausted{ui.R}"
                )
                overrun = f" {ui.YELLOW}· overrun{ui.R}" if state["hasOverrun"] else ""
                trigger = "own calls" if state["advanceOn"] == "self" else "advanceOn"
                click.echo(f"  sequence {state['id']}: {position} · {trigger}{overrun}")
        if pac is not None:
            state = "enabled" if pac.enabled else f"{ui.RED}DISABLED{ui.R}"
            owner = "" if ours or not pac.url else " · not ours"
            click.echo(f"  PAC on '{service}': {pac.url or '(none)'} · {state}{owner}")
        for reason in reasons:
            click.echo(f"  {ui.RED}✗ {reason}{ui.R}")
        if simulator:
            click.echo(
                f"  simulator: {simulator.get('name')} ({simulator.get('udid')}) "
                f"{ui.DIM}· CA + relaunch only; the PAC is not scoped to it{ui.R}"
            )

    raise SystemExit(0 if answering and intercepting and not reasons else 1)


def _reported_service(journal: ownership.Journal) -> ServiceRef | None:
    if isinstance(journal, SessionRecord):
        return journal.service
    if isinstance(journal, Archived) and isinstance(journal.context, Known):
        return journal.context.service
    return None


def _service_problem(resolved: ownership.Service, ref: ServiceRef) -> str:
    if isinstance(resolved, ownership.ServiceFailed):
        return resolved.reason
    if isinstance(resolved, ownership.ServiceAmbiguous):
        return f"two network services carry device '{ref.device}'"
    return f"no network service carries device '{ref.device}' any more"


def _reported_simulator(journal: ownership.Journal) -> dict | None:
    if isinstance(journal, SessionRecord) and journal.simulator is not None:
        return {"udid": journal.simulator.udid, "name": journal.simulator.name}
    return None


def _session_json(journal: ownership.Journal) -> dict:
    owner = None
    if isinstance(journal, SessionRecord):
        owner = {"controlPort": journal.owner.control_port, "profileFingerprint": journal.owner.profile_fingerprint}
    elif isinstance(journal, Archived) and isinstance(journal.context, Known):
        context = journal.context
        owner = {"controlPort": context.owner.control_port, "profileFingerprint": context.owner.profile_fingerprint}
    ref = _reported_service(journal)
    return {
        "phase": ownership.phase_word(journal),
        "owner": owner,
        "service": None if ref is None else {"name": ref.name, "device": ref.device},
        "watchdog": procs.watchdog_word(journal),
        "archive": journal.path if isinstance(journal, Archived) else None,
    }


def _status_reasons(
    journal: ownership.Journal,
    health: ownership.Health,
    route: ownership.Route,
    pac: Pac | None,
    foreign: bool,
    journal_error: str | None,
) -> list[str]:
    """Every reason this `status` is not exit 0, in the order a person would act on them."""
    reasons: list[str] = []
    if foreign:
        reasons.append(f"another profile answers on port {config.CONTROL_PORT}")
    if journal_error:
        reasons.append(f"session journal: {journal_error} — `down` cannot restore from it")
    if isinstance(journal, Archived):
        reasons.append(f"archived: {journal.path}")
        return reasons
    if not isinstance(journal, SessionRecord):
        reasons.append("no session holds the PAC — `lyrebird up`")
        return reasons
    if journal.owner != _owner():
        reasons.append(
            f"another session owns the PAC: port {journal.owner.control_port}, "
            f"profile {journal.owner.profile_fingerprint}"
        )
        return reasons
    phase = journal.phase
    if not isinstance(phase, Active):
        reasons.append(f"the session is {ownership.phase_word(journal)} — {_DOWN}")
        return reasons
    port = journal.owner.control_port
    if not (isinstance(health, Answering) and health.pid == phase.proxy.pid):
        reasons.append(f"the recorded proxy (pid {phase.proxy.pid}) is not what answers on port {port}")
    elif procs.liveness(phase.proxy, "proxy", port) is not Liveness.ALIVE:
        reasons.append(f"the recorded proxy (pid {phase.proxy.pid}) could not be proved alive")
    if procs.liveness(phase.watchdog, "watchdog", port) is not Liveness.ALIVE:
        reasons.append("watchdog dead — automatic restoration lost: `lyrebird down && lyrebird up`")
    if pac is None or pac.url != ownership.our_url(port) or not pac.enabled:
        reasons.append(f"the PAC on '{journal.service.name}' is not routing to this session")
    if isinstance(route, On):
        if route.service.device != journal.service.device:
            reasons.append(f"route moved to {route.service.device} — `lyrebird down && lyrebird up`")
    else:
        reasons.append("the active network service could not be confirmed")
    return reasons


@click.command()
def logs() -> None:
    """Print the last 60 lines of the proxy log (not a follow — use `tail -f` on the path shown)."""
    click.echo(ui._tail_log(60))
    click.echo(f"{ui.DIM}{config.LOG_FILE}{ui.R}", err=True)


# MARK: - the watchdog


@click.command(name="_watchdog", hidden=True)
@click.option("--control-port", type=int, required=True, help="The session this watchdog serves; also read from `ps`.")
@click.option("--ready-fd", type=int, default=None, help="Write this process's own Ref here, then close it.")
def watchdog(control_port: int, ready_fd: int | None) -> None:
    me = procs.self_ref()
    sess = _session()
    sess.ensure_root()
    if ready_fd is not None:
        # The parent journals *this* value, so what the loop below compares itself against is by
        # construction what it sent.
        os.write(ready_fd, f"{me.pid} {me.create_time!r}\n".encode())
        os.close(ready_fd)
    watchdog_loop(sess, control_port, me)


def watchdog_loop(sess: session.Session, port: int, me: Ref) -> None:
    """One decision per tick, and the lock is never held across the sleep.

    A watchdog that retired over contention would leave the session unwatched, so the lock is taken
    without a timeout; a restore that keeps failing gives up after `_WATCHDOG_RESTORE_ATTEMPTS`
    *consecutive* ticks, leaving the journal `Active` for `down`.
    """
    failed_restores = 0
    while True:
        with sess.locked(timeout=None) as lock_fd:
            journal = sess.read()
            health = api.observe_health(port).health
            watching = isinstance(journal, SessionRecord) and isinstance(journal.phase, Active)
            service: ownership.Service | NotObserved = NotObserved()
            pac: PacClass | NotObserved = NotObserved()
            proxy: Liveness | NotObserved = NotObserved()
            if watching:
                assert isinstance(journal, SessionRecord) and isinstance(journal.phase, Active)
                service = _observe_service(journal.service)
                pac = _observe_pac(service, port, journal.baseline)
                proxy = procs.liveness(journal.phase.proxy, "proxy", port)
            decision = ownership.decide_watchdog(
                WatchdogObs(journal=journal, me=me, service=service, pac=pac, health=health, proxy=proxy)
            )
            if isinstance(decision, ownership.Exit):
                return
            assert isinstance(journal, SessionRecord) and isinstance(journal.phase, Active)
            if isinstance(decision, RepairOwnPac):
                with contextlib.suppress(netproxy.NetworkSetupError):
                    _repair(journal.service, port, lock_fd)
            elif isinstance(decision, Restore):
                try:
                    _restore(journal.service, port, journal.baseline, lock_fd)
                except netproxy.NetworkSetupError:
                    failed_restores += 1
                    if failed_restores >= _WATCHDOG_RESTORE_ATTEMPTS:
                        # The journal stays `Active`: it is the only description of what to put
                        # back, and `down` is what acts on it now.
                        return
                else:
                    assert isinstance(proxy, Liveness)  # observed whenever the journal is Active
                    _watchdog_release(sess, journal, port, me, proxy)
                    return
            if not isinstance(decision, Restore):
                failed_restores = 0
        time.sleep(_WATCHDOG_POLL_SECONDS)


def _watchdog_release(sess: session.Session, journal: SessionRecord, port: int, me: Ref, proxy: Liveness) -> None:
    """Checkpoint the restore, stop every orphan, and release the journal if nothing is left.

    The recorded proxy is never signalled here — alive-but-silent is kept in `Restored(ref)` for
    `down` — and a scan must never re-authorise signalling a pid whose persisted ref is ALIVE or
    UNKNOWN: see test_watchdog_never_signals_the_recorded_pid_through_a_fresh_scan_ref.
    """
    phase = journal.phase
    assert isinstance(phase, Active)
    checkpoint = dataclasses.replace(
        journal, phase=Restored(phase.proxy if proxy is not Liveness.PROVEN_DEAD else None)
    )
    try:
        sess.write(checkpoint)
    except (OSError, session.Unrepresentable):
        return
    excluded = {me.pid}
    if proxy is not Liveness.PROVEN_DEAD:
        excluded.add(phase.proxy.pid)
    # `exclude=me.pid` states the releasing-actor exemption rather than relying on `scan_marked`'s
    # default: the watchdog asking whether anything is left must not find itself and keep the
    # journal for its own sake — see test_watchdog_stops_a_marked_orphan_before_releasing.
    scan = procs.scan_marked(exclude=me.pid)
    failures = _stop_marked(_extras(scan, excluded))
    if failures or isinstance(scan, Incomplete):
        return
    fresh = procs.scan_marked(exclude=me.pid)
    obs = _observe_release(checkpoint, port, NotObserved(), fresh)
    if isinstance(ownership.decide_release(obs), ReleaseNow):
        sess.unlink()
