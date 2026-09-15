"""`lyrebird status`: what it reports, and what it refuses to call success.

It answers for the *requested* owner — this profile, this port — and reports the journal beside it.
Exit 0 means the whole postcondition held at one observation: this session's journal, its proxy
answering, its PAC routing here, on the service the route still carries. Every other combination is
exit 1 with the reason printed, because `lyrebird status && …` is a thing people write.

No process is inspected: the health reading's pid and the journal are the evidence, and psutil is
not consulted at all.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import cli
import config
import ownership
import procs
import session
from cli_doubles import (
    SILENT,
    FakeHealth,
    FakeNetwork,
    FakePsutil,
    _answers_over,
    _health_payload,
    _status_network,
    answering,
    body,
    ours,
    owner,
    record,
    write_journal,
)
from ownership import Pac

CORPORATE = Pac("http://proxy.example.com/corp.pac", True)


def _healthy(monkeypatch, **payload):
    """A machine as a successful `up` leaves it, with a health reading to match."""
    world = _status_network(monkeypatch)
    world["health"] = FakeHealth(sequence=[answering(world["proxy"].pid, **payload)]).install(monkeypatch)
    return world


def _quiet(monkeypatch, *, services=None, journal=None):
    """A closed control port, and whatever journal the case is about."""
    monkeypatch.setattr(procs, "psutil", FakePsutil())
    network = FakeNetwork(services).install(monkeypatch)
    FakeHealth(sequence=[SILENT]).install(monkeypatch)
    write_journal(journal)
    return network


# MARK: - The happy path, and what it takes


def test_status_exits_0_only_when_the_whole_postcondition_holds(profile, runner, monkeypatch):
    world = _healthy(monkeypatch)

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "INTERCEPT" in result.output
    assert world["network"].pac("Wi-Fi") == ours()


def test_status_json_reports_the_service_and_pac_the_journal_describes(profile, runner, monkeypatch):
    world = _healthy(monkeypatch)

    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)

    assert payload["service"] == "Wi-Fi"
    assert payload["pac"] == {"url": ours().url, "enabled": True, "ours": True}
    assert payload["intercepting"] is True and payload["journalError"] is None
    assert world["health"].asked == [config.CONTROL_PORT], "the requested owner's port, once"


def test_status_json_has_no_session_block(profile, runner, monkeypatch):
    _healthy(monkeypatch)

    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)

    assert "session" not in payload


def test_status_json_reports_the_simulator_the_session_trusted(profile, runner, monkeypatch):
    world = _status_network(monkeypatch)
    FakeHealth(sequence=[answering(world["proxy"].pid)]).install(monkeypatch)
    write_journal(record(world["proxy"], simulator=ownership.Simulator(udid="PHONE-1", name="iPhone 17 Pro")))

    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)

    assert payload["simulator"] == {"udid": "PHONE-1", "name": "iPhone 17 Pro"}


def test_status_json_carries_answer_counts(profile, runner, monkeypatch):
    """AGENTS.md documents `answers` in `status --json`; it was in /health and never forwarded."""
    world = _status_network(monkeypatch)
    counts = _answers_over({"count": 2})
    FakeHealth(sequence=[lambda port: body({**counts(), "pid": world["proxy"].pid})]).install(monkeypatch)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert json.loads(result.output)["answers"] == [{"id": "ovr_a", "active": True, "count": 2, "runId": "run1"}]


def test_status_json_carries_sequences(profile, runner, monkeypatch):
    world = _status_network(monkeypatch)
    sequences = [
        {
            "id": "ovr_a",
            "runId": "r1",
            "advanceOn": "self",
            "nextStep": 1,
            "stepCount": 2,
            "exhausted": False,
            "hasOverrun": False,
            "serves": {},
        }
    ]
    FakeHealth(sequence=[answering(world["proxy"].pid, sequences=sequences)]).install(monkeypatch)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    assert '"sequences"' in result.output and "ovr_a" in result.output


def test_status_warns_about_scenario_files_changed_since_load(profile, runner, monkeypatch):
    """The proxy serving a file's previous contents looked, in every command, like one serving the
    file. The warning is the one place that says otherwise; it names the remedy and is not a reason,
    so the exit code still reports interception."""
    _healthy(monkeypatch, staleScenarioFiles=["orders-outage.json", "checkout/added.json"])

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "changed since the proxy read them: orders-outage.json, checkout/added.json" in result.output
    assert "lyrebird scenario reload" in result.output


def test_status_warns_about_changed_files_even_when_the_pac_cannot_be_read(profile, runner, monkeypatch):
    """The warning sits outside the banner's branches: a PAC read failure changes what the banner
    says, not whether the files changed."""
    world = _healthy(monkeypatch, staleScenarioFiles=["orders-outage.json"])
    world["network"].fail_at(3)  # the PAC read, after the route and the service table

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1, result.output
    assert "PAC UNREADABLE" in result.output
    assert "changed since the proxy read them: orders-outage.json" in result.output


def test_status_json_passes_stale_scenario_files_through_and_reports_null_when_unsent(profile, runner, monkeypatch):
    world = _status_network(monkeypatch)
    FakeHealth(
        sequence=[
            answering(world["proxy"].pid, staleScenarioFiles=["orders-outage.json"]),
            answering(world["proxy"].pid),
        ]
    ).install(monkeypatch)

    listed = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)
    unsent = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)

    assert listed["staleScenarioFiles"] == ["orders-outage.json"]
    assert unsent["staleScenarioFiles"] is None, "an engine that does not send it cannot be read as 'nothing changed'"


def test_status_output_and_exit_code_come_from_one_reading(profile, runner, monkeypatch):
    """A status taken from one fetch and fields printed from another can disagree about which proxy
    answered — and a `status` whose text says one thing and whose `$?` says another is worse than
    either being wrong on its own."""
    world = _status_network(monkeypatch)
    alternating = iter([answering(world["proxy"].pid), answering(999_201, profileFingerprint="deadbeefcafe")])
    FakeHealth(sequence=[lambda port: next(alternating, SILENT)]).install(monkeypatch)

    result = runner.invoke(cli.cli, ["status", "--json"])

    payload = json.loads(result.output)
    assert (result.exit_code == 0) == (payload["intercepting"] and not payload["profileMismatch"])


def test_status_consults_no_process_table(profile, runner, monkeypatch):
    """The health reading's pid and the journal are the evidence. A psutil lookup here would make
    `status` refuse over a clock step that changed nothing about whether traffic is intercepted."""
    world = _healthy(monkeypatch)

    def refuse(_pid):
        raise AssertionError("`status` must not inspect the process table")

    monkeypatch.setattr(world["table"], "Process", refuse)

    assert runner.invoke(cli.cli, ["status"]).exit_code == 0


# MARK: - Every reason it is not 0


def test_status_exits_1_when_the_recorded_proxy_is_not_what_answers(profile, runner, monkeypatch):
    world = _status_network(monkeypatch)
    FakeHealth(sequence=[answering(999_201)]).install(monkeypatch)

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert f"the recorded proxy (pid {world['proxy'].pid})" in result.output
    # Not ACTIVE beside exit 1: whatever answers on the port is not the proxy this session started.
    assert "INTERCEPT ACTIVE" not in result.output
    as_json = runner.invoke(cli.cli, ["status", "--json"])
    assert as_json.exit_code == 1 and json.loads(as_json.output)["intercepting"] is False


def test_status_exits_1_when_the_route_moved_off_the_journalled_device(profile, runner, monkeypatch):
    """The PAC is on the service the session took. Once the route is elsewhere, nothing of this
    profile's traffic reaches the proxy however healthy every other reading looks."""
    world = _healthy(monkeypatch)
    world["network"].services["Ethernet"] = ["en1", Pac("", False)]
    world["network"].route = "en1"

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert "route moved" in result.output
    # Not ACTIVE beside exit 1: our PAC on a service the route left routes nothing, and the banner
    # said otherwise while the exit code said this.
    assert "INTERCEPT ACTIVE" not in result.output

    as_json = runner.invoke(cli.cli, ["status", "--json"])
    assert as_json.exit_code == 1
    assert json.loads(as_json.output)["intercepting"] is False


def test_status_treats_a_proxy_without_a_fingerprint_as_foreign(profile, runner, monkeypatch):
    """A proxy that cannot say whose it is predates the guard; read as ours it let the app act on a
    stranger's profile. It is another profile until it says otherwise."""
    _healthy(monkeypatch, profileFingerprint=None)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 1
    state = json.loads(result.output)
    assert state["profileMismatch"] is True and state["intercepting"] is False


def test_status_exits_1_when_the_pac_is_not_this_sessions(profile, runner, monkeypatch):
    world = _healthy(monkeypatch)
    world["network"].set_pac("Wi-Fi", CORPORATE)

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert "is not routing to this session" in result.output


def test_status_exits_1_when_the_journalled_service_cannot_be_resolved(profile, runner, monkeypatch):
    world = _healthy(monkeypatch)
    world["network"].services["Wi-Fi"][0] = "en9"  # the recorded device is carried by nothing now

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert "could not be read" in result.output or "is gone" in result.output


def test_status_reports_the_proxys_own_journal_error_and_exits_1(profile, runner, monkeypatch):
    """The proxy reads the journal too, and its answer is the one that matters for the `down` this
    session promises: reported whatever the local read says, because the proxy may be the one that
    cannot read it."""
    world = _status_network(monkeypatch)
    FakeHealth(sequence=[answering(world["proxy"].pid, journalError="session.json cannot be read: bad JSON")]).install(
        monkeypatch
    )

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert "cannot be read" in result.output

    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)
    assert "cannot be read" in payload["journalError"]


def test_status_reports_a_journal_it_cannot_read_itself(profile, runner, monkeypatch):
    """The other source of the same field. Reported, and exit 1: `down` will not be able to restore
    from this record."""
    world = _status_network(monkeypatch)
    FakeHealth(sequence=[answering(world["proxy"].pid)]).install(monkeypatch)
    session.Session().journal_path.write_bytes(b"not json at all\xff")

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["journalError"] is not None


def test_status_exits_1_when_another_session_owns_the_pac(profile, runner, monkeypatch):
    """A journal belonging to another owner is not this profile's interception, however healthy the
    port looks."""
    table = FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    proxy = table.spawn_ref(config.CONTROL_PORT)
    FakeNetwork({"Wi-Fi": ("en0", ours())}).install(monkeypatch)
    FakeHealth(sequence=[answering(proxy.pid)]).install(monkeypatch)
    write_journal(record(proxy, owned_by=owner(port=9099, fingerprint="ffffffffffff")))

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert "another session owns the PAC" in result.output
    assert "9099" in result.output


def test_status_exits_1_with_no_session_at_all(profile, runner, monkeypatch):
    """The CI launcher check's case: a clean machine, a closed port, and no journal. Reading the
    absent root must create nothing."""
    _quiet(monkeypatch)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["proxyUp"] is False and payload["pac"] == {"url": "", "enabled": False, "ours": False}
    assert not session.Session().journal_path.exists()


def test_status_names_the_routes_service_when_no_journal_does(profile, runner, monkeypatch):
    _quiet(monkeypatch, services={"Wi-Fi": ("en0", CORPORATE)})

    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)

    assert payload["service"] == "Wi-Fi"
    assert payload["pac"] == {"url": CORPORATE.url, "enabled": True, "ours": False}


def test_status_json_says_null_for_a_field_the_engine_cannot_report(profile, runner, monkeypatch):
    """A proxy still running from before these fields existed cannot answer the question. Reporting
    `[]` would say "nothing has answered", which is a different claim from "I could not ask"."""
    world = _status_network(monkeypatch)
    FakeHealth(sequence=[body({"pid": world["proxy"].pid, "activeScenario": "default", "scenarios": []})]).install(
        monkeypatch
    )

    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)

    assert payload["answers"] is None and payload["sequences"] is None


def test_status_json_says_empty_when_the_engine_reports_nothing_to_show(profile, runner, monkeypatch):
    """The other half of the distinction: a current engine sends one entry per rule, so an empty
    list is a real state and must not be confused with the case above."""
    world = _status_network(monkeypatch)
    FakeHealth(sequence=[answering(world["proxy"].pid)]).install(monkeypatch)

    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)

    assert payload["answers"] == [] and payload["sequences"] == []
    assert payload["profileFingerprint"] == _health_payload()["profileFingerprint"]


# MARK: - A real process, against a port nothing is listening on


def test_status_prints_json_and_exits_non_zero_against_a_dead_control_port(profile, tmp_path):
    """A real interpreter, a real (closed) port, and a session root of this test's own: `--json`
    must still print JSON a script can read, because the exit code alone cannot say why.

    It goes through `isolated_cli.py` rather than `cli.py` because a subprocess inherits no
    monkeypatch, and the journal lives at one fixed per-user path — a contributor's own.
    """
    root = tmp_path / "real-session"
    root.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]  # bound and released: nothing is listening there now
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "isolated_cli.py"), str(root), "status", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "LYREBIRD_PROFILE": str(profile),
            "LYREBIRD_STATE_DIR": str(tmp_path / "state"),
            "LYREBIRD_CONTROL_PORT": str(dead_port),
        },
    )

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["proxyUp"] is False
    assert "no session holds the PAC" in result.stderr or payload["pac"] is not None
    assert not (root / "session.json").exists(), "a read must create nothing"
