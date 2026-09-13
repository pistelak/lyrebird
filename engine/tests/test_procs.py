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
import subprocess
import sys

import psutil
import pytest

import procs
from cli_doubles import FakeProc, FakePsutil
from ownership import Ref
from procs import Liveness, ProcessCheckError, Termination

PORT = 8088
PROXY_ARGV = ["mitmdump", "--set", f"lyrebird_control_port={PORT}", "-s", "/path/to/addon.py"]
REF = Ref(pid=101, create_time=1000.5)


def table(monkeypatch, processes=None):
    fake = FakePsutil(processes or {})
    monkeypatch.setattr(procs, "psutil", fake)
    return fake


def proxy(**kwargs):
    kwargs.setdefault("cmdline", PROXY_ARGV)
    kwargs.setdefault("create_time", REF.create_time)
    return FakeProc(**kwargs)


# MARK: - is_proxy_on


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (PROXY_ARGV, True),
        (["mitmdump", "--set", f"lyrebird_control_port={PORT}", "-s", "addon.py"], True),
        (["mitmdump", "-s", "/path/to/addon.py"], False),
        (["mitmdump", "--set", f"lyrebird_control_port={PORT}", "-s", "/path/to/other.py"], False),
        (["mitmdump", "--set", "lyrebird_control_port=9999", "-s", "addon.py"], False),
        (["mitmdump", f"lyrebird_control_port={PORT}", "-s", "addon.py"], False),
        (["mitmdump", "--set", "lyrebird_control_port=notaport", "-s", "addon.py"], False),
    ],
    ids=[
        "a proxy",
        "a relative addon path",
        "no port token",
        "the port but not our addon",
        "another session's port",
        "the port token without --set before it",
        "a port that is not a number",
    ],
)
def test_is_proxy_on_reads_the_argv_tokens(argv, expected):
    assert procs.is_proxy_on(argv, PORT) is expected


@pytest.mark.parametrize(
    "argv",
    [
        ["mitmdump", "--set", f"confdir=/Users/x/lyrebird_control_port={PORT}", "-s", "addon.py"],
        ["mitmdump", "--set", f"confdir=/tmp/--set lyrebird_control_port={PORT}", "-s", "addon.py"],
    ],
)
def test_is_proxy_on_reads_real_tokens_not_words_in_a_name(argv):
    """Exact list tokens, never a regex over a joined string: a state directory can be named
    anything, including the option's own text, and a match there would let `down` signal it."""
    assert procs.is_proxy_on(argv, PORT) is False


# MARK: - liveness


def test_liveness_is_alive_only_with_the_marker_the_port_and_the_create_time(monkeypatch):
    table(monkeypatch, {REF.pid: proxy()})
    assert procs.liveness(REF, PORT) is Liveness.ALIVE
    assert procs.liveness(REF, 9999) is Liveness.PROVEN_DEAD


def test_liveness_of_a_pid_that_is_gone_is_proven_dead(monkeypatch):
    table(monkeypatch, {})
    assert procs.liveness(REF, PORT) is Liveness.PROVEN_DEAD


def test_liveness_of_a_zombie_is_proven_dead(monkeypatch):
    """On macOS a zombie's create time still reads while `cmdline()` raises, so `status()` is asked
    first — otherwise a zombie looks alive."""
    table(monkeypatch, {REF.pid: proxy(status=psutil.STATUS_ZOMBIE)})
    assert procs.liveness(REF, PORT) is Liveness.PROVEN_DEAD


def test_inspect_reads_an_unmarked_process_with_our_pid_as_proven_dead(monkeypatch):
    table(monkeypatch, {REF.pid: FakeProc(cmdline=["/usr/bin/vim"], create_time=REF.create_time)})
    assert procs.liveness(REF, PORT) is Liveness.PROVEN_DEAD


def test_inspect_reads_a_marked_process_with_another_create_time_as_unknown(monkeypatch):
    """A clock step and a same-port pid reuse are indistinguishable, and neither is proof. Read as
    PROVEN_DEAD, `down` would report a running proxy as stopped; read as ALIVE, it would signal a
    stranger."""
    table(monkeypatch, {REF.pid: proxy(create_time=REF.create_time + 5)})
    assert procs.liveness(REF, PORT) is Liveness.UNKNOWN


@pytest.mark.parametrize(
    "error",
    [psutil.AccessDenied(REF.pid), OSError(errno.ENOMEM, "cannot allocate memory")],
    ids=["access denied", "a plain OSError psutil did not translate"],
)
@pytest.mark.parametrize("call", ["construct", "status", "create_time", "cmdline"])
def test_procs_normalises_plain_oserror_at_every_entry(monkeypatch, call, error):
    """psutil's macOS layer raises a bare `OSError` from any call. Escaping the adapter it would
    traceback out of `down`; read as "gone" it would license a claim nobody checked."""
    table(monkeypatch, {REF.pid: proxy(errors={call: error})})
    assert procs.liveness(REF, PORT) is Liveness.UNKNOWN
    with pytest.raises(ProcessCheckError):
        procs.terminate(REF, PORT, term_wait=0.01, kill_wait=0.01)
    if call != "cmdline":  # `ref_of` never reads the argv
        with pytest.raises(ProcessCheckError):
            procs.ref_of(REF.pid)


# MARK: - ref_of


def test_ref_of_reads_a_live_process(monkeypatch):
    table(monkeypatch, {REF.pid: proxy()})
    assert procs.ref_of(REF.pid) == REF


def test_ref_of_refuses_a_process_that_is_already_gone(monkeypatch):
    """A child that died the instant it was spawned is *reported*, not recorded: a `Ref` minted
    without a create time would name a pid and nothing else."""
    table(monkeypatch, {})
    with pytest.raises(ProcessCheckError):
        procs.ref_of(REF.pid)


def test_ref_of_refuses_a_zombie(monkeypatch):
    table(monkeypatch, {REF.pid: proxy(status=psutil.STATUS_ZOMBIE)})
    with pytest.raises(ProcessCheckError) as raised:
        procs.ref_of(REF.pid)
    assert "zombie" in str(raised.value)


# MARK: - terminate


def test_terminate_is_not_running_when_the_pid_is_gone(monkeypatch):
    table(monkeypatch, {})
    assert procs.terminate(REF, PORT) is Termination.NOT_RUNNING


def test_terminate_signals_nothing_when_the_pid_belongs_to_something_else(monkeypatch):
    fake = table(monkeypatch, {REF.pid: FakeProc(cmdline=["/usr/bin/vim"], create_time=REF.create_time)})
    assert procs.terminate(REF, PORT) is Termination.NOT_RUNNING
    assert fake.signalled == []


def test_terminate_refuses_a_reused_pid(monkeypatch):
    """Marked, on our port, and a create time that is not the one recorded: the identity is
    unproven, and doubt is never a licence to signal."""
    fake = table(monkeypatch, {REF.pid: proxy(create_time=REF.create_time + 5)})
    with pytest.raises(ProcessCheckError):
        procs.terminate(REF, PORT)
    assert fake.signalled == []


def test_terminate_stops_a_process_that_answers_sigterm(monkeypatch):
    fake = table(monkeypatch, {REF.pid: proxy(dies=True)})
    assert procs.terminate(REF, PORT) is Termination.STOPPED
    assert fake.signalled == [(REF.pid, "terminate")]


def test_terminate_escalates_to_sigkill(monkeypatch):
    fake = table(monkeypatch, {REF.pid: proxy(waits=[psutil.TimeoutExpired(0.1)], on_kill=_dies)})
    assert procs.terminate(REF, PORT, term_wait=0.01, kill_wait=0.01) is Termination.STOPPED
    assert fake.signalled == [(REF.pid, "terminate"), (REF.pid, "kill")]


def _dies(spec):
    spec.gone = True


def test_terminate_reports_stopped_when_the_process_exits_before_kill(monkeypatch):
    """The exit race between the inspection and the signal: `kill()` raising NoSuchProcess is the
    process being gone, which is what was wanted."""
    spec = proxy(waits=[psutil.TimeoutExpired(0.1)], errors={"kill": psutil.NoSuchProcess(REF.pid)})
    table(monkeypatch, {REF.pid: spec})
    assert procs.terminate(REF, PORT, term_wait=0.01, kill_wait=0.01) is Termination.STOPPED


def test_terminate_reports_a_process_that_survives_sigkill(monkeypatch):
    """Never "stopped" over a process still running: `down` prints the pid and tells the operator
    to kill it by hand."""
    spec = proxy(waits=[psutil.TimeoutExpired(0.1), psutil.TimeoutExpired(0.1)])
    table(monkeypatch, {REF.pid: spec})
    assert procs.terminate(REF, PORT, term_wait=0.01, kill_wait=0.01) is Termination.STILL_RUNNING


def test_terminate_reads_a_zombie_after_the_kill_wait_as_stopped(monkeypatch):
    """A non-child zombie makes `wait()` time out, which is why the same instance is asked again."""

    def zombify(spec):
        spec.status = psutil.STATUS_ZOMBIE

    spec = proxy(waits=[psutil.TimeoutExpired(0.1), psutil.TimeoutExpired(0.1)], on_kill=zombify)
    table(monkeypatch, {REF.pid: spec})
    assert procs.terminate(REF, PORT, term_wait=0.01, kill_wait=0.01) is Termination.STOPPED


@pytest.mark.parametrize(
    ("stage", "error"),
    [
        ("terminate", psutil.AccessDenied(REF.pid)),
        ("terminate", OSError(errno.EPERM, "operation not permitted")),
        ("kill", psutil.AccessDenied(REF.pid)),
    ],
)
def test_terminate_raises_when_the_signal_is_refused(monkeypatch, stage, error):
    spec = proxy(errors={stage: error}, waits=[psutil.TimeoutExpired(0.1)])
    table(monkeypatch, {REF.pid: spec})
    with pytest.raises(ProcessCheckError):
        procs.terminate(REF, PORT, term_wait=0.01, kill_wait=0.01)


def test_terminate_raises_when_the_final_inspection_cannot_be_made(monkeypatch):
    """After the kill wait timed out, an unreadable process is not a stopped one."""
    spec = proxy(waits=[psutil.TimeoutExpired(0.1), psutil.TimeoutExpired(0.1)])
    fake = table(monkeypatch, {REF.pid: spec})

    def break_after_signals(_spec):
        spec.errors["status"] = psutil.AccessDenied(REF.pid)

    spec.on_kill = break_after_signals
    with pytest.raises(ProcessCheckError):
        procs.terminate(REF, PORT, term_wait=0.01, kill_wait=0.01)
    assert (REF.pid, "kill") in fake.signalled


def test_inspection_and_signal_go_through_one_instance(monkeypatch):
    """`Process.terminate()` re-checks `(pid, create_time)` against the identity the *instance* was
    built with, so a second construction between the check and the signal would be a second
    identity."""
    fake = table(monkeypatch, {REF.pid: proxy(dies=True)})
    procs.terminate(REF, PORT)
    assert fake.constructed == [REF.pid]


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
_MARKER = ["--set", f"lyrebird_control_port={PORT}", "-s", "addon.py"]


def test_terminate_escalates_to_sigkill_on_a_real_process(real_psutil):
    """A real process that ignores SIGTERM: reported STOPPED only once it is actually gone."""
    child = _child(_IGNORES_SIGTERM, _MARKER)
    try:
        ref = procs.ref_of(child.pid)
        assert procs.terminate(ref, PORT, term_wait=0.3, kill_wait=5) is Termination.STOPPED
        # Nothing else could have ended a process that ignores SIGTERM: the escalation is what this
        # asserts, and `poll()` cannot say so — `wait()` inside the adapter has already reaped it.
        assert procs.liveness(ref, PORT) is Liveness.PROVEN_DEAD
    finally:
        child.kill()
        child.wait(10)


def test_terminate_signals_nothing_for_a_child_that_has_exited(real_psutil):
    child = _child(_SLEEPS, _MARKER)
    ref = procs.ref_of(child.pid)
    child.kill()
    child.wait(10)
    assert procs.terminate(ref, PORT, term_wait=0.2, kill_wait=0.2) is Termination.NOT_RUNNING


def test_a_marked_child_with_a_foreign_create_time_is_unknown_and_is_not_signalled(real_psutil):
    """Pid reuse cannot be forced — the kernel assigns pids — so this is the branch a reused pid
    takes, driven deterministically: the marker matches, the create time does not, and nothing is
    signalled."""
    child = _child(_SLEEPS, _MARKER)
    try:
        ref = procs.ref_of(child.pid)
        stale = Ref(pid=ref.pid, create_time=ref.create_time - 3600)
        assert procs.liveness(stale, PORT) is Liveness.UNKNOWN
        with pytest.raises(ProcessCheckError):
            procs.terminate(stale, PORT, term_wait=0.2, kill_wait=0.2)
        assert child.poll() is None, "nothing may be signalled on an identity that was not proved"
    finally:
        child.kill()
        child.wait(10)


def test_an_unmarked_child_with_our_pid_is_proven_dead_and_is_not_signalled(real_psutil):
    child = _child(_SLEEPS)
    try:
        ref = procs.ref_of(child.pid)
        assert procs.liveness(ref, PORT) is Liveness.PROVEN_DEAD
        assert procs.terminate(ref, PORT, term_wait=0.2, kill_wait=0.2) is Termination.NOT_RUNNING
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(10)
