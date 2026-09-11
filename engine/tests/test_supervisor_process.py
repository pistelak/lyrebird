"""The three process helpers `down` and `up` decide with: `_pid_alive`, `_pid_is_ours`, `_terminate`.

Each is asked what it *knows*, and the failure paths are the point: a check that could not be made
must not come back as "not running" or "not ours", because that is the answer `down` prints
"stopped" on. The doubles are the OS seams — `os.kill` and the `ps` call — and one test uses a real
process that ignores SIGTERM, since escalation is nothing a double can prove.
"""

import errno
import signal
import subprocess
import sys
import threading

import pytest

import simulator as sim
import supervisor
from supervisor import ProcessCheckError, Termination


def _kill_raising(monkeypatch, error):
    def kill(pid, sig):
        raise error

    monkeypatch.setattr(supervisor.os, "kill", kill)


# MARK: - _pid_alive


def test_pid_alive_reads_permission_denied_as_alive(monkeypatch):
    """EPERM is a process that exists under another user. Read as "dead", `down` deleted the record
    of a proxy that was still running — and still had the PAC pointed at it."""
    _kill_raising(monkeypatch, PermissionError(errno.EPERM, "Operation not permitted"))
    assert supervisor._pid_alive(4242) is True


def test_pid_alive_reads_no_such_process_as_dead(monkeypatch):
    _kill_raising(monkeypatch, ProcessLookupError(errno.ESRCH, "No such process"))
    assert supervisor._pid_alive(4242) is False


def test_pid_alive_raises_when_the_check_itself_fails(monkeypatch):
    """Any other error is not an answer about the process."""
    _kill_raising(monkeypatch, OSError(errno.EINVAL, "Invalid argument"))
    with pytest.raises(ProcessCheckError, match="could not check pid 4242"):
        supervisor._pid_alive(4242)


# MARK: - _pid_is_ours


def _alive(monkeypatch, alive=True):
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: alive)


def test_pid_is_ours_raises_when_ps_fails(monkeypatch):
    """A `ps` that fails while the pid is alive has said nothing about whose it is. Read as
    "not ours", `_terminate` skipped the signal and `down` printed "stopped"."""
    _alive(monkeypatch)
    monkeypatch.setattr(sim, "_run", lambda args: subprocess.CompletedProcess(args, 1, "", "ps: unexpected error"))
    with pytest.raises(ProcessCheckError, match=r"`ps -p 4242` failed: ps: unexpected error"):
        supervisor._pid_is_ours(4242, "addon.py")


def test_pid_is_ours_is_false_when_the_process_exits_between_the_two_looks(monkeypatch):
    """`ps -p` exits 1 for a pid that is gone — the one non-zero exit that is an answer."""
    looks = iter([True, False])
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: next(looks))
    monkeypatch.setattr(sim, "_run", lambda args: subprocess.CompletedProcess(args, 1, "", ""))
    assert supervisor._pid_is_ours(4242, "addon.py") is False


def test_pid_is_ours_raises_when_ps_cannot_be_run(monkeypatch):
    _alive(monkeypatch)

    def missing(args):
        raise FileNotFoundError(2, "No such file or directory", "ps")

    monkeypatch.setattr(sim, "_run", missing)
    with pytest.raises(ProcessCheckError, match="could not run `ps -p 4242`"):
        supervisor._pid_is_ours(4242, "addon.py")


# MARK: - _terminate


def test_terminate_escalates_to_sigkill_when_sigterm_is_ignored(monkeypatch):
    """A real process that ignores SIGTERM. Reported STOPPED only once it is actually gone —
    which takes the SIGKILL that `down` never used to send."""
    marker = "lyrebird-test-ignores-sigterm"
    child = (
        "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"print('ready', flush=True); time.sleep(30)  # {marker}"
    )
    proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline() == "ready\n", "the handler must be installed before the signal is sent"
    # Reap it the moment it dies: a zombie still answers `kill(pid, 0)`, and nothing here waits.
    threading.Thread(target=proc.wait, daemon=True).start()
    monkeypatch.setattr(supervisor, "_DOWN_WAIT_SECONDS", 0.3)
    try:
        assert supervisor._terminate(proc.pid, marker) is Termination.STOPPED
        assert proc.poll() == -signal.SIGKILL
    finally:
        proc.kill()


def test_terminate_reports_a_process_that_survives_sigkill(monkeypatch):
    """Nothing here can make a real process unkillable, so the OS is doubled: alive throughout,
    ours throughout, both signals delivered. The outcome is STILL_RUNNING, not "stopped"."""
    sent = []
    _alive(monkeypatch)
    monkeypatch.setattr(supervisor, "_pid_is_ours", lambda pid, marker: True)
    monkeypatch.setattr(supervisor, "_signal", lambda pid, sig: sent.append(sig) or True)
    monkeypatch.setattr(supervisor, "_DOWN_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "_KILL_WAIT_SECONDS", 0.05)

    assert supervisor._terminate(4242, "addon.py") is Termination.STILL_RUNNING
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_terminate_does_not_sigkill_a_pid_reused_during_the_wait(monkeypatch):
    """SIGTERM lands, the proxy exits, and the pid is handed to something else before the poll
    sees it gone. Without the ownership re-check, SIGKILL reaches the newcomer."""
    sent = []
    ownership = iter([True, False])  # ours at the first look; somebody else's by the second
    _alive(monkeypatch)
    monkeypatch.setattr(supervisor, "_pid_is_ours", lambda pid, marker: next(ownership))
    monkeypatch.setattr(supervisor, "_signal", lambda pid, sig: sent.append(sig) or True)
    monkeypatch.setattr(supervisor, "_DOWN_WAIT_SECONDS", 0.05)

    assert supervisor._terminate(4242, "addon.py") is Termination.STOPPED
    assert sent == [signal.SIGTERM]


def test_terminate_leaves_a_reused_pid_alone(monkeypatch):
    _alive(monkeypatch)
    monkeypatch.setattr(
        sim, "_run", lambda args: subprocess.CompletedProcess(args, 0, "/usr/bin/some-other-tool\n", "")
    )
    _kill_raising(monkeypatch, AssertionError("a pid that is not ours must never be signalled"))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)

    assert supervisor._terminate(4242, "addon.py") is Termination.NOT_OURS


def test_terminate_raises_when_the_signal_cannot_be_sent(monkeypatch):
    """`os.kill` refusing (EPERM: the process changed hands) was suppressed, and the proxy it
    named went on running under a "stopped"."""
    _alive(monkeypatch)
    monkeypatch.setattr(supervisor, "_pid_is_ours", lambda pid, marker: True)
    _kill_raising(monkeypatch, PermissionError(errno.EPERM, "Operation not permitted"))
    with pytest.raises(ProcessCheckError, match="could not send SIGTERM to pid 4242"):
        supervisor._terminate(4242, "addon.py")


def test_terminate_reports_a_pid_that_is_gone_as_not_running(monkeypatch):
    _alive(monkeypatch, alive=False)
    assert supervisor._terminate(4242, "addon.py") is Termination.NOT_RUNNING
    assert supervisor._terminate(None, "addon.py") is Termination.NOT_RUNNING


# MARK: - Ours means this instance's


ROOT_ID = "0123456789ab"


@pytest.mark.parametrize(
    ("command", "ours"),
    [
        ("mitmdump --set lyrebird_control_port=8088 -s /path/to/addon.py", True),
        ("mitmdump --set lyrebird_control_port=9099 -s /path/to/addon.py", False),
        ("mitmdump -s /path/to/addon.py", True),  # a proxy older than the token: the port is unique anyway
        (f"python cli.py _watchdog --control-port 8088 --state-root-id {ROOT_ID} Wi-Fi", True),
        (f"python cli.py _watchdog --control-port 9099 --state-root-id {ROOT_ID} Wi-Fi", False),
        ("python cli.py _watchdog --control-port 8088 --state-root-id fedcba987654 Ethernet", False),
        ("python cli.py _watchdog Wi-Fi", False),  # a watchdog older than the tokens: replaced, not adopted
    ],
    ids=[
        "proxy ours",
        "proxy other port",
        "proxy pre-token",
        "watchdog ours",
        "watchdog other port",
        "watchdog other state root",
        "watchdog pre-token",
    ],
)
def test_pid_is_ours_refuses_another_instances_process(monkeypatch, command, ours):
    """A pid reused by another Lyrebird's proxy or watchdog carries the same marker. Judged on the
    marker alone, `up` adopted the other instance's watchdog and `down` killed its proxy."""
    _alive(monkeypatch)
    monkeypatch.setattr(supervisor.config, "CONTROL_PORT", 8088)
    monkeypatch.setattr(supervisor.config, "state_root_id", lambda: ROOT_ID)
    monkeypatch.setattr(sim, "_run", lambda args: subprocess.CompletedProcess(args, 0, command + "\n", ""))
    marker = "_watchdog" if "_watchdog" in command else "addon.py"
    assert supervisor._pid_is_ours(4242, marker) is ours


def test_pid_is_ours_refuses_a_watchdog_of_another_state_root(monkeypatch):
    """Two state roots can run watchdogs for one port, each restoring from its own record. One
    adopted across roots watches the other root's record, and this root's PAC has nobody."""
    _alive(monkeypatch)
    monkeypatch.setattr(supervisor.config, "CONTROL_PORT", 8088)
    monkeypatch.setattr(supervisor.config, "state_root_id", lambda: ROOT_ID)
    other = "python cli.py _watchdog --control-port 8088 --state-root-id fedcba987654 Wi-Fi\n"
    monkeypatch.setattr(sim, "_run", lambda args: subprocess.CompletedProcess(args, 0, other, ""))
    assert supervisor._pid_is_ours(4242, "_watchdog") is False


@pytest.mark.parametrize(
    "command",
    [
        f"python cli.py _watchdog --control-port 8088 --state-root-id {ROOT_ID} Office --control-port 9099",
        "mitmdump --set lyrebird_control_port=8088 --set confdir=/tmp/--set lyrebird_control_port=9099/x -s addon.py",
        "mitmdump --set lyrebird_control_port=8088 --set confdir=/tmp/lyrebird_control_port=9099/x -s addon.py",
    ],
    ids=["service named like the option", "state dir named like the option", "state dir containing the words"],
)
def test_pid_is_ours_reads_the_first_real_token_not_words_in_a_name(monkeypatch, command):
    """A service or a state directory can be named anything, including the option's text. Ours
    is the first thing after the executable; that match is the one that counts."""
    _alive(monkeypatch)
    monkeypatch.setattr(supervisor.config, "CONTROL_PORT", 8088)
    monkeypatch.setattr(supervisor.config, "state_root_id", lambda: ROOT_ID)
    monkeypatch.setattr(sim, "_run", lambda args: subprocess.CompletedProcess(args, 0, command + "\n", ""))
    marker = "_watchdog" if "_watchdog" in command else "addon.py"
    assert supervisor._pid_is_ours(4242, marker) is True


def test_pid_is_ours_refuses_a_foreign_process_whose_path_names_our_port(monkeypatch):
    _alive(monkeypatch)
    monkeypatch.setattr(supervisor.config, "CONTROL_PORT", 8088)
    command = "mitmdump --set lyrebird_control_port=9099 --set confdir=/tmp/lyrebird_control_port=8088/x -s addon.py\n"
    monkeypatch.setattr(sim, "_run", lambda args: subprocess.CompletedProcess(args, 0, command, ""))
    assert supervisor._pid_is_ours(4242, "addon.py") is False
