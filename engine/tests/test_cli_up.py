"""`lyrebird up`: taking the PAC, and the unwind that puts it back when the run cannot finish.

A fresh acquisition journals its intent before it starts anything, and from that first write on
*any* failure unwinds — which is `down`'s own row for whatever is on disk, run in-process on the
lock this `up` already holds. An idempotent `up` never unwinds: it took no obligation, and
discharging somebody else's is how a working session loses its watchdog.
"""

import psutil

import api
import cli
import config
import ownership
import procs
import session
import simulator as sim
import supervisor
from cli_doubles import (
    SILENT,
    FakeHealth,
    FakeNetwork,
    FakePopen,
    FakePsutil,
    answering,
    archived,
    fake_simctl,
    ours,
    ours_off,
    record,
    spawning_proxy,
    up_after,
)
from ownership import Absent, Acquiring, Active, Pac, Restored

CORPORATE = Pac("http://proxy.example.com/corp.pac", True)
OTHER = Pac("http://proxy.example.net/other.pac", True)
EMPTY = Pac("", False)


def _fresh(monkeypatch, profile, *, services=None, route="en0", journal=None, table=None, health=None):
    """A machine ready for a fresh acquisition: no session, an empty PAC, nothing running."""
    table = table if table is not None else FakePsutil()
    network = FakeNetwork(services, route=route)
    reader = up_after(monkeypatch, profile, journal, network, table, health=health)
    return _Place(network, table, reader)


class _Place:
    def __init__(self, network, table, health):
        self.network = network
        self.table = table
        self.health = health
        self.session = session.Session()

    def journal(self):
        return self.session.read()

    def pac(self, name="Wi-Fi"):
        return self.network.pac(name)

    def archives(self):
        directory = self.session.archive_dir
        return sorted(directory.glob("*.json")) if directory.is_dir() else []


def _idempotent(monkeypatch, profile, *, table=None, health=None, services=None):
    """An `up` over this very session, already active with its PAC installed."""
    table = table if table is not None else FakePsutil()
    proxy = table.spawn_ref("proxy", config.CONTROL_PORT)
    watchdog = table.spawn_ref("watchdog", config.CONTROL_PORT)
    place = _fresh(
        monkeypatch,
        profile,
        services=services or {"Wi-Fi": ("en0", ours())},
        journal=record(Active(proxy, watchdog)),
        table=table,
        health=health,
    )
    return place, proxy, watchdog


# MARK: - A fresh acquisition


def test_up_takes_the_pac_and_journals_an_active_session(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    journal = place.journal()
    assert isinstance(journal.phase, Active)
    assert journal.baseline == CORPORATE, "what was there before, verbatim"
    assert place.pac() == ours()


def test_up_installs_over_the_disabled_residue_a_down_leaves(profile, runner, monkeypatch):
    """`up → down → up`. An Off restore leaves our own URL disabled, because macOS rejects an empty
    one — so the second `up` must accept that residue as "there was nothing here" rather than
    recording it as a baseline to put back."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", ours_off())})

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert place.journal().baseline == EMPTY, "our own disabled URL is never the thing to restore"
    assert place.pac() == ours()


def test_up_refuses_an_enabled_lyrebird_pac_with_no_journal(profile, runner, monkeypatch):
    """An enabled Lyrebird PAC that no journal claims is somebody's unowned session — on any port.
    Acquiring over it strands whatever it points at, with nothing left naming the baseline."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", ours(9099))})

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.pac() == ours(9099)
    assert "lyrebird down" in result.output


def test_up_refuses_over_a_marked_orphan_on_another_port(profile, runner, monkeypatch):
    """A Lyrebird process with no session is unresolved recovery evidence, whatever port it is on:
    the scan finds it, and `up` sends the operator to `down` rather than starting a second one."""
    table = FakePsutil()
    table.spawn("proxy", 9099)
    place = _fresh(monkeypatch, profile, table=table)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "9099" in result.output and "lyrebird down" in result.output
    assert place.journal() == Absent()


def test_up_refuses_when_the_route_moved_and_deletes_nothing(profile, runner, monkeypatch):
    """Hot migration is gone. The record belongs to the old service and the PAC there is still
    ours; moving it would drop the only description of what to put back on a service this run can
    no longer see."""
    table = FakePsutil()
    proxy = table.spawn_ref("proxy", config.CONTROL_PORT)
    watchdog = table.spawn_ref("watchdog", config.CONTROL_PORT)
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", ours()), "Ethernet": ("en1", EMPTY)},
        route="en1",
        journal=record(Active(proxy, watchdog), baseline=CORPORATE),
        table=table,
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "moved" in result.output and "lyrebird down && lyrebird up" in result.output
    assert place.journal().baseline == CORPORATE, "nothing about the old service was dropped"
    assert place.pac() == ours()


def test_up_refuses_a_moved_route_even_when_the_new_services_pac_is_unreadable(profile, runner, monkeypatch):
    """The route is judged before the PAC, and no PAC is read on either service: read on the new
    one and failed, an unreadable answer would turn the refusal into a preserve and say nothing
    about the move."""
    table = FakePsutil()
    proxy = table.spawn_ref("proxy", config.CONTROL_PORT)
    watchdog = table.spawn_ref("watchdog", config.CONTROL_PORT)
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", ours()), "Ethernet": ("en1", EMPTY)},
        route="en1",
        journal=record(Active(proxy, watchdog), baseline=CORPORATE),
        table=table,
    )
    place.network.die_at(3)  # the route, the service table, then the PAC read

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "moved" in result.output


def test_up_refuses_when_two_services_share_the_route_device(profile, runner, monkeypatch):
    """Which of them holds the PAC is not something this Mac can be asked, and a session recorded
    against the wrong one restores a service it never installed on."""
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", EMPTY), "Wi-Fi (2)": ("en0", EMPTY)},
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.network.setters() == []


def test_up_refuses_on_the_journal_without_touching_simctl(profile, runner, monkeypatch):
    """`sim._run` has no timeout, so an `up` refused on the journal alone must not hold the session
    lock for the length of a hanging `simctl`. The device is resolved only after the decision."""
    table = FakePsutil()
    place = _fresh(monkeypatch, profile, journal=archived("displaced"), table=table)
    calls = fake_simctl(monkeypatch, [])

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert calls == [], "nothing was asked of simctl"
    assert place.network.calls == [], "and nothing of networksetup"


def test_up_refuses_when_the_observed_pac_cannot_be_journalled(profile, runner, monkeypatch):
    """The writer proves its own reader accepts a record before publishing it, so a PAC that cannot
    be journalled verbatim refuses acquisition with nothing started — rather than a session whose
    recovery record nothing can read."""
    enormous = Pac("http://proxy.example.com/" + "a" * (ownership.MAX_STRING + 1), True)
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", enormous)})

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.network.setters() == []
    assert place.table.marked_pid("proxy", config.CONTROL_PORT) is None, "nothing was started"


def test_up_refuses_to_install_over_a_pac_that_changed_since_it_looked(profile, runner, monkeypatch):
    """`_install`'s pre-write read must equal the acquisition observation exactly. A PAC that moved
    in between is one this run never decided about, and writing over it would lose a baseline
    nothing recorded."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    # After the acquisition read, immediately before `_install`'s own pre-write read.
    place.network.write_at(5, "Wi-Fi", OTHER)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.pac() == OTHER, "zero writes"
    assert place.network.setters() == []


def test_a_foreign_write_inside_the_install_window_is_overwritten_without_trace(profile, runner, monkeypatch):
    """The documented limit, asserted as a loss rather than as a detection that cannot exist: an
    edit that lands between `_install`'s accepted read and its setter is simply overwritten, and
    the baseline recorded is the one observed before it."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    place.network.write_at(7, "Wi-Fi", OTHER)  # after the pre-write read, before the URL setter

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert place.pac() == ours()
    assert place.journal().baseline == CORPORATE, "the edit inside the window left no trace"


def test_up_install_happens_over_acquiring_ref_and_an_unchanged_pac(profile, runner, monkeypatch):
    """The install follows `Acquiring(ref)` — so a crash during it leaves a record naming the child
    — and it happens over the PAC the acquisition observed, not over whatever is there now."""
    _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    seen = {}
    real = supervisor._install

    def watched(ref, port, expected, lock_fd):
        seen["phase"] = session.Session().read().phase
        seen["expected"] = expected
        return real(ref, port, expected, lock_fd)

    monkeypatch.setattr(supervisor, "_install", watched)

    assert runner.invoke(cli.cli, ["up"]).exit_code == 0
    assert isinstance(seen["phase"], Acquiring) and seen["phase"].proxy is not None
    assert seen["expected"] == CORPORATE


# MARK: - Idempotence


def test_up_is_idempotent_only_with_the_same_owner_and_a_live_watchdog(profile, runner, monkeypatch):
    """An `up` over this session's own active record installs nothing and starts nothing. A
    watchdog that is not alive is not an idempotent run — it is a session whose restoration is
    already lost, and starting a second proxy over it would strand the first."""
    place, _, watchdog = _idempotent(monkeypatch, profile)

    assert runner.invoke(cli.cli, ["up"]).exit_code == 0
    assert place.network.setters() == []

    place.table.processes[watchdog.pid].gone = True
    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.network.setters() == [], "and nothing was installed over the session either"


def test_up_repairs_its_own_disabled_pac(profile, runner, monkeypatch):
    """macOS silently switches the PAC off while the proxy is alive. That is ours to switch back
    on, and the one write an idempotent `up` may make."""
    place, _, _ = _idempotent(monkeypatch, profile, services={"Wi-Fi": ("en0", ours_off())})

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert place.pac() == ours()
    assert [call[1] for call in place.network.setters()] == ["-setautoproxystate"]


def test_up_preserves_when_the_proxy_reports_the_journal_broken(profile, runner, monkeypatch):
    """A repair over a journal the proxy cannot read would re-enable a PAC whose baseline may be
    gone. The proxy reads the journal too, and it saying so is the one fact that stops the run."""
    table = FakePsutil()
    proxy = table.spawn_ref("proxy", config.CONTROL_PORT)
    watchdog = table.spawn_ref("watchdog", config.CONTROL_PORT)
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", ours_off())},
        journal=record(Active(proxy, watchdog)),
        table=table,
        health=FakeHealth(table, payload={"journalError": "session.json cannot be read"}),
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.network.setters() == []
    assert "lyrebird down" in result.output


def test_up_idempotent_failure_keeps_active(profile, runner, monkeypatch):
    """The obligation is an earlier `up`'s and this run never took it, so a CA failure here exits 1
    with the session exactly as it was — never a discharge of somebody else's record."""
    place, _, _ = _idempotent(monkeypatch, profile)
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (False, "keychain failed"))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert isinstance(place.journal().phase, Active)
    assert place.pac() == ours(), "the PAC an earlier `up` installed is untouched"


def test_idempotent_up_keeps_the_old_simulator_when_trusting_the_new_one_fails(profile, runner, monkeypatch):
    """The journal's `simulator` means "the device holding the CA". An `up --simulator B` whose
    trust fails must leave the record bound to A, or `relaunch` sends the app to a device without
    the certificate."""
    table = FakePsutil()
    proxy = table.spawn_ref("proxy", config.CONTROL_PORT)
    watchdog = table.spawn_ref("watchdog", config.CONTROL_PORT)
    device_a = ownership.Simulator(udid="PHONE-1", name="iPhone 17 Pro")
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", ours())},
        journal=record(Active(proxy, watchdog), simulator=device_a),
        table=table,
    )
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (False, "keychain failed"))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal().simulator == device_a


def test_up_records_the_simulator_only_after_trusting_it(profile, runner, monkeypatch):
    """A CA failure followed by an unwind must not leave a record saying a device holds a
    certificate it was never given."""
    place = _fresh(monkeypatch, profile)
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (False, "keychain failed"))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent(), "the acquisition unwound"


def test_up_records_the_resolved_simulator_in_the_journal(profile, runner, monkeypatch):
    """`status` and `relaunch` both read it from there: the question they answer is which device
    *this run* trusted, which a fresh lookup cannot say."""
    place = _fresh(monkeypatch, profile)

    assert runner.invoke(cli.cli, ["up"]).exit_code == 0
    assert place.journal().simulator == ownership.Simulator(udid="PHONE-1", name="iPhone 17 Pro")


# MARK: - The unwind


def test_up_unwinds_when_the_log_or_spawn_fails(profile, runner, monkeypatch):
    """The first journal write arms the unwind. A `Popen` that raises leaves no process and no PAC
    change — and the `Acquiring(None)` record it wrote is released rather than left for a `down`
    that has nothing to do."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    spawning_proxy(monkeypatch, place.table, raises=OSError("no such file: mitmdump"))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.pac() == CORPORATE, "nothing was installed, so nothing is restored"


def test_up_exits_without_effects_when_its_first_journal_write_fails_before_replace(profile, runner, monkeypatch):
    """Before `tmp.replace` the file is absent, and `down`'s absent row — the machine-wide sweep —
    is not an `up`'s to run."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})

    def cannot_write(self, record_):
        raise OSError("no space left on device")

    monkeypatch.setattr(session.Session, "write", cannot_write)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.network.setters() == []
    assert place.table.marked_pid("proxy", config.CONTROL_PORT) is None


def test_up_stops_its_child_when_it_cannot_record_it(profile, runner, monkeypatch):
    """A child whose record could not be written is stopped, not left: persistently failing, the
    ref write leaves `Acquiring(None)` on disk, and that row stops what was spawned (proven),
    touches no PAC, writes no terminal record and releases the journal."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    writes = {"n": 0}
    real = session.Session.write

    def sometimes(self, record_):
        writes["n"] += 1
        if writes["n"] == 1:
            return real(self, record_)
        raise OSError("no space left on device")

    monkeypatch.setattr(session.Session, "write", sometimes)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.table.marked_pid("proxy", config.CONTROL_PORT) is None, "the child was stopped"
    assert place.journal() == Absent(), "nothing remains to be recovered"
    assert place.network.setters() == []


def test_up_unwind_never_resumes_a_baseline_over_an_unrecorded_ref(profile, runner, monkeypatch):
    """`Acquiring(None)` is the process-only row: no PAC is read and none is written. A baseline
    URL somebody enabled by hand meanwhile must never be "resumed" on the strength of a ref that
    was never durably recorded."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    writes = {"n": 0}
    real = session.Session.write

    def sometimes(self, record_):
        writes["n"] += 1
        if writes["n"] == 1:
            return real(self, record_)
        place.network.set_pac("Wi-Fi", OTHER)
        raise OSError("no space left on device")

    monkeypatch.setattr(session.Session, "write", sometimes)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.pac() == OTHER, "the unwind touched no PAC at all"
    assert place.network.setters() == []


def test_up_unwind_follows_the_phase_actually_on_disk_when_a_write_fails_after_replace(profile, runner, monkeypatch):
    """A write can fail *after* `tmp.replace` has made the record visible, so the unwind never
    assumes the failed write left the old phase: it re-reads and runs the matching row."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    writes = {"n": 0}
    real = session.Session.write

    def visible_then_failing(self, record_):
        writes["n"] += 1
        real(self, record_)
        if writes["n"] == 2:  # `Acquiring(ref)` is on disk, and the caller is told it failed
            raise OSError("fsync: input/output error")

    monkeypatch.setattr(session.Session, "write", visible_then_failing)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.table.marked_pid("proxy", config.CONTROL_PORT) is None, "the recorded child was stopped"
    assert place.journal() == Absent()
    assert place.pac() == CORPORATE, "nothing was installed, so nothing was restored over"


def test_up_unwind_stops_its_child_under_an_incomplete_scan(profile, runner, monkeypatch):
    """Scan uncertainty prevents the *release*, not the cleanup of a child this run started and can
    prove."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    writes = {"n": 0}
    real = session.Session.write

    def sometimes(self, record_):
        writes["n"] += 1
        if writes["n"] == 1:
            return real(self, record_)
        place.table.pids_error = PermissionError("operation not permitted")
        raise OSError("no space left on device")

    monkeypatch.setattr(session.Session, "write", sometimes)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.table.signalled, "the child this run started was signalled anyway"
    assert isinstance(place.journal(), ownership.SessionRecord), "but the journal is kept"


def test_up_unwinds_and_releases_when_the_proxy_never_became_healthy(profile, runner, monkeypatch):
    """Nothing was installed, so the unwind's second decision is `AlreadyRestored`: the journal is
    checkpointed and released, and the PAC is exactly as this `up` found it."""
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", CORPORATE)},
        health=FakeHealth(sequence=[SILENT]),
    )
    monkeypatch.setattr(supervisor, "_STARTUP_DEADLINE_SECONDS", 0.01)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.pac() == CORPORATE
    assert place.table.marked_pid("proxy", config.CONTROL_PORT) is None


def test_up_unwind_refuses_when_another_pid_took_the_port(profile, runner, monkeypatch):
    """The startup port race. The journal is kept — something holds the port this session's record
    names — but the child this run started and that lost the race has no business running."""
    table = FakePsutil()
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", CORPORATE)},
        table=table,
        health=FakeHealth(sequence=[SILENT, answering(999_004)]),
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.table.marked_pid("proxy", config.CONTROL_PORT) is None, "the child that lost the race is dead"
    assert place.network.setters() == []


def test_up_unwinds_under_the_lock_when_the_watchdog_never_signals_ready(profile, runner, monkeypatch):
    """The readiness wait is part of the acquisition, and it happens on the lock this `up` already
    holds — so a watchdog that never reports is unwound before anything else can see the session."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})

    def never_ready(port):
        raise supervisor.WatchdogSpawnFailed("nothing was read from the readiness pipe", None)

    monkeypatch.setattr(supervisor, "_spawn_watchdog", never_ready)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.pac() == CORPORATE, "the PAC this run installed is back"


def test_up_journals_the_watchdog_ref_the_child_reports(profile, runner, monkeypatch):
    """The readiness message is the child's own serialised `Ref`, not a byte: the journal records
    *that* value, so what the child later compares itself against is by construction what it sent,
    and a clock step between two independent reads cannot make them disagree."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    reported = ownership.Ref(pid=6001, create_time=1234.5)

    def spawn(port):
        place.table.spawn("watchdog", port, pid=reported.pid, create_time=9999.0)
        return reported

    monkeypatch.setattr(supervisor, "_spawn_watchdog", spawn)

    assert runner.invoke(cli.cli, ["up"]).exit_code == 1  # the final look cannot prove that ref alive
    journal = place.journal()
    assert journal == Absent() or journal.phase.watchdog == reported


def test_up_unwind_archives_rather_than_restores_over_a_foreign_pac(profile, runner, monkeypatch):
    """The unwind decides on what it observes, through `decide_down` — so a PAC that became
    somebody else's while this `up` was starting is archived, exactly as `down` would."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (False, "keychain failed"))
    # The install has happened by the time the CA is tried; a person changes the PAC now.
    real = sim.trust_ca_in_sim

    def changes_the_pac(simulator):
        place.network.set_pac("Wi-Fi", OTHER)
        return real(simulator)

    monkeypatch.setattr(sim, "trust_ca_in_sim", changes_the_pac)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.pac() == OTHER, "a PAC this session does not own is never written over"
    assert len(place.archives()) == 1


def test_up_unwind_preserves_the_watchdog_on_an_unreadable_pac(profile, runner, monkeypatch):
    """Preserve comes before any fence. A final PAC read that failed must not cost a session that
    reached `Active` the process that would have restored it."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (False, "keychain failed"))
    calls = {"n": 0}
    real = place.network._run

    def failing_reads(args, check=False, *, pass_fds=()):
        if args[1:2] == ["-getautoproxyurl"]:
            calls["n"] += 1
            if calls["n"] > 3:  # after the acquisition, the install's pre-write read and its read-back
                raise supervisor.netproxy.NetworkSetupError("`networksetup -getautoproxyurl Wi-Fi` failed")
        return real(args, check=check, pass_fds=pass_fds)

    monkeypatch.setattr(supervisor.netproxy, "_run", failing_reads)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    journal = place.journal()
    assert isinstance(journal, ownership.SessionRecord) and isinstance(journal.phase, Active)
    assert place.table.alive(journal.phase.watchdog.pid), "nothing was fenced"


def test_up_unwind_keeps_the_phase_when_the_watchdog_will_not_die(profile, runner, monkeypatch):
    """An unwind that cannot fence leaves the journal in its phase for `down`, and names the pid."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (False, "keychain failed"))

    def immortal(port):
        pid = place.table.spawn("watchdog", port)
        place.table.processes[pid].dies = False
        place.table.processes[pid].waits = [psutil.TimeoutExpired(1)] * 6
        return place.table.ref(pid)

    monkeypatch.setattr(supervisor, "_spawn_watchdog", immortal)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    journal = place.journal()
    assert isinstance(journal.phase, Active)
    assert str(journal.phase.watchdog.pid) in result.output
    assert place.pac() == ours(), "the PAC stays as it is for `down` to decide about"


def test_up_unwind_keeps_restored_when_the_proxy_will_not_die(profile, runner, monkeypatch):
    """The fence and the restore succeed and the proxy survives SIGKILL: `Restored(ref)` is kept
    for `down`, exactly as `down`'s own row leaves it."""
    table = FakePsutil()
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)}, table=table)
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (False, "keychain failed"))

    def popen(argv, **kwargs):
        pid = table.spawn("proxy", config.CONTROL_PORT)
        table.processes[pid].dies = False
        table.processes[pid].waits = [psutil.TimeoutExpired(1)] * 8
        return FakePopen(pid)

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    journal = place.journal()
    assert isinstance(journal.phase, Restored)
    assert place.pac() == CORPORATE, "the baseline is back even though the proxy would not stop"


def test_up_unwinds_a_fresh_acquisition_when_the_relaunch_fails(profile, runner, monkeypatch):
    """plan-v6's "any failure unwinds": a relaunch that failed leaves the operator with the network
    as they had it, rather than a proxy they must remember to stop."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8"
    )
    config.reload_profile()
    phone = {"udid": "PHONE-1", "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}
    fake_simctl(monkeypatch, [phone], launch_status=1)
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (True, "trusted"))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.pac() == CORPORATE


def test_up_final_look_fails_when_the_route_moved_during_startup(profile, runner, monkeypatch):
    """The PAC is installed on the service the route carried when the run began. If the route moved
    while the proxy was starting, nothing of this profile's traffic is being intercepted — and a
    banner saying otherwise is the claim this command exists not to make."""
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", CORPORATE), "Ethernet": ("en1", EMPTY)},
    )
    real = sim.trust_ca_in_sim

    def moves_the_route(simulator):
        place.network.route = "en1"
        return real(simulator)

    monkeypatch.setattr(sim, "trust_ca_in_sim", moves_the_route)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "route moved" in result.output or "moved" in result.output
    assert place.journal() == Absent(), "the acquisition unwound"


def test_up_fails_when_its_final_look_reports_a_broken_journal(profile, runner, monkeypatch):
    """The proxy reads the journal too. It saying the journal is unreadable means the `down` this
    `up` promises cannot restore from it, so the run cannot be reported as a success."""
    table = FakePsutil()
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", CORPORATE)},
        table=table,
        health=FakeHealth(table, payload={"journalError": "session.json cannot be read"}),
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "journal" in result.output
    assert place.journal() == Absent(), "a fresh acquisition that cannot be proved unwinds"


def test_up_use_selects_through_the_health_reading_it_decided_on(profile, runner, monkeypatch):
    """`--use` checks its scenario against the reading from the proxy *this run* started, not the
    one taken before it existed."""
    readings = []
    table = FakePsutil()

    def health(port):
        pid = table.marked_pid("proxy", port)
        readings.append(pid)
        return SILENT if pid is None else answering(pid, scenariosNotWhole={"orders-outage": []})

    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", CORPORATE)},
        table=table,
        health=FakeHealth(sequence=[health]),
    )
    selected = []

    def control(path, method="GET", payload=None, timeout=3.0):
        selected.append(payload["name"])
        return {"active": payload["name"], "previous": None}

    monkeypatch.setattr(api, "_control", control)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 0, result.output
    assert selected == ["orders-outage"]
    assert readings[0] is None and readings[1] is not None, "the first reading found nothing; the second, this run's"
    assert place.journal() is not None


def test_up_is_refused_while_the_releasing_watchdog_still_exists_then_succeeds(profile, runner, monkeypatch):
    """The benign transient: a watchdog that has released the journal but not yet exited is still a
    marked Lyrebird process, so the next `up` refuses — and succeeds the moment it is gone."""
    table = FakePsutil()
    retiring = table.spawn("watchdog", config.CONTROL_PORT)
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)}, table=table)

    assert runner.invoke(cli.cli, ["up"]).exit_code == 1
    assert place.journal() == Absent()

    place.table.processes.pop(retiring)
    assert runner.invoke(cli.cli, ["up"]).exit_code == 0
    assert isinstance(place.journal().phase, Active)


def test_up_spawns_no_real_watchdog_subprocess(profile, runner, monkeypatch, _no_real_watchdog):
    """The fixture that keeps a real watchdog off a contributor's own Wi-Fi is load-bearing, and a
    test that forgets it leaves a process behind and passes anyway."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    started = []

    def popen(argv, **kwargs):
        started.append(argv)
        return FakePopen(place.table.spawn("proxy", config.CONTROL_PORT))

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)

    runner.invoke(cli.cli, ["up"])

    assert not any("_watchdog" in argv for argv in started), "the fixture is what keeps this true"


def test_the_watchdog_argv_is_one_procs_can_read_back(profile, monkeypatch, _no_real_watchdog):
    """The readiness fd and the control port are on the command line because `procs.marked_as`
    reads them back out of the process table — which is the only way a watchdog says which session
    it belongs to."""
    argv = supervisor._watchdog_argv(9099, 7)
    assert procs.marked_as(argv) == ("watchdog", 9099)
    assert "--ready-fd" in argv and "7" in argv


def test_the_proxy_argv_is_one_procs_can_read_back(profile, monkeypatch):
    assert procs.marked_as(supervisor._proxy_argv()) == ("proxy", config.CONTROL_PORT)
    assert not any("state_root_id" in token for token in supervisor._proxy_argv())
