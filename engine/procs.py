"""The psutil adapter: the only module that imports psutil, and the only place a pid becomes a fact.

Identity here is **marker + port + create_time**, all three, read from one `psutil.Process`
instance and signalled through that same instance. A pid alone is a number the kernel hands out
again; a marker alone is shared by every Lyrebird on the machine; a create time alone says nothing
about what the process is. `down` killing "the pid in the record" is the failure this exists to
make impossible.

Two verified psutil behaviours shape it:

* `Process.terminate()` re-checks `(pid, create_time)` against the identity the *instance* was
  built with, so inspection and signal must share an instance;
* on macOS a zombie's `create_time()`, `uids()` and `status()` still read while `cmdline()` raises,
  so "it answered" is never taken as "it is alive" — `status()` is asked explicitly.

**Error normalisation.** psutil's macOS layer raises a plain `OSError` for syscall failures its
wrapper does not translate, from any call. Every entry point here catches `(psutil.Error, OSError)`
and answers in its own vocabulary — `UNKNOWN`, `Incomplete`, `ProcessCheckError` — so no
observation can escape the executors' mapping: a watchdog tick that met ENOMEM inspecting its proxy
must still reach `decide_watchdog` with proxy UNKNOWN and restore
(test_procs_normalises_plain_oserror_at_every_entry).
"""

from __future__ import annotations

import os
from enum import Enum

import psutil

from ownership import (
    Active,
    Complete,
    Incomplete,
    Journal,
    Liveness,
    Marked,
    Marker,
    Ref,
    Scan,
    SessionRecord,
    Unreadable,
)


class ProcessCheckError(RuntimeError):
    """The check itself failed. Not an answer about the process — callers must not act on it."""


class Termination(Enum):
    NOT_RUNNING = "not_running"
    STOPPED = "stopped"
    STILL_RUNNING = "still_running"


def marked_as(cmdline: list[str]) -> tuple[Marker, int] | None:
    """Which Lyrebird process this argv belongs to, and on which port — or None.

    Exact list tokens, never a regex over a joined string: a service or a state directory can be
    named anything, including the option's own text — see
    test_marked_as_reads_real_tokens_not_words_in_a_name.
    """
    tokens = list(cmdline)
    for index, token in enumerate(tokens):
        if token == "--set" and index + 1 < len(tokens):
            value = tokens[index + 1]
            if value.startswith("lyrebird_control_port="):
                port = _port(value.partition("=")[2])
                # The port token alone is not a proxy: `mitmdump --set lyrebird_control_port=…`
                # says nothing about what is being run. The addon is what makes it ours.
                if port is not None and any(os.path.basename(other) == "addon.py" for other in tokens):
                    return ("proxy", port)
    for index, token in enumerate(tokens):
        if token == "_watchdog":
            for offset in range(index + 1, len(tokens) - 1):
                if tokens[offset] == "--control-port":
                    port = _port(tokens[offset + 1])
                    if port is not None:
                        return ("watchdog", port)
    return None


def _port(text: str) -> int | None:
    if not text.isdigit():
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def _process(pid: int) -> psutil.Process:
    return psutil.Process(pid)


def ref_of(pid: int) -> Ref:
    """The `Ref` for a process that is running now.

    A child that died the instant it was spawned is *reported*, not recorded: a `Ref` minted from a
    create time we could not read would name a pid and nothing else — see
    test_ref_of_refuses_a_process_that_is_already_gone.
    """
    try:
        process = _process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            raise ProcessCheckError(f"pid {pid} is a zombie")
        return Ref(pid=pid, create_time=process.create_time())
    except (psutil.Error, OSError) as error:
        raise ProcessCheckError(f"could not read pid {pid}: {error}") from None


def self_ref() -> Ref:
    """This process's own `Ref` — the watchdog's `me`, and what `ref_of` must agree with."""
    return ref_of(os.getpid())


def _inspect(process: psutil.Process, ref: Ref, marker: Marker, port: int) -> Liveness:
    """What one `psutil.Process` instance proves about `ref`.

    The order matters: a zombie reads as PROVEN_DEAD before anything else, because its create time
    still answers on macOS and would otherwise look alive.
    """
    try:
        if process.status() == psutil.STATUS_ZOMBIE:
            return Liveness.PROVEN_DEAD
        create_time = process.create_time()
        marked = marked_as(process.cmdline())
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return Liveness.PROVEN_DEAD
    except (psutil.Error, OSError):
        # AccessDenied and every unmapped syscall failure: the check did not happen, and doubt
        # acts on nothing.
        return Liveness.UNKNOWN
    if marked != (marker, port):
        # The argv read and it is not ours: this pid belongs to something else now.
        return Liveness.PROVEN_DEAD
    if create_time != ref.create_time:
        # Still marked, different create time: a clock step (psutil adjusts create times when the
        # boot time moves) and a same-port reuse are indistinguishable, and neither is proof — see
        # test_inspect_reads_a_marked_process_with_another_create_time_as_unknown.
        return Liveness.UNKNOWN
    return Liveness.ALIVE


def liveness(ref: Ref, marker: Marker, port: int) -> Liveness:
    try:
        process = _process(ref.pid)
    except psutil.NoSuchProcess:
        return Liveness.PROVEN_DEAD
    except (psutil.Error, OSError):
        return Liveness.UNKNOWN
    return _inspect(process, ref, marker, port)


def _open(ref: Ref, marker: Marker, port: int) -> psutil.Process | None:
    """The instance every signal in this module goes through, once it has proved the identity.

    None means "not running"; `ProcessCheckError` means "we do not know", which is never a reason
    to signal.
    """
    try:
        process = _process(ref.pid)
    except psutil.NoSuchProcess:
        return None
    except (psutil.Error, OSError) as error:
        raise ProcessCheckError(f"could not read pid {ref.pid}: {error}") from None
    state = _inspect(process, ref, marker, port)
    if state is Liveness.PROVEN_DEAD:
        return None
    if state is Liveness.UNKNOWN:
        raise ProcessCheckError(f"could not confirm that pid {ref.pid} is this session's {marker}")
    return process


def _settled(process: psutil.Process, ref: Ref, marker: Marker, port: int) -> Termination | None:
    """After a wait timed out: STOPPED once the same instance proves it is gone, None if it is still
    there. A non-child zombie makes `wait()` time out, which is why this is asked again at all."""
    state = _inspect(process, ref, marker, port)
    if state is Liveness.PROVEN_DEAD:
        return Termination.STOPPED
    if state is Liveness.UNKNOWN:
        raise ProcessCheckError(f"could not confirm what happened to pid {ref.pid}")
    return None


def terminate(ref: Ref, marker: Marker, port: int, *, term_wait: float = 5.0, kill_wait: float = 2.0) -> Termination:
    """SIGTERM, then SIGKILL, then say what is true — never "stopped" over a process still running."""
    process = _open(ref, marker, port)
    if process is None:
        return Termination.NOT_RUNNING
    try:
        process.terminate()
        process.wait(term_wait)
        return Termination.STOPPED
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return Termination.STOPPED
    except psutil.TimeoutExpired:
        pass
    except (psutil.Error, OSError) as error:
        raise ProcessCheckError(f"could not send SIGTERM to pid {ref.pid}: {error}") from None
    settled = _settled(process, ref, marker, port)
    if settled is not None:
        return settled
    return _kill(process, ref, marker, port, kill_wait)


def kill_now(ref: Ref, marker: Marker, port: int, *, kill_wait: float = 2.0) -> Termination:
    """An identity-checked immediate SIGKILL, for the one caller that must make a proxy die without
    asking it to: a pid that was reused since the record was written receives nothing
    (test_kill_now_never_signals_a_reused_pid)."""
    process = _open(ref, marker, port)
    if process is None:
        return Termination.NOT_RUNNING
    return _kill(process, ref, marker, port, kill_wait)


def _kill(process: psutil.Process, ref: Ref, marker: Marker, port: int, kill_wait: float) -> Termination:
    try:
        process.kill()
        process.wait(kill_wait)
        return Termination.STOPPED
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        # The exit race between the inspection above and the signal — see
        # test_terminate_reports_stopped_when_the_process_exits_before_kill.
        return Termination.STOPPED
    except psutil.TimeoutExpired:
        pass
    except (psutil.Error, OSError) as error:
        raise ProcessCheckError(f"could not send SIGKILL to pid {ref.pid}: {error}") from None
    settled = _settled(process, ref, marker, port)
    return Termination.STILL_RUNNING if settled is None else settled


def scan_marked(*, exclude: int | None = None) -> Scan:
    """Every marked Lyrebird process of this user, or `Incomplete`.

    Fresh `Process` instances over `psutil.pids()`, never `process_iter`: that caches instances
    across calls, and in the long-lived watchdog a reused pid would keep the identity of the
    process it replaced.

    `Incomplete` is a claim about the *scan*, not about the machine: read as "nothing found", an
    `AccessDenied` on one of this user's pids would let `up` acquire over a session that is still
    running (test_scan_is_incomplete_when_enumeration_fails).
    """
    mine = os.getuid()
    skip = os.getpid() if exclude is None else exclude
    try:
        pids = psutil.pids()
    except (psutil.Error, OSError):
        return Incomplete()
    found: list[Marked] = []
    incomplete = False
    for pid in pids:
        if pid == skip:
            continue
        try:
            process = _process(pid)
            if process.uids().real != mine:
                # Filtered before `cmdline()`, which raises AccessDenied for other users' processes
                # on macOS: only a same-UID refusal is evidence that the scan missed something.
                continue
            create_time = process.create_time()
            cmdline = process.cmdline()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue  # an exit race, not a gap: the process is gone
        except (psutil.Error, OSError):
            incomplete = True
            continue
        marked = marked_as(cmdline)
        if marked is not None:
            # The argv is kept verbatim: it is the evidence the acceptance harness picks this run's
            # proxies out by, and this scan is the only place it can come from.
            found.append(
                Marked(
                    ref=Ref(pid=pid, create_time=create_time), kind=marked[0], port=marked[1], cmdline=tuple(cmdline)
                )
            )
    if incomplete:
        return Incomplete()
    return Complete(tuple(found))


def watchdog_word(journal: Journal) -> str:
    """What `/health` and `status` say about automatic restoration: `alive|dead|unknown` about a
    recorded ref, and `none` only for a *decoded* phase that provably owns none.

    `Unreadable` is `unknown`, not `none`: a corrupt `session.json` may be an `Active` record whose
    watchdog is alive, and `none` would assert a fact nobody observed and silence the
    lost-restoration warning — see test_health_session_field_for_every_phase_and_on_timeout.
    """
    if isinstance(journal, Unreadable):
        return "unknown"
    if isinstance(journal, SessionRecord) and isinstance(journal.phase, Active):
        state = liveness(journal.phase.watchdog, "watchdog", journal.owner.control_port)
        return {Liveness.ALIVE: "alive", Liveness.PROVEN_DEAD: "dead", Liveness.UNKNOWN: "unknown"}[state]
    return "none"
