"""`lyrebird up`: taking the PAC, and the unwind that puts it back when the run cannot finish.

One session per user, so `up` refuses while a journal exists — this profile's or anybody's. Once it
has journalled the proxy it started, *any* failure unwinds: the settings go back, the journal goes
away and the child is stopped, because a half-acquired session nobody was told about is worse than
no session at all.
"""

import pytest

import cli
import config
import session
import simulator as sim
import supervisor
from cli_doubles import (
    SILENT,
    FakeHealth,
    FakeNetwork,
    FakePsutil,
    answering,
    fake_simctl,
    ours,
    ours_off,
    owner,
    record,
    spawning_proxy,
    up_after,
)
from ownership import Absent, Pac, Ref, SessionRecord, Unreadable

CORPORATE = Pac("http://proxy.example.com/corp.pac", True)
EMPTY = Pac("", False)


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


def _fresh(monkeypatch, profile, *, services=None, route="en0", journal=None, table=None, health=None):
    """A machine ready for a fresh acquisition: no session, an empty PAC, nothing running."""
    table = table if table is not None else FakePsutil()
    network = FakeNetwork(services, route=route)
    reader = up_after(monkeypatch, profile, journal, network, table, health=health)
    return _Place(network, table, reader)


# MARK: - A fresh acquisition


def test_up_takes_the_pac_and_journals_the_session(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    journal = place.journal()
    assert isinstance(journal, SessionRecord)
    assert journal.baseline == CORPORATE, "what was there before, verbatim"
    assert journal.proxy.pid == place.table.marked_pid(config.CONTROL_PORT)
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


# MARK: - refusals that touch nothing


def test_up_refuses_over_a_journal_of_its_own_owner(profile, runner, monkeypatch):
    """One session per user: even this profile's own. `up --use X` on a running session is
    `lyrebird use X && lyrebird relaunch`, and the message has to say so."""
    place = _fresh(monkeypatch, profile, journal=record(Ref(pid=4321, create_time=1.0)))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "already up" in result.output and "lyrebird use" in result.output
    assert place.pac() == EMPTY and place.network.setters() == []


def test_up_refuses_over_another_owners_journal(profile, runner, monkeypatch):
    place = _fresh(
        monkeypatch,
        profile,
        journal=record(Ref(pid=4321, create_time=1.0), owned_by=owner(port=9099, fingerprint="deadbeef")),
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "9099" in result.output and "deadbeef" in result.output
    assert place.network.setters() == []


def test_up_refuses_on_the_journal_without_touching_simctl(profile, runner, monkeypatch):
    """`sim._run` has no timeout: an `up` refused on the journal alone must not hold the session
    lock for the length of a hanging `simctl`."""
    _fresh(monkeypatch, profile, journal=record(Ref(pid=4321, create_time=1.0)))
    calls = fake_simctl(monkeypatch, [])

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert calls == []


def test_up_refuses_over_an_unreadable_journal(profile, runner, monkeypatch):
    """A record that cannot be read may still be holding somebody's PAC, so it is refused hardest
    of all — and the remedy names `down`, which is what reads it next."""
    place = _fresh(monkeypatch, profile)
    place.session.journal_path.write_text("{not json}", encoding="utf-8")

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "cannot be read" in result.output and "lyrebird down" in result.output
    assert isinstance(place.journal(), Unreadable)
    assert place.network.setters() == []


def test_up_refuses_an_enabled_unowned_lyrebird_pac(profile, runner, monkeypatch):
    """An enabled Lyrebird PAC that no journal claims is somebody's unowned session — on any port.
    Acquiring over it strands whatever it points at, with nothing left naming the baseline."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", ours(9099))})

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.pac() == ours(9099)
    assert "setautoproxystate" in result.output


def test_up_refuses_when_this_mac_has_no_default_route(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile, route=None)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not detect the active network service" in result.output
    assert place.journal() == Absent()


def test_up_refuses_when_two_services_carry_the_route_device(profile, runner, monkeypatch):
    """Which one holds the PAC is not something this Mac can be asked, and guessing is how a
    session ends up recorded against a service whose PAC it never installed."""
    place = _fresh(
        monkeypatch,
        profile,
        services={"Wi-Fi": ("en0", EMPTY), "Wi-Fi copy": ("en0", EMPTY)},
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "two network services carry device 'en0'" in result.output
    assert place.journal() == Absent()


def test_up_refuses_when_the_pac_cannot_be_read(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile)
    place.network.fail_at(3)  # route, service order, then the PAC read

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.network.setters() == []


# MARK: - installing


def test_up_refuses_to_install_over_a_pac_that_changed_since_it_looked(profile, runner, monkeypatch):
    """The pre-write read must equal the reading `up` decided on, verbatim, or nothing is written
    at all: the baseline in the journal would otherwise describe a PAC that is no longer there."""
    place = _fresh(monkeypatch, profile)
    # The install's own read is the first command after the proxy is healthy; a stranger's write
    # lands immediately before it.
    place.network.write_at(4, "Wi-Fi", CORPORATE)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "changed while this `up` was deciding" in result.output
    assert place.network.setters() == [], "zero writes over a reading it did not admit"
    assert place.pac() == CORPORATE, "what landed is left exactly as it is"


def test_a_foreign_write_inside_the_install_window_is_overwritten(profile, runner, monkeypatch):
    """The documented limit (R1/R11). Between the install's read-back and the next command there is
    no lock macOS offers, so a PAC set by hand in that window is overwritten without trace — and
    the baseline recorded is the one `up` read *before* it, not the one that landed."""
    place = _fresh(monkeypatch, profile)
    place.network.write_at(5, "Wi-Fi", CORPORATE)  # after the pre-write read, before `-setautoproxyurl`

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert place.journal().baseline == EMPTY
    assert place.pac() == ours()


def test_up_unwinds_when_the_install_fails(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", CORPORATE)})
    place.network.fail_at(5)  # the `-setautoproxyurl`

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.pac() == CORPORATE, "what was there is what is there"


# MARK: - startup


def test_up_unwinds_when_the_proxy_never_becomes_healthy(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile, health=FakeHealth(sequence=[SILENT]))
    monkeypatch.setattr(supervisor, "_STARTUP_DEADLINE_SECONDS", 0.0)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent(), "the journal is gone"
    assert place.pac() == EMPTY and place.network.setters() == [], "the PAC was never touched"
    assert place.table.signalled, "the child this run started is stopped"


def test_up_unwinds_when_the_proxy_exits_on_startup(profile, runner, monkeypatch):
    table = FakePsutil()
    place = _fresh(monkeypatch, profile, table=table, health=FakeHealth(sequence=[SILENT]))
    spawning_proxy(monkeypatch, table, dead=True)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "exited on startup" in result.output
    assert place.journal() == Absent()


def test_up_unwinds_when_another_pid_answers_on_the_port(profile, runner, monkeypatch):
    """The startup port race: adopting a reading from another pid would install a PAC for a proxy
    this run did not start, and `down` would then signal the wrong one."""
    place = _fresh(monkeypatch, profile, health=FakeHealth(sequence=[answering(pid=9999)]))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "another proxy (pid 9999)" in result.output
    assert place.journal() == Absent()
    assert place.network.setters() == []


def test_up_stops_the_child_when_the_journal_cannot_be_written(profile, runner, monkeypatch):
    """Nothing is installed and there is no journal to unwind from, so the child this run started
    is stopped here or it is never stopped at all."""
    place = _fresh(monkeypatch, profile)

    def refuse(_self, _record):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(session.Session, "write", refuse)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not write the session journal" in result.output
    assert place.network.setters() == []
    assert place.table.signalled, "the proxy this run started was stopped"


def test_up_exits_1_when_the_proxy_cannot_be_started(profile, runner, monkeypatch):
    table = FakePsutil()
    place = _fresh(monkeypatch, profile, table=table)
    spawning_proxy(monkeypatch, table, raises=OSError(2, "no such file"))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not be started" in result.output
    assert place.journal() == Absent() and place.network.setters() == []


# MARK: - the CA, the relaunch and the final look


def test_up_records_the_simulator_only_after_trusting_it(profile, runner, monkeypatch):
    """The journal's `simulator` means "the device holding the CA": a failed trust must not leave
    the session bound to a device without one."""
    place = _fresh(monkeypatch, profile)
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (False, "keychain failed"))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent(), "a failed CA trust unwinds the whole acquisition"
    assert place.pac() == ours_off(), "the disabled residue an Off restore leaves"


def test_up_records_the_device_it_trusted(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert place.journal().simulator.udid == "PHONE-1"


def test_up_unwinds_when_the_relaunch_fails(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile)
    monkeypatch.setattr(sim, "_relaunch", lambda bundle_id, simulator: (False, "not installed"))
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8"
    )
    config.reload_profile()

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.pac() == ours_off(), "the network is back: macOS rejects an empty URL, so Off is the flag"


def test_up_unwinds_when_the_route_moved_during_startup(profile, runner, monkeypatch):
    """The final look is the postcondition: a PAC installed on a service the route has since left
    intercepts nothing, however well every step before it went."""
    place = _fresh(monkeypatch, profile, services={"Wi-Fi": ("en0", EMPTY), "Ethernet": ("en5", EMPTY)})
    real_run = place.network._run

    def moving(args, check=False):
        result = real_run(args, check=check)
        if args[:2] == ["route", "-n"]:
            place.network.route = "en5"  # the next `route` call answers with the new device
        return result

    monkeypatch.setattr(supervisor.netproxy, "_run", moving)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "route moved" in result.output.lower()
    assert place.journal() == Absent()
    assert place.pac() == ours_off()


def test_up_unwinds_when_the_final_look_finds_another_pid_on_the_port(profile, runner, monkeypatch):
    table = FakePsutil()
    started = table.spawn(config.CONTROL_PORT, pid=7001)
    health = FakeHealth(sequence=[answering(pid=started), answering(pid=9999)])
    place = _fresh(monkeypatch, profile, table=table, health=health)
    spawning_proxy(monkeypatch, table, pid=started)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert place.journal() == Absent()
    assert place.pac() == ours_off()


def test_up_unwinds_when_the_proxy_reports_the_journal_unreadable(profile, runner, monkeypatch):
    """The proxy reads the journal too; it saying the journal is broken means the `down` this `up`
    promises cannot restore from it."""
    table = FakePsutil()
    started = table.spawn(config.CONTROL_PORT, pid=7002)
    health = FakeHealth(
        sequence=[answering(pid=started), answering(pid=started, journalError="session.json: unknown key")]
    )
    place = _fresh(monkeypatch, profile, table=table, health=health)
    spawning_proxy(monkeypatch, table, pid=started)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "journal unreadable" in result.output
    assert place.journal() == Absent()


def test_up_unwinds_when_something_unexpected_raises_after_the_install(profile, runner, monkeypatch):
    """Not every failure is one this code named: a transport error nobody mapped, a Ctrl-C in the
    startup wait. Whatever it is, the network goes back — an exception that escaped `up` left the
    PAC installed at a port whose proxy this run had just abandoned."""
    table = FakePsutil()
    started = table.spawn(config.CONTROL_PORT, pid=7005)
    place = _fresh(monkeypatch, profile, table=table, health=FakeHealth(sequence=[answering(pid=started)]))
    spawning_proxy(monkeypatch, table, pid=started)

    def boom(*_args, **_kwargs):
        raise RuntimeError("nobody expected this")

    monkeypatch.setattr(supervisor, "_relaunch", boom)

    result = runner.invoke(cli.cli, ["up"])

    assert isinstance(result.exception, RuntimeError), "re-raised, not swallowed"
    assert "putting the network back" in result.output
    assert place.journal() == Absent()
    assert place.pac() == ours_off()
    assert not table.alive(started)


def test_up_reports_a_simulator_it_could_not_resolve_and_unwinds(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile)
    fake_simctl(monkeypatch, [])

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "CA was NOT trusted" in result.output
    assert place.journal() == Absent()


# MARK: - `--use`


def test_up_use_selects_through_the_health_reading_it_decided_on(profile, runner, monkeypatch):
    """The scenario is selected before the relaunch, from the reading `up` already has: asking
    again would only widen the window in which the answer moves."""
    place = _fresh(monkeypatch, profile)
    selected = []
    monkeypatch.setattr(
        supervisor.api,
        "_control",
        lambda path, method="GET", payload=None, timeout=3.0: (
            selected.append(payload["name"]),
            {"active": payload["name"]},
        )[1],
    )
    place.health.payload = {"scenariosNotWhole": {}, "scenarios": ["default", "orders-outage"]}

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 0, result.output
    assert selected == ["orders-outage"]


def test_up_use_refuses_to_relaunch_against_a_scenario_that_did_not_load_whole(profile, runner, monkeypatch):
    place = _fresh(monkeypatch, profile)
    place.health.payload = {"scenariosNotWhole": {"orders-outage": ["override 2 dropped"]}}

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert "did not load whole" in result.output
    assert place.journal() == Absent(), "the acquisition unwound"


@pytest.mark.parametrize("argv", [["up", "--relaunch", "com.example.Store", "--no-relaunch"], ["up", "--use", "  "]])
def test_up_refuses_contradictory_usage_before_anything_starts(profile, runner, monkeypatch, argv):
    """ "Which of these two did you mean" is not a question to ask after the network has been
    rewired."""
    place = _fresh(monkeypatch, profile)

    result = runner.invoke(cli.cli, argv)

    assert result.exit_code == 2
    assert place.journal() == Absent() and place.network.calls == []
