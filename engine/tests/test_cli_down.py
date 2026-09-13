"""`lyrebird down`: the one recovery command, and the order it does things in.

Restore, then release the journal, then stop the proxy. The order is the whole design: a journal
kept after a successful restore let a *later* `down` read a baseline the operator had since enabled
by hand as RESUMABLE and switch it off again. With the journal gone the second `down` says "no
session" and touches nothing.

`down` reads no health and asks no proxy: it works from the journal, from any profile, port or
directory.
"""

import psutil
import pytest

import cli
import config
import session
from cli_doubles import CORPORATE, WIFI, FakeNetwork, FakeProc, FakePsutil, ours, ours_off, owner, record, world
from ownership import Absent, Pac, ServiceRef, SessionRecord

EMPTY = Pac("", False)
OTHER = Pac("http://proxy.example.net/other.pac", True)


def _up(monkeypatch, *, baseline=EMPTY, pac=None, services=None, route="en0", service=WIFI, table=None):
    """A machine with a live session: a journalled proxy, and the PAC it installed."""
    table = table if table is not None else FakePsutil()
    proxy = table.spawn_ref(config.CONTROL_PORT)
    found = ours() if pac is None else pac
    place = world(
        monkeypatch,
        record(proxy, baseline=baseline, service=service),
        services=services or {service.name: (service.device, found)},
        route=route,
        table=table,
    )
    place.proxy = proxy
    return place


# MARK: - nothing to stop


def test_down_with_no_session_exits_0(runner, monkeypatch):
    place = world(monkeypatch, None)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert "nothing to stop" in result.output
    assert place.network.calls == [], "nothing was even read"


def test_down_on_a_machine_with_no_session_root_exits_0(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(session, "default_root", lambda: tmp_path / "never-created")
    world(monkeypatch, None)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output


def test_down_refuses_an_unreadable_journal_and_touches_nothing(runner, monkeypatch):
    """The bytes may still describe somebody's PAC. Guessing at them is how a `down` restores a
    baseline nobody read — so it says what to do by hand and changes nothing."""
    place = world(monkeypatch, None)
    place.session.journal_path.write_text("{not json}", encoding="utf-8")

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "cannot be read" in result.output and "setautoproxystate" in result.output
    assert place.network.calls == []
    assert place.session.journal_path.exists(), "the bytes are left for the operator"


# MARK: - restoring


def test_down_switches_our_pac_off_and_releases_the_journal(runner, monkeypatch):
    place = _up(monkeypatch)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert "direct networking restored" in result.output
    assert place.pac() == ours_off(), "macOS rejects an empty URL, so Off is the flag alone"
    assert place.journal() == Absent()
    assert not place.table.alive(place.proxy.pid)


def test_down_puts_a_configured_baseline_back(runner, monkeypatch):
    place = _up(monkeypatch, baseline=CORPORATE)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert CORPORATE.url in result.output
    assert place.pac() == CORPORATE
    assert place.journal() == Absent()


def test_down_finishes_a_restore_that_failed_half_way(runner, monkeypatch):
    """RESUMABLE: the URL is already the baseline's and only the flag is wrong — one command, and
    the journal was kept by the `down` that could not finish."""
    place = _up(monkeypatch, baseline=CORPORATE, pac=Pac(CORPORATE.url, False))

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == CORPORATE
    assert [call[1] for call in place.network.setters()] == ["-setautoproxystate"], "one command, not two"


def test_down_over_an_already_restored_pac_writes_nothing(runner, monkeypatch):
    place = _up(monkeypatch, baseline=CORPORATE, pac=CORPORATE)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.network.setters() == []
    assert place.journal() == Absent()


@pytest.mark.parametrize(
    ("baseline", "found", "expected"),
    [
        (EMPTY, ours(), ["-setautoproxystate"]),
        (EMPTY, ours_off(), []),  # the residue an Off restore leaves already satisfies it
        (EMPTY, EMPTY, []),
        (CORPORATE, ours(), ["-setautoproxyurl"]),  # setting the URL switches it on as a side effect
        (CORPORATE, Pac(CORPORATE.url, False), ["-setautoproxystate"]),
        (Pac(CORPORATE.url, False), ours(), ["-setautoproxyurl", "-setautoproxystate"]),
    ],
)
def test_restore_issues_exactly_the_commands_the_target_needs(runner, monkeypatch, baseline, found, expected):
    """Every class × target, over the real recipe. A target already satisfied costs zero writes and
    is still a success; a disabled configured baseline costs the URL *and* the flag, because
    `-setautoproxyurl` switches the PAC on."""
    place = _up(monkeypatch, baseline=baseline, pac=found)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert [call[1] for call in place.network.setters()] == expected


def test_down_keeps_the_journal_when_a_setter_changes_nothing(runner, monkeypatch):
    """`networksetup` exits 0 without applying its change. Read back, that is an open obligation
    kept — never one abandoned with "stopped" printed over it."""
    place = _up(monkeypatch, baseline=CORPORATE)
    place.network.inert_setters()

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "did not restore" in result.output
    assert isinstance(place.journal(), SessionRecord), "the journal is the only record of the baseline"


def test_down_keeps_the_journal_when_a_setter_fails(runner, monkeypatch):
    place = _up(monkeypatch, baseline=CORPORATE)
    place.network.fail_at(3)  # the first setter, after the service table and the PAC read

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "could not restore" in result.output
    assert isinstance(place.journal(), SessionRecord)


# MARK: - a PAC that is no longer this session's


def test_down_over_a_foreign_pac_prints_the_baseline_and_keeps_the_journal(runner, monkeypatch):
    """A hand-set PAC is never written over. The proxy still goes — nothing should keep routing to
    it — and the journal stays: it is the only durable record of what was there before, and once
    the operator has put that back by hand, the next `down` finds it in place and releases."""
    place = _up(monkeypatch, baseline=CORPORATE, pac=OTHER)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert CORPORATE.url in result.output and "not restored" in result.output
    assert str(place.session.journal_path) in result.output, "the way out for somebody who meant it"
    assert place.pac() == OTHER, "untouched"
    assert place.network.setters() == []
    assert isinstance(place.journal(), SessionRecord)
    assert not place.table.alive(place.proxy.pid), "the proxy is stopped even so"

    place.network.set_pac("Wi-Fi", CORPORATE)  # by hand
    again = runner.invoke(cli.cli, ["down"])
    assert again.exit_code == 0 and "stopped" in again.output
    assert place.network.setters() == [], "found in place: nothing written"
    assert isinstance(place.journal(), Absent)


def test_down_keeps_the_journal_when_the_service_is_gone(runner, monkeypatch):
    place = _up(
        monkeypatch,
        baseline=CORPORATE,
        service=ServiceRef(name="Wi-Fi", device="en9"),
        services={"Wi-Fi": ("en0", ours())},
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "is gone" in result.output and CORPORATE.url in result.output
    assert place.network.setters() == []
    assert isinstance(place.journal(), SessionRecord)


def test_down_keeps_the_journal_when_the_service_is_ambiguous(runner, monkeypatch):
    place = _up(
        monkeypatch,
        baseline=CORPORATE,
        # The recorded name is gone and two services now carry its device: which one holds the PAC
        # is not something this Mac can be asked.
        services={"Work Wi-Fi": ("en0", ours()), "Wi-Fi copy": ("en0", EMPTY)},
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "ambiguous" in result.output
    assert isinstance(place.journal(), SessionRecord)


def test_down_keeps_the_journal_when_the_service_table_cannot_be_read(runner, monkeypatch):
    """A table that could not be read is not a service that is gone, and neither is a reason to
    write: both keep the journal, and only one of them says the service is missing."""
    place = _up(monkeypatch, baseline=CORPORATE)
    place.network.fail_at(1)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.network.setters() == []
    assert isinstance(place.journal(), SessionRecord)


def test_down_keeps_the_journal_when_the_pac_cannot_be_read(runner, monkeypatch):
    place = _up(monkeypatch, baseline=CORPORATE)
    place.network.fail_at(2)  # the PAC read, after the service table

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "could not be read" in result.output
    assert isinstance(place.journal(), SessionRecord)


def test_down_resolves_a_renamed_service_by_device(runner, monkeypatch):
    """The name is what `networksetup` takes; the device is what survives a rename. Writing to
    whatever now carries the old name would be somebody else's settings."""
    place = _up(monkeypatch, baseline=CORPORATE)
    place.network.rename("Wi-Fi", "Work Wi-Fi")

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac("Work Wi-Fi") == CORPORATE


def test_down_restores_a_service_that_is_disabled(runner, monkeypatch):
    """A `(*)` entry is still listed and still holds its PAC."""
    place = _up(monkeypatch, baseline=CORPORATE)
    place.network.disabled.add("Wi-Fi")

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == CORPORATE


# MARK: - stopping the proxy


def test_down_reports_a_proxy_that_will_not_stop_after_the_settings_are_back(runner, monkeypatch):
    table = FakePsutil()
    place = _up(monkeypatch, baseline=CORPORATE, table=table)
    spec = table.processes[place.proxy.pid]
    spec.dies = False  # it ignores SIGTERM and survives SIGKILL
    spec.waits = [psutil.TimeoutExpired(0.1), psutil.TimeoutExpired(0.1)]

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert f"kill -9 {place.proxy.pid}" in result.output
    assert place.pac() == CORPORATE, "the settings are back all the same"
    assert place.journal() == Absent(), "and the journal is released"


def test_down_never_touches_the_pac_after_a_restore_whose_stop_failed(runner, monkeypatch):
    """The reason the journal goes before the proxy is signalled.

    Kept, a later `down` would read a baseline the operator has since enabled by hand as RESUMABLE
    and switch it off again. Gone, the second `down` says "no session" and writes nothing.
    """
    table = FakePsutil()
    place = _up(monkeypatch, baseline=CORPORATE, table=table)
    spec = table.processes[place.proxy.pid]
    spec.dies = False  # the stop fails, and the restore before it did not
    spec.waits = [psutil.TimeoutExpired(0.1), psutil.TimeoutExpired(0.1)]
    assert runner.invoke(cli.cli, ["down"]).exit_code == 1
    place.network.set_pac("Wi-Fi", Pac(CORPORATE.url, False))  # the operator switches it off by hand
    before = len(place.network.calls)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert "nothing to stop" in result.output
    assert place.network.calls[before:] == [], "the second down reads nothing and writes nothing"
    assert place.pac() == Pac(CORPORATE.url, False)


def test_down_refuses_to_signal_a_pid_it_cannot_prove_is_this_sessions(runner, monkeypatch):
    """A reused pid. The marker and the port match and the create time does not, which is a clock
    step and a reuse at once — neither is proof, and doubt never signals."""
    table = FakePsutil()
    place = _up(monkeypatch, baseline=CORPORATE, table=table)
    table.processes[place.proxy.pid].create_time = place.proxy.create_time + 5

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert f"kill -9 {place.proxy.pid}" in result.output and "could not be proved" in result.output
    assert table.signalled == []
    assert place.pac() == CORPORATE and place.journal() == Absent()


def test_down_says_stopped_when_the_recorded_proxy_is_already_gone(runner, monkeypatch):
    table = FakePsutil()
    place = _up(monkeypatch, baseline=CORPORATE, table=table)
    table.processes[place.proxy.pid].gone = True

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert table.signalled == []


# MARK: - from anywhere


@pytest.mark.parametrize(
    "different",
    [{"port": 9099}, {"fingerprint": "deadbeef"}, {"state_root": "/tmp/lyrebird-tests/elsewhere"}],
    ids=["another control port", "another profile", "another state directory"],
)
def test_down_restores_a_session_it_does_not_own(runner, monkeypatch, different):
    """There is one session per user and `down` is the only recovery command: it must work from any
    profile, port or directory, and it reads no health to find out."""
    holder = owner(**different)
    table = FakePsutil()
    proxy = table.spawn_ref(holder.control_port)
    place = world(
        monkeypatch,
        record(proxy, baseline=CORPORATE, owned_by=holder),
        services={"Wi-Fi": ("en0", ours(holder.control_port))},
        table=table,
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == CORPORATE
    assert place.journal() == Absent()


def test_down_asks_no_proxy_at_all(runner, monkeypatch):
    """No health reading anywhere in `down`: a proxy with a hung control port must not be able to
    stop the network being put back."""
    place = _up(monkeypatch, baseline=CORPORATE)

    def refuse(*_args, **_kwargs):
        raise AssertionError("`down` must not read health")

    monkeypatch.setattr(cli.supervisor.api, "_health", refuse)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == CORPORATE


def test_down_refuses_while_another_command_holds_the_lock(runner, monkeypatch):
    place = _up(monkeypatch, baseline=CORPORATE)
    monkeypatch.setattr(session.Session, "locked", _busy)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.network.calls == []


def _busy(self, timeout=None):
    raise session.LockBusy(f"another Lyrebird command is holding {self.lock_path}")


def test_down_reports_a_journal_it_could_not_remove(runner, monkeypatch):
    """The settings are back, so that is said first — but a journal still on disk is a session the
    next `up` refuses over, and the operator has to hear the path."""
    place = _up(monkeypatch, baseline=CORPORATE)

    def refuse(_self):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(session.Session, "unlink", refuse)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "could not be removed" in result.output
    assert place.pac() == CORPORATE


def test_a_process_that_is_not_a_proxy_on_our_port_is_never_signalled(runner, monkeypatch):
    table = FakePsutil()
    place = _up(monkeypatch, baseline=CORPORATE, table=table)
    table.processes[place.proxy.pid] = FakeProc(
        cmdline=["/usr/bin/vim", "notes.txt"], create_time=place.proxy.create_time
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert table.signalled == []


def test_down_restores_over_a_network_whose_route_has_moved(runner, monkeypatch):
    """`down` never asks where the route is: the journal names the service it installed on, and
    that is the one to put back."""
    table = FakePsutil()
    proxy = table.spawn_ref(config.CONTROL_PORT)
    network = FakeNetwork({"Wi-Fi": ("en0", ours()), "Ethernet": ("en5", EMPTY)}, route="en5")
    place = world(monkeypatch, record(proxy, baseline=CORPORATE), table=table)
    place.network = network.install(monkeypatch)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac("Wi-Fi") == CORPORATE
    assert place.pac("Ethernet") == EMPTY
