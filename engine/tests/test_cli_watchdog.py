"""The watchdog: one decision per tick, and nothing it does may outlive the session it serves.

The loop takes the session lock without a timeout — a watchdog that retired over contention would
leave the session unwatched — makes exactly one decision, and releases the lock before it sleeps,
so a `down` can always get in and a foreign write between two attempts is simply the next tick's
decision.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

import ownership
import session
import supervisor
from cli_doubles import (
    SILENT,
    FakeHealth,
    FakePsutil,
    answering,
    ours,
    ours_off,
    record,
    world,
)
from ownership import Active, Liveness, Pac, Ref, Restored

CORPORATE = Pac("http://proxy.example.com/corp.pac", True)
OTHER = Pac("http://proxy.example.net/other.pac", True)
EMPTY = Pac("", False)
PORT = 8088


def _watching(monkeypatch, *, pac=None, baseline=EMPTY, table=None, health=None, services=None):
    """A live session with this watchdog as the one the journal names."""
    table = table if table is not None else FakePsutil()
    proxy = table.spawn_ref("proxy", PORT)
    me = table.spawn_ref("watchdog", PORT)
    services = services or {"Wi-Fi": ("en0", pac if pac is not None else ours(PORT))}
    place = world(
        monkeypatch,
        record(Active(proxy, me), baseline=baseline),
        services=services,
        table=table,
        health=health,
    )
    return place, proxy, me


def _tick(place, me, *, ticks=1):
    """Run the loop for a bounded number of ticks, then stop it however it left the journal."""
    stop = {"n": 0}
    real = time.sleep

    def sleeping(seconds):
        stop["n"] += 1
        if stop["n"] >= ticks:
            raise _Enough()
        real(0)

    return sleeping, stop


class _Enough(Exception):
    """The test has seen enough ticks."""


def _run_loop(monkeypatch, place, me, *, ticks=1):
    sleeping, _ = _tick(place, me, ticks=ticks)
    monkeypatch.setattr(supervisor.time, "sleep", sleeping)
    try:
        supervisor.watchdog_loop(place.session, PORT, me)
    except _Enough:
        pass


# MARK: - What the watchdog does while the proxy answers


def test_watchdog_repairs_its_own_pac_that_macos_switched_off(profile, monkeypatch):
    """The live loop's one job: macOS silently disables the PAC while the proxy is alive."""
    place, _, me = _watching(monkeypatch, pac=ours_off(PORT))

    _run_loop(monkeypatch, place, me)

    assert place.pac() == ours(PORT)


def test_watchdog_preserves_when_the_pac_is_unreadable_while_the_proxy_answers(profile, monkeypatch):
    """Unknown is not "off": neither repair nor give up, ask again next tick."""
    place, _, me = _watching(monkeypatch, pac=ours_off(PORT))
    place.network.die_at(2)

    _run_loop(monkeypatch, place, me, ticks=1)

    assert place.network.setters() == []
    assert isinstance(place.journal().phase, Active), "the session is still watched"


def test_watchdog_exits_when_another_pid_answers_on_its_port(profile, monkeypatch):
    """The port outlives the proxy that opened it. A watchdog whose proxy died while a later `up`
    took the port kept seeing health, never reached its restore, and repaired a PAC for a proxy
    that was not the one running."""
    place, _, me = _watching(monkeypatch, health=FakeHealth(sequence=[answering(999_101)]))

    _run_loop(monkeypatch, place, me, ticks=5)

    assert place.network.setters() == [], "it retired without touching anything"
    assert isinstance(place.journal().phase, Active), "and left the record for whoever owns it"


def test_decide_watchdog_exits_when_a_reused_pid_answers(profile, monkeypatch):
    """The recorded pid answers and the recorded process is provably gone: another listener holds
    the port under a number the kernel handed out again."""
    table = FakePsutil()
    place, proxy, me = _watching(monkeypatch, table=table)
    table.processes[proxy.pid].gone = True
    FakeHealth(sequence=[answering(proxy.pid)]).install(monkeypatch)

    _run_loop(monkeypatch, place, me, ticks=5)

    assert place.network.setters() == []


def test_watchdog_exits_over_an_unreadable_journal_touching_nothing(profile, monkeypatch):
    """An unreadable journal is not "Active naming this Ref". Switching the PAC off over it — as
    the old watchdog did — acts on an obligation nobody can read."""
    place, _, me = _watching(monkeypatch)
    place.session.journal_path.write_bytes(b"not json at all\xff")

    _run_loop(monkeypatch, place, me, ticks=5)

    assert place.network.setters() == []
    assert isinstance(place.journal(), ownership.Unreadable), "left exactly as found, for `down`"


# MARK: - What it does once the proxy goes quiet


def test_watchdog_restores_the_baseline_and_releases_the_journal(profile, monkeypatch):
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, health=FakeHealth(sequence=[SILENT]))
    place.table.processes[proxy.pid].gone = True

    _run_loop(monkeypatch, place, me, ticks=5)

    assert place.pac() == CORPORATE
    assert place.journal() == ownership.Absent()


def test_watchdog_keeps_the_record_of_a_proxy_that_is_alive_but_silent(profile, monkeypatch):
    """A proxy whose control port has hung looks exactly like a dead one. The Mac must not stay
    routed at it — so the restore happens — but the record names the process `down` still has to
    stop, and deleting it leaves nothing to stop it by."""
    for liveness in (Liveness.ALIVE, Liveness.UNKNOWN):
        table = FakePsutil()
        place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, table=table, health=FakeHealth(sequence=[SILENT]))
        if liveness is Liveness.UNKNOWN:
            # A clock step: still marked, and a create time that no longer matches the record.
            table.processes[proxy.pid].create_time += 100.0

        _run_loop(monkeypatch, place, me, ticks=5)

        assert place.pac() == CORPORATE, liveness
        journal = place.journal()
        assert isinstance(journal, ownership.SessionRecord), liveness
        assert journal.phase == Restored(proxy), liveness


def test_watchdog_restores_when_liveness_raises_a_plain_oserror(profile, monkeypatch):
    """psutil's macOS layer raises a bare `OSError` for syscall failures its wrapper does not
    translate. Escaping the adapter it would kill the loop; normalised to UNKNOWN the Mac still
    stops being routed at a port nothing answers on."""
    table = FakePsutil()
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, table=table, health=FakeHealth(sequence=[SILENT]))
    table.processes[proxy.pid].errors["create_time"] = OSError("Cannot allocate memory")

    _run_loop(monkeypatch, place, me, ticks=5)

    assert place.pac() == CORPORATE


def test_watchdog_restore_of_a_satisfied_target_writes_nothing(profile, monkeypatch):
    """A target already met is a read-back with zero writes followed by the checkpoint — one path,
    so the `Silent` row does not need to know whether the obligation is outstanding."""
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, pac=CORPORATE, health=FakeHealth(sequence=[SILENT]))
    place.table.processes[proxy.pid].gone = True

    _run_loop(monkeypatch, place, me, ticks=5)

    assert place.network.setters() == []
    assert place.journal() == ownership.Absent()


def test_watchdog_preserves_and_keeps_ticking_when_the_service_is_gone(profile, monkeypatch):
    """A service that is gone is a temporary condition here — a cable out, a VPN down — and only
    `down` archives. The lock is released between ticks, so a `down` can still get in."""
    place, proxy, me = _watching(
        monkeypatch,
        baseline=CORPORATE,
        services={"Ethernet": ("en1", EMPTY)},
        health=FakeHealth(sequence=[SILENT]),
    )
    place.table.processes[proxy.pid].gone = True

    _run_loop(monkeypatch, place, me, ticks=3)

    assert place.network.setters() == []
    assert isinstance(place.journal().phase, Active), "still Active, still watched"


def test_watchdog_keeps_ticking_after_a_failed_restore_when_the_pac_turns_foreign(profile, monkeypatch):
    """A foreign write between two attempts is "touch nothing, keep ticking", not a retirement and
    not a write: the next tick decides on what it sees."""
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, health=FakeHealth(sequence=[SILENT]))
    place.table.processes[proxy.pid].gone = True
    place.network.fail_at(3)  # the first attempt's restore
    ticks = {"n": 0}
    real = supervisor._restore

    def watched(ref, port, baseline, lock_fd):
        ticks["n"] += 1
        if ticks["n"] == 1:
            try:
                return real(ref, port, baseline, lock_fd)
            finally:
                place.network.set_pac("Wi-Fi", OTHER)
        return real(ref, port, baseline, lock_fd)

    monkeypatch.setattr(supervisor, "_restore", watched)

    _run_loop(monkeypatch, place, me, ticks=4)

    assert place.pac() == OTHER, "a PAC this session does not own is never written over"
    assert isinstance(place.journal().phase, Active), "the record is kept for `down`"


def test_watchdog_retry_re_decides_and_stops_at_a_foreign_write(profile, monkeypatch):
    """The same rule stated the other way round: the retry is a fresh tick with a fresh decision,
    not a loop that repeats the one it started with."""
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, health=FakeHealth(sequence=[SILENT]))
    place.table.processes[proxy.pid].gone = True
    decisions = []
    real = ownership.decide_watchdog

    def watched(obs):
        answer = real(obs)
        decisions.append(type(answer).__name__)
        return answer

    monkeypatch.setattr(ownership, "decide_watchdog", watched)
    place.network.fail_at(3)
    place.network.write_at(4, "Wi-Fi", OTHER)

    _run_loop(monkeypatch, place, me, ticks=3)

    assert decisions[0] == "Restore"
    assert decisions[1] == "Proceed", "the second tick saw a foreign PAC and touched nothing"


def test_watchdog_keeps_the_journal_when_the_restore_keeps_failing(profile, monkeypatch):
    """N *consecutive* ticks with a failing restore, then retire. The journal stays `Active`: it is
    the only description of what to put back, and `down` is what acts on it now."""
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, health=FakeHealth(sequence=[SILENT]))
    place.table.processes[proxy.pid].gone = True

    def always_fails(ref, port, baseline, lock_fd):
        raise supervisor.netproxy.NetworkSetupError("`networksetup -setautoproxyurl Wi-Fi` failed")

    monkeypatch.setattr(supervisor, "_restore", always_fails)

    _run_loop(monkeypatch, place, me, ticks=supervisor._WATCHDOG_RESTORE_ATTEMPTS + 3)

    assert isinstance(place.journal().phase, Active)


def test_watchdog_never_signals_the_recorded_pid_through_a_fresh_scan_ref(profile, monkeypatch):
    """A scan hands back fresh `Ref`s, and after a clock step the recorded proxy's fresh ref
    differs from the persisted one while its persisted liveness is UNKNOWN. A scan must never
    re-authorise signalling a pid whose persisted ref is alive or unknown."""
    table = FakePsutil()
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, table=table, health=FakeHealth(sequence=[SILENT]))
    table.processes[proxy.pid].create_time += 100.0  # the clock stepped

    _run_loop(monkeypatch, place, me, ticks=5)

    assert (proxy.pid, "terminate") not in table.signalled
    assert (proxy.pid, "kill") not in table.signalled
    assert place.journal().phase == Restored(proxy), "alive-but-silent is kept for `down`"


def test_watchdog_stops_a_marked_orphan_before_releasing(profile, monkeypatch):
    """Any marked process the journal does not name is an orphan, and it is stopped — proven —
    before the record that would have said what to look for is unlinked."""
    table = FakePsutil()
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, table=table, health=FakeHealth(sequence=[SILENT]))
    table.processes[proxy.pid].gone = True
    orphan = table.spawn("proxy", 9099)

    _run_loop(monkeypatch, place, me, ticks=5)

    assert not table.alive(orphan)
    assert place.journal() == ownership.Absent()


def test_watchdog_keeps_restored_when_an_orphan_will_not_die(profile, monkeypatch):
    """A release over a marked process still running leaves nothing naming it."""
    table = FakePsutil()
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, table=table, health=FakeHealth(sequence=[SILENT]))
    table.processes[proxy.pid].gone = True
    orphan = table.spawn("proxy", 9099)
    table.processes[orphan].dies = False
    table.processes[orphan].waits = [psutil.TimeoutExpired(1)] * 6

    _run_loop(monkeypatch, place, me, ticks=5)

    assert place.pac() == CORPORATE, "the restore still happened"
    assert isinstance(place.journal().phase, Restored), "and the record is kept for `down`"


def test_watchdog_after_release_touches_nothing_of_a_successor_session(profile, monkeypatch):
    """The named worry: a watchdog paused between its unlink and its return, while a successor
    acquires on the same root. Nothing it does afterwards may reach the new session."""
    table = FakePsutil()
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, table=table, health=FakeHealth(sequence=[SILENT]))
    table.processes[proxy.pid].gone = True
    released, resume = threading.Event(), threading.Event()
    store = place.session
    real_locked = store.locked

    class _Paused:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self.inner.__enter__()

        def __exit__(self, *exc):
            answer = self.inner.__exit__(*exc)
            if not store.journal_path.exists():
                released.set()
                resume.wait(5)
            return answer

    store.locked = lambda timeout=session.LOCK_WAIT_SECONDS: _Paused(real_locked(timeout=timeout))
    thread = threading.Thread(target=lambda: supervisor.watchdog_loop(store, PORT, me))
    thread.start()
    assert released.wait(5), "the tick never released the journal"

    # A successor acquires on the same root while the old watchdog is paused after its unlink.
    table.processes.pop(me.pid, None)
    successor_proxy = table.spawn_ref("proxy", PORT)
    successor_watchdog = table.spawn_ref("watchdog", PORT)
    successor = record(Active(successor_proxy, successor_watchdog), baseline=CORPORATE)
    store.write(successor)
    place.network.set_pac("Wi-Fi", ours(PORT))
    before = store.journal_path.read_bytes()

    resume.set()
    thread.join(5)

    assert store.journal_path.read_bytes() == before, "the successor's journal is untouched"
    assert place.pac() == ours(PORT), "and so is its PAC"
    assert table.alive(successor_proxy.pid) and table.alive(successor_watchdog.pid)


# MARK: - The lock


def test_watchdog_waits_out_a_long_lock_holder(profile, monkeypatch):
    """An idempotent `up` can hold the lock for the length of a `simctl` relaunch. A watchdog that
    retired over contention would leave the session unwatched, so it blocks."""
    place, _, me = _watching(monkeypatch, pac=ours_off(PORT))
    holding = threading.Event()
    done = threading.Event()

    def hold():
        with place.session.locked():
            holding.set()
            time.sleep(0.4)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(5)

    def loop():
        _run_loop(monkeypatch, place, me)
        done.set()

    ticker = threading.Thread(target=loop)
    ticker.start()
    thread.join(5)
    ticker.join(5)

    assert done.is_set(), "it waited rather than retiring"
    assert place.pac() == ours(PORT), "and did its work once it got in"


def test_watchdog_releases_the_lock_between_failed_restore_attempts(profile, monkeypatch):
    """No decision keeps the lock across the sleep, so a persistent failure cannot hold the lock
    against `down`."""
    place, proxy, me = _watching(monkeypatch, baseline=CORPORATE, health=FakeHealth(sequence=[SILENT]))
    place.table.processes[proxy.pid].gone = True

    def always_fails(ref, port, baseline, lock_fd):
        raise supervisor.netproxy.NetworkSetupError("transient")

    monkeypatch.setattr(supervisor, "_restore", always_fails)
    got_in = threading.Event()

    def between_ticks(seconds):
        with place.session.locked(timeout=1.0):
            got_in.set()
        raise _Enough()

    monkeypatch.setattr(supervisor.time, "sleep", between_ticks)
    with pytest.raises(_Enough):
        supervisor.watchdog_loop(place.session, PORT, me)

    assert got_in.is_set(), "the lock was free between two attempts"


def test_down_waits_for_a_watchdog_repair_holding_the_lock_then_restores_after_it(profile, monkeypatch):
    """The lock is what serialises them, and it is held for the *whole* of a tick.

    Unlocked, a repair in flight switched the PAC back on after `down` had read it back clean, and
    the operator was left routed at a port whose proxy had just been stopped.
    """
    place, proxy, me = _watching(monkeypatch, pac=ours_off(PORT), baseline=CORPORATE)
    holding, release = threading.Event(), threading.Event()
    real_locked = place.session.locked

    def tick():
        with real_locked(timeout=None) as lock_fd:
            holding.set()
            release.wait(5)
            supervisor._repair(ownership.ServiceRef("Wi-Fi", "en0"), PORT, lock_fd)

    repair = threading.Thread(target=tick)
    repair.start()
    assert holding.wait(5)

    def let_it_finish():
        time.sleep(0.2)
        release.set()

    threading.Thread(target=let_it_finish).start()
    outcome = supervisor.down_session(session.Session(place.session.root))
    repair.join(5)

    assert outcome.exit_code == 0, outcome
    assert place.pac() == CORPORATE, "the repair finished before `down` read the PAC, not after"
    assert place.journal() == ownership.Absent()


# MARK: - The real child


def _isolated(root, *argv):
    return [sys.executable, str(Path(__file__).parent / "isolated_cli.py"), str(root), *argv]


def test_the_watchdog_child_reports_its_own_ref_and_dies_to_sigterm(profile, tmp_path, _no_real_watchdog):
    """The readiness line is parsed by the production parser from the production sender, and the
    child holds nothing once it is asked to stop: it blocks on the lock this test holds, and a
    SIGTERM there must leave the lock free."""
    root = tmp_path / "real-session"
    root.mkdir()
    store = session.Session(root)
    read_fd, write_fd = os.pipe()
    child = subprocess.Popen(
        _isolated(root, "_watchdog", "--control-port", "8099", "--ready-fd", str(write_fd)),
        pass_fds=(write_fd,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(write_fd)
    try:
        reported = supervisor._read_ready_line(read_fd, timeout=20.0)
        assert reported is not None and reported.pid == child.pid
        assert isinstance(reported, Ref) and reported.create_time > 0
    finally:
        os.close(read_fd)
        child.terminate()
        child.wait(10)

    with store.locked(timeout=5.0):
        pass  # the lock is free: the child held nothing it did not release
