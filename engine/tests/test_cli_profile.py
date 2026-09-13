"""Which profile a control call means.

One proxy holds the control port. With profile A running, `lyrebird --profile B use X` reached A,
switched A's scenario and printed success — so the operator watched an unchanged profile B. Every
call now names its profile, and a command that reads or writes for the wrong one fails.
"""

import json
import time

import pytest

import api
import cli
import config
import ownership
import procs
from cli_doubles import (
    _BASE_SEQ,
    FOREIGN_FINGERPRINT,
    FakeHealth,
    FakeNetwork,
    FakePsutil,
    _answers_with_a_conflict,
    _health_payload,
    _polling,
    _status_network,
    answering,
    body,
    record,
    up_after,
    write_journal,
)


def _records_the_request(monkeypatch, payload):
    """Capture the outgoing `Request` and answer it with `payload`."""
    seen = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        @staticmethod
        def read():
            return json.dumps(payload).encode()

    def fake_open(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = {name.lower(): value for name, value in request.header_items()}
        return _Response()

    # `api._open` is the one call every request goes through, and the autouse transport guard
    # replaces it: a double installed on `urllib.request.urlopen` would now double nothing.
    monkeypatch.setattr(api, "_open", fake_open)
    return seen


def test_a_mutation_names_the_profile_it_means(profile, runner, monkeypatch):
    """Without the header the API cannot tell a call meant for it from one meant for a profile that
    is not running, so it serves both and the caller never learns which one it changed."""
    seen = _records_the_request(monkeypatch, {"reloaded": 1, "active": "default"})

    result = runner.invoke(cli.cli, ["scenario", "reload"])

    assert result.exit_code == 0
    assert seen["headers"]["x-lyrebird-profile"] == config.PROFILE_FINGERPRINT


def test_a_read_names_the_profile_it_means_too(profile, monkeypatch):
    """A read answered by another profile's proxy reports its scenarios, counters and traffic as
    this profile's — a wrong answer, not a missing one."""
    seen = _records_the_request(monkeypatch, [])

    assert api._get_json("/__mock__/recent") == []
    assert seen["headers"]["x-lyrebird-profile"] == config.PROFILE_FINGERPRINT


def test_use_refuses_to_switch_a_scenario_in_someone_elses_profile(profile, runner, monkeypatch):
    """The bug in its original form: `--profile B use X` switched profile A and said "active: X"."""
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["use", "smoke"])

    assert result.exit_code == 1
    assert FOREIGN_FINGERPRINT in result.output, "say which profile actually holds the port"
    assert config.PROFILE_FINGERPRINT in result.output, "and which one was asked for"
    assert "lyrebird down" in result.output, "and how to get out of it"


def test_a_refused_call_does_not_print_the_bare_slug(profile, runner, monkeypatch):
    """`profile_mismatch` alone names neither fingerprint, so it reads as a bug in the command
    rather than as two proxies being confused for one."""
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["reset"])

    assert result.exit_code == 1
    assert "profile_mismatch" not in result.output


def test_up_over_another_profiles_session_sends_the_operator_to_down(profile, runner, monkeypatch):
    """There is one session per user, so a second `up` is refused whoever holds the journal — and
    the remedy is `lyrebird down`, never another control port: a different port would only change
    what this run requests while the journal stays exactly where it is.
    """
    table = FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    proxy = table.spawn_ref(config.CONTROL_PORT)
    network = FakeNetwork({"Wi-Fi": ("en0", ownership.Pac(ownership.our_url(config.CONTROL_PORT), True))})
    stranger = ownership.Owner(control_port=8088, profile_fingerprint=FOREIGN_FINGERPRINT, state_root="/tmp/elsewhere")
    up_after(monkeypatch, profile, record(proxy, owned_by=stranger), network, table)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert FOREIGN_FINGERPRINT in result.output
    assert "lyrebird down" in result.output
    assert "LYREBIRD_CONTROL_PORT" not in result.output, "another port changes nothing under one session"


@pytest.mark.parametrize(
    "command",
    [
        ["sequence", "wait", "ovr_a", "--step", "1"],
        ["assert-answered", "ovr_a"],
    ],
)
def test_a_command_reading_health_refuses_another_profiles_reading(profile, runner, monkeypatch, command):
    """Health is unscoped at the API — that is how `down` recovers across profiles — so a command
    that *interprets* a reading has to compare the fingerprint itself, or it reports a stranger's
    sequence cursors and answer counts as evidence about this profile's rules."""
    monkeypatch.setattr(
        api,
        "_health",
        lambda: _health_payload(
            profileFingerprint=FOREIGN_FINGERPRINT,
            sequences=[_BASE_SEQ],
            answers=[{"id": "ovr_a", "active": True, "count": 7}],
        ),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: pytest.fail("a mismatch must fail before any polling"))

    result = runner.invoke(cli.cli, command)

    assert result.exit_code == 1
    assert FOREIGN_FINGERPRINT in result.output
    assert "answered 7" not in result.output, "no claim may be made from the wrong profile's state"


def test_a_bound_assertion_reports_a_foreign_profile_as_unproven(profile, runner, monkeypatch):
    """The port can change hands between the reset and the assertion. Refusing is right, but under
    --run exit 1 claims "the rule is in your run and answered nothing" — about a run this command
    never got to look at, in a store that is not even the one the reset drew its boundary in."""
    monkeypatch.setattr(
        api,
        "_health",
        lambda: _health_payload(
            profileFingerprint=FOREIGN_FINGERPRINT,
            answers=[{"id": "ovr_a", "active": True, "count": 7, "runId": "run1"}],
        ),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: pytest.fail("a mismatch must fail before any polling"))

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1", "--timeout", "30"])

    assert result.exit_code == 3
    assert FOREIGN_FINGERPRINT in result.output
    assert "answered 7" not in result.output, "a matching run id from a stranger proves nothing"


def test_a_bound_assertion_refuses_a_profile_that_changes_under_the_poll(profile, runner, monkeypatch):
    """The same swap arriving mid-wait: this profile's proxy is stopped and another profile's is
    started on the port. The fingerprint is re-read every poll, so the wait ends where it lost the
    ability to answer — rather than burning its timeout on, or believing, a stranger's counters."""
    monkeypatch.setattr(
        api,
        "_health",
        _polling(
            [{}, {"profileFingerprint": FOREIGN_FINGERPRINT}],
            lambda state: _health_payload(
                answers=[{"id": "ovr_a", "active": True, "count": 0, "runId": "run1"}], **state
            ),
        ),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1", "--timeout", "30"])

    assert result.exit_code == 3
    assert FOREIGN_FINGERPRINT in result.output


def test_a_health_reading_without_a_fingerprint_is_refused(profile, runner, monkeypatch):
    """An engine that predates the field cannot say which profile it runs, and accepting it as ours
    let a current CLI and app read, activate and reset a stranger's profile behind a daemon that
    enforces no guard. Refused, naming the remedy: that engine's own `down`."""
    health = _health_payload(answers=[{"id": "ovr_a", "active": True, "count": 1}])
    del health["profileFingerprint"]
    monkeypatch.setattr(api, "_health", lambda: health)

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])

    assert result.exit_code != 0
    assert "none reported" in result.output and "lyrebird down" in result.output


def test_down_still_stops_a_proxy_that_belongs_to_another_profile(profile, runner, monkeypatch):
    """`down` is the recovery command: it must put the network back whatever profile is running, or
    the scoping added everywhere else would strand the Mac pointing at a proxy it may not name.

    The journal is the authority and it names this session; the *fingerprint* a proxy would report
    is not a fact `down` consults at all, because it reads no health.
    """
    table = FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    proxy = table.spawn_ref(config.CONTROL_PORT)
    corporate = ownership.Pac("http://proxy.example.com/corp.pac", True)
    network = FakeNetwork({"Wi-Fi": ("en0", ownership.Pac(ownership.our_url(config.CONTROL_PORT), True))})
    network.install(monkeypatch)
    stranger = ownership.Owner(control_port=8088, profile_fingerprint=FOREIGN_FINGERPRINT, state_root="/tmp/elsewhere")
    write_journal(record(proxy, baseline=corporate, owned_by=stranger))

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert network.pac("Wi-Fi") == corporate, "the baseline is back"
    assert not table.alive(proxy.pid)


def test_status_json_says_null_when_the_engine_cannot_report(profile, runner, monkeypatch):
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


def test_status_json_reports_a_discovery_timeout_rather_than_printing_nothing(profile, runner, monkeypatch):
    """`--json` is what a script reads. An uncaught error from discovery printed no JSON at all,
    so the caller could not tell "not intercepting" from "the command fell over" — and the exit
    code is 1 for both. The reason goes where every other unproven PAC goes."""
    FakeHealth(sequence=[answering(4321)]).install(monkeypatch)
    FakeNetwork().die_at(1).install(monkeypatch)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["service"] is None
    assert payload["intercepting"] is False
    assert "did not finish within 5s" in payload["pacError"]
