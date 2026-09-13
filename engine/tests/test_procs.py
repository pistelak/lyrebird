"""The psutil adapter: what a pid proves, and what it never proves.

Two layers. Over `FakePsutil`, every branch including the ones a real machine will not produce on
demand — an `AccessDenied`, a plain `OSError` out of a syscall psutil does not translate, a pid
reused between the record and the signal. Over real child processes, the cases a double cannot
argue: a process that ignores SIGTERM, one that has already exited, and a live child whose pid is
paired with a create time that is not its own.

The rule under all of it: a check that could not be made is `UNKNOWN` or `ProcessCheckError`, never
"not running" and never "not ours" — those are the answers `down` prints "stopped" on.
"""

import errno
import os
import subprocess
import sys

import psutil
import pytest

import procs
from cli_doubles import FakeProc, FakePsutil
from ownership import Complete, Incomplete, Liveness, Ref
from procs import ProcessCheckError, Termination

PORT = 8088
PROXY_ARGV = ["mitmdump", "--set", f"lyrebird_control_port={PORT}", "-s", "/path/to/addon.py"]
WATCHDOG_ARGV = ["python", "cli.py", "_watchdog", "--control-port", str(PORT), "--ready-fd", "5"]
REF = Ref(pid=101, create_time=1000.5)


def table(monkeypatch, processes=None, **kwargs):
    fake = FakePsutil(processes or {}, **kwargs)
    monkeypatch.setattr(procs, "psutil", fake)
    return fake


def proxy(**kwargs):
    kwargs.setdefault("cmdline", PROXY_ARGV)
    kwargs.setdefault("create_time", REF.create_time)
    return FakeProc(**kwargs)


# MARK: - marked_as


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (PROXY_ARGV, ("proxy", PORT)),
        (["mitmdump", "--set", f"lyrebird_control_port={PORT}", "-s", "addon.py"], ("proxy", PORT)),
        (WATCHDOG_ARGV, ("watchdog", PORT)),
        (["mitmdump", "-s", "/path/to/addon.py"], None),
        (["mitmdump", "--set", f"lyrebird_control_port={PORT}", "-s", "/path/to/other.py"], None),
        (["python", "cli.py", "_watchdog"], None),
        (["python", "cli.py", "status"], None),
        (["mitmdump", "--set", "lyrebird_control_port=notaport", "-s", "addon.py"], None),
    ],
    ids=[
        "a proxy",
        "a proxy started from a relative path",
        "a watchdog",
        "a proxy with no port token",
        "the port token without our addon",
        "a watchdog with no port",
        "something else entirely",
        "a port that is not a number",
    ],
)
def test_marked_as_reads_the_argv_tokens(argv, expected):
    assert procs.marked_as(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "cli.py", "_watchdog", "--control-port", "8088", "--set lyrebird_control_port=9099"],
        ["mitmdump", "--set", f"lyrebird_control_port={PORT}", "--set", "confdir=/tmp/_watchdog/x", "-s", "addon.py"],
        ["mitmdump", "--set", f"lyrebird_control_port={PORT}", "-s", "/tmp/--control-port 9099/addon.py"],
    ],
    ids=["a service named like the option", "a state dir named like the marker", "a path containing the words"],
)
def test_marked_as_reads_real_tokens_not_words_in_a_name(argv):
    """A directory or a service can be named anything, the option's own text included. Matched as
    words in a joined string, a state directory decided which port a process belonged to."""
    assert procs.marked_as(argv) == (("watchdog", PORT) if "_watchdog" in argv else ("proxy", PORT))


# MARK: - liveness over the fake table


def test_liveness_is_alive_only_with_the_marker_the_port_and_the_create_time(monkeypatch):
    table(monkeypatch, {REF.pid: proxy()})
    assert procs.liveness(REF, "proxy", PORT) is Liveness.ALIVE
    assert procs.liveness(REF, "watchdog", PORT) is Liveness.PROVEN_DEAD
    assert procs.liveness(REF, "proxy", 9099) is Liveness.PROVEN_DEAD


def test_liveness_of_a_pid_that_is_gone_is_proven_dead(monkeypatch):
    table(monkeypatch, {})
    assert procs.liveness(REF, "proxy", PORT) is Liveness.PROVEN_DEAD


def test_liveness_of_a_zombie_is_proven_dead(monkeypatch):
    """On macOS a zombie's `create_time` and `uids` still read while `cmdline` raises, so `status`
    is asked first — inferred from a create time alone, a zombie proxy looked alive for ever."""
    table(monkeypatch, {REF.pid: proxy(status=psutil.STATUS_ZOMBIE)})
    assert procs.liveness(REF, "proxy", PORT) is Liveness.PROVEN_DEAD


def test_inspect_reads_an_unmarked_process_with_our_pid_as_proven_dead(monkeypatch):
    table(monkeypatch, {REF.pid: FakeProc(cmdline=["vim", "notes.txt"], create_time=REF.create_time)})
    assert procs.liveness(REF, "proxy", PORT) is Liveness.PROVEN_DEAD


def test_inspect_reads_a_marked_process_with_another_create_time_as_unknown(monkeypatch):
    """A clock step shifts the create time psutil reports for a process that never died, and a
    same-port reuse looks identical. Read as proven dead, the watchdog released the journal of a
    proxy that was alive; doubt acts on nothing instead."""
    table(monkeypatch, {REF.pid: proxy(create_time=REF.create_time + 500)})
    assert procs.liveness(REF, "proxy", PORT) is Liveness.UNKNOWN


_UNMAPPED = pytest.mark.parametrize(
    "error",
    [psutil.AccessDenied(101), OSError(errno.ENOMEM, "Cannot allocate memory")],
    ids=["access denied", "a plain OSError"],
)


@pytest.mark.parametrize(
    "call", ["construct", "status", "create_time", "cmdline"], ids=["Process()", "status", "create_time", "cmdline"]
)
@_UNMAPPED
def test_procs_normalises_plain_oserror_at_every_entry(monkeypatch, call, error):
    """psutil's macOS layer raises a bare `OSError` for syscall failures its wrapper does not
    translate, from *any* call. Anything escaping this module kills the watchdog tick that met it,
    and the Mac stays routed at a proxy nobody is watching.

    Every entry point, over every inspection call: a reading is `UNKNOWN`, and everything that
    would signal raises `ProcessCheckError` — never `NOT_RUNNING`, which is what `down` prints
    "stopped" on.
    """
    table(monkeypatch, {REF.pid: proxy(errors={call: error})})
    assert procs.liveness(REF, "proxy", PORT) is Liveness.UNKNOWN
    for act in (procs.terminate, procs.kill_now):
        with pytest.raises(ProcessCheckError):
            act(REF, "proxy", PORT)
    if call != "cmdline":  # `ref_of` needs the status and the create time, and reads no argv
        with pytest.raises(ProcessCheckError):
            procs.ref_of(REF.pid)


@_UNMAPPED
def test_self_ref_raises_rather_than_inventing_this_processs_identity(monkeypatch, error):
    """The watchdog's `me` is what every later "is this record mine?" is decided against. A `Ref`
    minted when the read failed would name a pid and a create time nothing established, and a
    watchdog comparing itself against it would retire — or fail to."""
    table(monkeypatch, {os.getpid(): proxy(errors={"create_time": error})})
    with pytest.raises(ProcessCheckError):
        procs.self_ref()


@_UNMAPPED
@pytest.mark.parametrize("stage", ["SIGTERM", "the wait after SIGTERM", "SIGKILL", "the wait after SIGKILL"])
def test_terminate_raises_at_every_signal_and_wait_stage_it_cannot_complete(monkeypatch, error, stage):
    """The signal stages too, not just the inspection. `terminate()`, `kill()` and both `wait()`s
    are syscalls that fail the same way, and each of the four is reached only by getting past the
    one before it. A refusal read as "it is gone" would let `down` release the journal of a proxy
    that is still holding the port.
    """
    timed_out = psutil.TimeoutExpired(1)
    spec = {
        # Each entry is the first thing that fails on the way through `terminate`.
        "SIGTERM": proxy(errors={"terminate": error}),
        "the wait after SIGTERM": proxy(waits=[error]),
        "SIGKILL": proxy(errors={"kill": error}, waits=[timed_out]),
        "the wait after SIGKILL": proxy(waits=[timed_out, error]),
    }[stage]
    table(monkeypatch, {REF.pid: spec})
    with pytest.raises(ProcessCheckError):
        procs.terminate(REF, "proxy", PORT, term_wait=0.01, kill_wait=0.01)


@_UNMAPPED
def test_scan_is_incomplete_when_a_same_uid_pid_cannot_be_read(monkeypatch, error):
    """A gap in the scan is a claim about the scan. Read as "nothing found", an unreadable pid of
    this user would let `up` acquire over a session that is still running."""
    fake = table(monkeypatch, {REF.pid: proxy(errors={"uids": error})})
    assert isinstance(procs.scan_marked(), Incomplete)
    fake.pids_error = error
    assert isinstance(procs.scan_marked(), Incomplete), "and so is an enumeration that never began"


def test_ref_of_reads_a_live_process(monkeypatch):
    table(monkeypatch, {REF.pid: proxy()})
    assert procs.ref_of(REF.pid) == REF


def test_ref_of_refuses_a_process_that_is_already_gone(monkeypatch):
    """`up` records the ref of the child it just spawned; a ref minted from a create time nobody
    could read would name a pid and nothing else."""
    table(monkeypatch, {})
    with pytest.raises(ProcessCheckError):
        procs.ref_of(REF.pid)
    table(monkeypatch, {REF.pid: proxy(status=psutil.STATUS_ZOMBIE)})
    with pytest.raises(ProcessCheckError, match="zombie"):
        procs.ref_of(REF.pid)


# MARK: - terminate


def test_terminate_is_not_running_when_the_pid_is_gone(monkeypatch):
    fake = table(monkeypatch, {})
    assert procs.terminate(REF, "proxy", PORT) is Termination.NOT_RUNNING
    assert fake.signalled == []


def test_terminate_signals_nothing_when_the_pid_belongs_to_something_else(monkeypatch):
    fake = table(monkeypatch, {REF.pid: FakeProc(cmdline=["vim"], create_time=REF.create_time)})
    assert procs.terminate(REF, "proxy", PORT) is Termination.NOT_RUNNING
    assert fake.signalled == []


def test_terminate_refuses_to_signal_what_it_could_not_identify(monkeypatch):
    fake = table(monkeypatch, {REF.pid: proxy(create_time=REF.create_time + 500)})
    with pytest.raises(ProcessCheckError):
        procs.terminate(REF, "proxy", PORT)
    assert fake.signalled == []


def test_terminate_stops_a_process_that_answers_sigterm(monkeypatch):
    fake = table(monkeypatch, {REF.pid: proxy()})
    assert procs.terminate(REF, "proxy", PORT) is Termination.STOPPED
    assert fake.signalled == [(REF.pid, "terminate")]
    assert fake.constructed == [REF.pid], "inspection and signal must share one instance"


def test_terminate_escalates_to_sigkill(monkeypatch):
    spec = proxy(waits=[psutil.TimeoutExpired(0.1), 0], on_kill=lambda s: setattr(s, "gone", True))
    fake = table(monkeypatch, {REF.pid: spec})
    assert procs.terminate(REF, "proxy", PORT, term_wait=0.01, kill_wait=0.01) is Termination.STOPPED
    assert fake.signalled == [(REF.pid, "terminate"), (REF.pid, "kill")]


def test_terminate_reports_stopped_when_the_process_exits_before_kill(monkeypatch):
    """The exit race between the second inspection and the signal: psutil's own reuse guard raises
    `NoSuchProcess` out of `kill()`, which is the process being gone, not a failure."""
    spec = proxy(waits=[psutil.TimeoutExpired(0.1)], errors={"kill": psutil.NoSuchProcess(REF.pid)})
    table(monkeypatch, {REF.pid: spec})
    assert procs.terminate(REF, "proxy", PORT, term_wait=0.01, kill_wait=0.01) is Termination.STOPPED


def test_terminate_reads_a_zombie_after_the_kill_wait_as_stopped(monkeypatch):
    """`wait()` on a non-child zombie polls `pid_exists` and times out; the third inspection is what
    turns that into an answer."""

    def zombify(spec):
        spec.status = psutil.STATUS_ZOMBIE

    spec = proxy(waits=[psutil.TimeoutExpired(0.1), psutil.TimeoutExpired(0.1)], on_kill=zombify)
    table(monkeypatch, {REF.pid: spec})
    assert procs.terminate(REF, "proxy", PORT, term_wait=0.01, kill_wait=0.01) is Termination.STOPPED


def test_terminate_reports_a_process_that_survives_sigkill(monkeypatch):
    spec = proxy(waits=[psutil.TimeoutExpired(0.1), psutil.TimeoutExpired(0.1)])
    fake = table(monkeypatch, {REF.pid: spec})
    assert procs.terminate(REF, "proxy", PORT, term_wait=0.01, kill_wait=0.01) is Termination.STILL_RUNNING
    assert fake.signalled == [(REF.pid, "terminate"), (REF.pid, "kill")]


def test_terminate_raises_when_the_signal_is_refused(monkeypatch):
    table(monkeypatch, {REF.pid: proxy(errors={"terminate": psutil.AccessDenied(REF.pid)})})
    with pytest.raises(ProcessCheckError):
        procs.terminate(REF, "proxy", PORT)


# MARK: - kill_now


def test_kill_now_never_signals_a_reused_pid(monkeypatch):
    """The one caller that makes a proxy die without asking it to passes the journalled `Ref`, so a
    pid handed to something else in the meantime receives nothing — where the acceptance harness
    used to `os.kill` a number read out of a record."""
    fake = table(monkeypatch, {REF.pid: FakeProc(cmdline=["vim"], create_time=REF.create_time)})
    assert procs.kill_now(REF, "proxy", PORT) is Termination.NOT_RUNNING
    assert fake.signalled == []


def test_kill_now_kills_without_asking_first(monkeypatch):
    spec = proxy(on_kill=lambda s: setattr(s, "gone", True))
    fake = table(monkeypatch, {REF.pid: spec})
    assert procs.kill_now(REF, "proxy", PORT) is Termination.STOPPED
    assert fake.signalled == [(REF.pid, "kill")]


# MARK: - scan_marked


def test_scan_finds_both_kinds_on_any_port(monkeypatch):
    fake = table(
        monkeypatch,
        {
            10: proxy(),
            11: FakeProc(cmdline=["python", "cli.py", "_watchdog", "--control-port", "9099"], create_time=2.0),
            12: FakeProc(cmdline=["vim", "notes.txt"]),
        },
    )
    scan = procs.scan_marked(exclude=999)
    assert isinstance(scan, Complete)
    assert {(marked.ref.pid, marked.kind, marked.port) for marked in scan.found} == {
        (10, "proxy", PORT),
        (11, "watchdog", 9099),
    }
    assert fake.constructed.count(10) == 1


def test_scan_keeps_the_argv_it_read_the_marker_from(monkeypatch):
    table(monkeypatch, {10: proxy()})
    scan = procs.scan_marked(exclude=999)
    assert scan.found[0].cmdline == tuple(PROXY_ARGV)


def test_scan_excludes_the_caller(monkeypatch):
    table(monkeypatch, {10: proxy()})
    assert procs.scan_marked(exclude=10) == Complete(())


def test_scan_skips_another_users_process_before_reading_its_argv(monkeypatch):
    """`cmdline()` raises AccessDenied for other users' processes on macOS, and only a *same-UID*
    refusal is evidence that the scan missed something."""
    table(
        monkeypatch,
        {10: FakeProc(uid=os.getuid() + 1, errors={"cmdline": psutil.AccessDenied(10)}, cmdline=PROXY_ARGV)},
    )
    assert procs.scan_marked(exclude=999) == Complete(())


def test_scan_skips_an_exit_race(monkeypatch):
    table(monkeypatch, {10: proxy(errors={"cmdline": psutil.NoSuchProcess(10)})})
    assert procs.scan_marked(exclude=999) == Complete(())


def test_scan_is_incomplete_when_one_of_our_own_cannot_be_read(monkeypatch):
    """Read as "nothing found", an AccessDenied on this user's own pid let `up` acquire over a
    session that was still running."""
    table(monkeypatch, {10: proxy(errors={"cmdline": psutil.AccessDenied(10)})})
    assert isinstance(procs.scan_marked(exclude=999), Incomplete)
    table(monkeypatch, {10: proxy(errors={"uids": OSError(errno.ENOMEM, "Cannot allocate memory")})})
    assert isinstance(procs.scan_marked(exclude=999), Incomplete)


def test_scan_is_incomplete_when_enumeration_fails(monkeypatch):
    table(monkeypatch, {10: proxy()}, pids_error=OSError(errno.EPERM, "Operation not permitted"))
    assert isinstance(procs.scan_marked(exclude=999), Incomplete)
    table(monkeypatch, {10: proxy()}, pids_error=psutil.AccessDenied(0))
    assert isinstance(procs.scan_marked(exclude=999), Incomplete)


# MARK: - real processes, real psutil


@pytest.fixture
def real_psutil(monkeypatch, _no_real_psutil):
    """These cases are about the real adapter against the real process table."""
    monkeypatch.setattr(procs, "psutil", _no_real_psutil)
    return _no_real_psutil


def _child(script, argv_marker=()):
    proc = subprocess.Popen([sys.executable, "-c", script, *argv_marker], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline() == "ready\n", "the child must be running before it is signalled"
    return proc


_IGNORES_SIGTERM = (
    "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ready', flush=True); time.sleep(30)"
)
_SLEEPS = "import sys, time; print('ready', flush=True); time.sleep(30)"


def _marker_argv(kind, port):
    return (
        ["_watchdog", "--control-port", str(port)]
        if kind == "watchdog"
        else ["--set", f"lyrebird_control_port={port}", "-s", "addon.py"]
    )


def test_terminate_escalates_to_sigkill_on_a_real_process(real_psutil):
    """A real process that ignores SIGTERM: reported STOPPED only once it is actually gone."""
    child = _child(_IGNORES_SIGTERM, _marker_argv("watchdog", PORT))
    try:
        ref = procs.ref_of(child.pid)
        assert procs.terminate(ref, "watchdog", PORT, term_wait=0.3, kill_wait=5) is Termination.STOPPED
        # Nothing else could have ended a process that ignores SIGTERM: the escalation is what this
        # asserts, and `poll()` cannot say so — `wait()` inside the adapter has already reaped it.
        assert procs.liveness(ref, "watchdog", PORT) is Liveness.PROVEN_DEAD
    finally:
        child.kill()
        child.wait(10)


def test_terminate_signals_nothing_for_a_child_that_has_exited(real_psutil):
    child = _child(_SLEEPS, _marker_argv("watchdog", PORT))
    ref = procs.ref_of(child.pid)
    child.kill()
    child.wait(10)
    assert procs.terminate(ref, "watchdog", PORT, term_wait=0.2, kill_wait=0.2) is Termination.NOT_RUNNING


def test_a_marked_child_with_a_foreign_create_time_is_unknown_and_is_not_signalled(real_psutil):
    """Pid reuse cannot be forced — the kernel assigns pids — so this is the branch a reused pid
    takes, driven deterministically: the marker matches, the create time does not, and nothing is
    signalled."""
    child = _child(_SLEEPS, _marker_argv("watchdog", PORT))
    try:
        ref = procs.ref_of(child.pid)
        stale = Ref(pid=ref.pid, create_time=ref.create_time - 3600)
        assert procs.liveness(stale, "watchdog", PORT) is Liveness.UNKNOWN
        with pytest.raises(ProcessCheckError):
            procs.terminate(stale, "watchdog", PORT, term_wait=0.2, kill_wait=0.2)
        with pytest.raises(ProcessCheckError):
            procs.kill_now(stale, "watchdog", PORT, kill_wait=0.2)
        assert child.poll() is None, "nothing may be signalled on an identity that was not proved"
    finally:
        child.kill()
        child.wait(10)


def test_an_unmarked_child_with_our_pid_is_proven_dead_and_is_not_signalled(real_psutil):
    child = _child(_SLEEPS)
    try:
        ref = procs.ref_of(child.pid)
        assert procs.liveness(ref, "watchdog", PORT) is Liveness.PROVEN_DEAD
        assert procs.terminate(ref, "watchdog", PORT, term_wait=0.2, kill_wait=0.2) is Termination.NOT_RUNNING
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(10)


def test_ref_of_agrees_with_the_self_ref_the_child_computes(real_psutil, tmp_path):
    """`up` records `ref_of(child.pid)` and the watchdog reports its own `self_ref()`; if those
    disagreed, every tick would read the journal as naming another watchdog and retire."""
    script = (
        "import sys, json, time;"
        f"sys.path.insert(0, {str(tmp_path.parent)!r});"
        f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(procs.__file__)))!r});"
        "import procs;"
        "me = procs.self_ref();"
        "print(json.dumps([me.pid, me.create_time]), flush=True);"
        "time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        pid, create_time = __import__("json").loads(child.stdout.readline())
        assert procs.ref_of(child.pid) == Ref(pid=pid, create_time=create_time)
    finally:
        child.kill()
        child.wait(10)
