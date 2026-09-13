"""`lyrebird status`: what it reports, and what it refuses to call success.

It answers for the *requested* owner — this profile, this port — and reports the journal beside it.
Exit 0 means the whole postcondition held at one observation: this session active, its proxy
answering, its watchdog alive, its PAC routing here, on the service the route still carries. Every
other combination is exit 1 with the reason printed, because `lyrebird status && …` is a thing
people write.
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
    record,
    write_journal,
)
from ownership import Active, Pac, Restored

CORPORATE = Pac("http://proxy.example.com/corp.pac", True)


def _healthy(monkeypatch, **payload):
    """A machine as a successful `up` leaves it, with a health reading to match."""
    world = _status_network(monkeypatch)
    reader = FakeHealth(sequence=[answering(world["proxy"].pid, **payload)]).install(monkeypatch)
    world["health"] = reader
    return world


# MARK: - The happy path, and what it takes


def test_status_exits_0_only_when_the_whole_postcondition_holds(profile, runner, monkeypatch):
    world = _healthy(monkeypatch)

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "INTERCEPT" in result.output
    assert world["network"].pac("Wi-Fi") == ours()


def test_status_json_reports_the_session_the_journal_describes(profile, runner, monkeypatch):
    """The journal is additional information, not the exit code: a reader has to be able to see the
    phase, whose it is, which service it took and whether the watchdog is still there."""
    world = _healthy(monkeypatch)

    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)

    assert payload["session"] == {
        "phase": "active",
        "owner": {"controlPort": config.CONTROL_PORT, "profileFingerprint": config.PROFILE_FINGERPRINT},
        "service": {"name": "Wi-Fi", "device": "en0"},
        "watchdog": "alive",
        "archive": None,
    }
    assert payload["intercepting"] is True and payload["journalError"] is None
    assert world["health"].asked == [config.CONTROL_PORT], "the requested owner's port, once"


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


# MARK: - Every reason it is not 0


def test_status_reports_a_dead_watchdog_as_lost_restoration(profile, runner, monkeypatch):
    """The proxy answers and the PAC routes to it, so everything a person can see is fine — and
    nothing will put the network back if the proxy dies. That is the whole point of saying so."""
    world = _healthy(monkeypatch)
    world["table"].processes[world["watchdog"].pid].gone = True

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert "watchdog dead" in result.output and "lyrebird down && lyrebird up" in result.output
    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)
    assert payload["session"]["watchdog"] == "dead"


def test_status_exits_1_when_the_route_moved_off_the_journalled_device(profile, runner, monkeypatch):
    """The PAC is on the service the session took. Once the route is elsewhere, nothing of this
    profile's traffic reaches the proxy however healthy every other reading looks."""
    world = _healthy(monkeypatch)
    world["network"].services["Ethernet"] = ["en1", Pac("", False)]
    world["network"].route = "en1"

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert "route moved" in result.output


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

    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)

    assert payload["journalError"] is not None
    assert payload["session"] == {
        "phase": "unreadable",
        "owner": None,
        "service": None,
        "watchdog": "unknown",
        "archive": None,
    }


def test_status_reports_an_archived_session_with_its_path(profile, runner, monkeypatch):
    """An archived session means the previous settings were never put back. `status` says so and
    names the file, because that file is what a person needs to put them back by hand."""
    table = FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    FakeNetwork().install(monkeypatch)
    FakeHealth(sequence=[SILENT]).install(monkeypatch)
    write_journal(
        ownership.Archived(
            version=ownership.SESSION_VERSION,
            since="20260912T101500Z",
            reason="displaced",
            path="/tmp/lyrebird-tests/archive/20260912T101500Z-displaced-ab12cd.json",
            context=ownership.Known(
                owner=ownership.Owner(config.CONTROL_PORT, config.PROFILE_FINGERPRINT, str(config.STATE_ROOT)),
                service=ownership.ServiceRef("Wi-Fi", "en0"),
                proxy=None,
            ),
        )
    )

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert "archived:" in result.output and "ab12cd.json" in result.output


def test_status_exits_1_when_another_session_owns_the_pac(profile, runner, monkeypatch):
    """A journal belonging to another owner is not this profile's interception, however healthy the
    port looks."""
    table = FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    proxy = table.spawn_ref("proxy", config.CONTROL_PORT)
    watchdog = table.spawn_ref("watchdog", config.CONTROL_PORT)
    FakeNetwork({"Wi-Fi": ("en0", ours())}).install(monkeypatch)
    FakeHealth(sequence=[answering(proxy.pid)]).install(monkeypatch)
    stranger = ownership.Owner(control_port=9099, profile_fingerprint="ffffffffffff", state_root="/tmp/elsewhere")
    write_journal(record(Active(proxy, watchdog), owned_by=stranger))

    result = runner.invoke(cli.cli, ["status"])

    assert result.exit_code == 1
    assert "another session owns the PAC" in result.output
    assert "9099" in result.output


def test_status_exits_1_with_no_session_at_all(profile, runner, monkeypatch):
    """The CI launcher check's case: a clean machine, a closed port, and no journal. Reading the
    absent root must create nothing."""
    table = FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    FakeNetwork().install(monkeypatch)
    FakeHealth(sequence=[SILENT]).install(monkeypatch)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["proxyUp"] is False and payload["session"]["phase"] == "absent"
    assert not session.Session().journal_path.exists()


def test_status_reports_a_restored_session_as_not_intercepting(profile, runner, monkeypatch):
    """A terminal checkpoint is not a running session, and `status` must not describe it as one."""
    table = FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    FakeNetwork({"Wi-Fi": ("en0", CORPORATE)}).install(monkeypatch)
    FakeHealth(sequence=[SILENT]).install(monkeypatch)
    write_journal(record(Restored(None), baseline=CORPORATE))

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["session"]["phase"] == "restored" and payload["session"]["watchdog"] == "none"
    assert payload["intercepting"] is False


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
    assert payload["session"]["phase"] == "absent"
    assert not (root / "session.json").exists(), "a read must create nothing"
