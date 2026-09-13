"""The psutil adapter: the only module that imports psutil, and the only place a pid becomes a fact.

Identity here is **marker + control port + create_time**, all three, read from one `psutil.Process`
instance and signalled through that same instance. A pid alone is a number the kernel hands out
again; a marker alone is shared by every Lyrebird on the machine; a create time alone says nothing
about what the process is. `down` killing "the pid in the record" is the failure this exists to
make impossible.

Two verified psutil behaviours shape it:

* `Process.terminate()` re-checks `(pid, create_time)` against the identity the *instance* was
  built with, so inspection and signal must share an instance;
* on macOS a zombie's `create_time()` and `status()` still read while `cmdline()` raises, so "it
  answered" is never taken as "it is alive" — `status()` is asked explicitly.

**Error normalisation.** psutil's macOS layer raises a plain `OSError` for syscall failures its
wrapper does not translate, from any call. Every entry point here catches `(psutil.Error, OSError)`
and answers in its own vocabulary — `UNKNOWN` or `ProcessCheckError` — so a caller that met ENOMEM
inspecting the proxy cannot mistake it for a process that is gone
(test_procs_normalises_plain_oserror_at_every_entry).
"""

from __future__ import annotations

import os
from enum import Enum

import psutil

from ownership import Ref


class ProcessCheckError(RuntimeError):
    """The check itself failed. Not an answer about the process — callers must not act on it."""


class Termination(Enum):
    NOT_RUNNING = "not_running"
    STOPPED = "stopped"
    STILL_RUNNING = "still_running"


class Liveness(Enum):
    ALIVE = "alive"
    PROVEN_DEAD = "proven_dead"
    UNKNOWN = "unknown"


def is_proxy_on(cmdline: list[str], port: int) -> bool:
    """Whether this argv is a Lyrebird proxy serving `port`.

    Exact list tokens, never a regex over a joined string: a service or a state directory can be
    named anything, including the option's own text — see
    test_is_proxy_on_reads_real_tokens_not_words_in_a_name.
    """
    wanted = f"lyrebird_control_port={port}"
    for index, token in enumerate(cmdline):
        if token == "--set" and index + 1 < len(cmdline) and cmdline[index + 1] == wanted:
            # The port token alone is not a proxy: `mitmdump --set lyrebird_control_port=…` says
            # nothing about what is being run. The addon is what makes it ours.
            return any(os.path.basename(other) == "addon.py" for other in cmdline)
    return False


def ref_of(pid: int) -> Ref:
    """The `Ref` for a process that is running now.

    A child that died the instant it was spawned is *reported*, not recorded: a `Ref` minted from a
    create time we could not read would name a pid and nothing else — see
    test_ref_of_refuses_a_process_that_is_already_gone.
    """
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            raise ProcessCheckError(f"pid {pid} is a zombie")
        return Ref(pid=pid, create_time=process.create_time())
    except (psutil.Error, OSError) as error:
        raise ProcessCheckError(f"could not read pid {pid}: {error}") from None


def _inspect(process: psutil.Process, ref: Ref, port: int) -> Liveness:
    """What one `psutil.Process` instance proves about `ref`.

    The order matters: a zombie reads as PROVEN_DEAD before anything else, because its create time
    still answers on macOS and would otherwise look alive.
    """
    try:
        if process.status() == psutil.STATUS_ZOMBIE:
            return Liveness.PROVEN_DEAD
        create_time = process.create_time()
        marked = is_proxy_on(process.cmdline(), port)
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return Liveness.PROVEN_DEAD
    except (psutil.Error, OSError):
        # AccessDenied and every unmapped syscall failure: the check did not happen, and doubt
        # acts on nothing.
        return Liveness.UNKNOWN
    if not marked:
        # The argv read and it is not ours: this pid belongs to something else now.
        return Liveness.PROVEN_DEAD
    if create_time != ref.create_time:
        # Still marked, different create time: a clock step (psutil adjusts create times when the
        # boot time moves) and a same-port reuse are indistinguishable, and neither is proof — see
        # test_inspect_reads_a_marked_process_with_another_create_time_as_unknown.
        return Liveness.UNKNOWN
    return Liveness.ALIVE


def liveness(ref: Ref, port: int) -> Liveness:
    try:
        process = psutil.Process(ref.pid)
    except psutil.NoSuchProcess:
        return Liveness.PROVEN_DEAD
    except (psutil.Error, OSError):
        return Liveness.UNKNOWN
    return _inspect(process, ref, port)


def _open(ref: Ref, port: int) -> psutil.Process | None:
    """The instance every signal in this module goes through, once it has proved the identity.

    None means "not running"; `ProcessCheckError` means "we do not know", which is never a reason
    to signal.
    """
    try:
        process = psutil.Process(ref.pid)
    except psutil.NoSuchProcess:
        return None
    except (psutil.Error, OSError) as error:
        raise ProcessCheckError(f"could not read pid {ref.pid}: {error}") from None
    state = _inspect(process, ref, port)
    if state is Liveness.PROVEN_DEAD:
        return None
    if state is Liveness.UNKNOWN:
        raise ProcessCheckError(f"could not confirm that pid {ref.pid} is this session's proxy")
    return process


def _settled(process: psutil.Process, ref: Ref, port: int) -> Termination | None:
    """After a wait timed out: STOPPED once the same instance proves it is gone, None if it is still
    there. A non-child zombie makes `wait()` time out, which is why this is asked again at all."""
    state = _inspect(process, ref, port)
    if state is Liveness.PROVEN_DEAD:
        return Termination.STOPPED
    if state is Liveness.UNKNOWN:
        raise ProcessCheckError(f"could not confirm what happened to pid {ref.pid}")
    return None


def terminate(ref: Ref, port: int, *, term_wait: float = 5.0, kill_wait: float = 2.0) -> Termination:
    """SIGTERM, then SIGKILL, then say what is true — never "stopped" over a process still running."""
    process = _open(ref, port)
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
    settled = _settled(process, ref, port)
    if settled is not None:
        return settled
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
    settled = _settled(process, ref, port)
    return Termination.STILL_RUNNING if settled is None else settled
