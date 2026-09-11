"""Which profile a control call means.

One proxy holds the control port. With profile A running, `lyrebird --profile B use X` reached A,
switched A's scenario and printed success — so the operator watched an unchanged profile B. Every
call now names its profile, and a command that reads or writes for the wrong one fails.
"""

import json
import time
import urllib.request

import pytest

import api
import cli
import config
import netproxy
import rules
import supervisor
from cli_doubles import (
    _BASE_SEQ,
    FOREIGN_FINGERPRINT,
    _answers_over,
    _answers_with_a_conflict,
    _discovery_times_out,
    _health_payload,
    _polling,
    _status_network,
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

    def fake_urlopen(request, timeout=None):
        seen["headers"] = {name.lower(): value for name, value in request.header_items()}
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_a_mutation_names_the_profile_it_means(profile, runner, monkeypatch):
    """Without the header the API cannot tell a call meant for it from one meant for a profile that
    is not running, so it serves both and the caller never learns which one it changed."""
    seen = _records_the_request(monkeypatch, {"id": "ovr_a", "active": True})

    result = runner.invoke(cli.cli, ["override", "add", '{"mode":"replace","status":204}'])

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


def test_a_polling_read_refused_for_the_wrong_profile_is_not_reported_as_unreachable(profile, runner, monkeypatch):
    """`_get_json` answers None for "not reachable", and a 409 is the opposite of that: the proxy is
    up and talking. Reporting it as silence sends the operator to look for a dead port."""
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["wait-ready", "--timeout", "1"])

    assert result.exit_code == 1
    assert "no proxied requests" not in result.output
    assert FOREIGN_FINGERPRINT in result.output


def test_up_refuses_to_adopt_a_proxy_running_another_profile(profile, runner, monkeypatch):
    """`up` must not report INTERCEPT ACTIVE for a proxy serving somebody else's rules."""
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    config.reload_profile()
    monkeypatch.setattr(api, "_health", lambda: _health_payload(profileFingerprint=FOREIGN_FINGERPRINT))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert FOREIGN_FINGERPRINT in result.output
    assert "lyrebird down" in result.output


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


def test_a_bound_assertion_refuses_a_profile_that_takes_the_port_before_the_diagnostic_read(
    profile, runner, monkeypatch
):
    """The last call a failing assertion makes is the `/recent` read behind its "what did arrive"
    hint, and unlike `/health` that one is scoped, so the API refuses it with a 409. Exiting 1 there
    prints a profile mismatch under the code that means "your run was checked and nothing answered",
    which is the one reading of exit 1 that has to stay true."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 0, "runId": "run1"}))
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])

    assert result.exit_code == 3
    assert FOREIGN_FINGERPRINT in result.output


def test_an_unbound_assertion_still_exits_one_when_the_diagnostic_read_is_refused(profile, runner, monkeypatch):
    """The same moment without --run: no boundary was claimed, so nothing here may start returning
    a code the older contract never had."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 0, "runId": "run1"}))
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])

    assert result.exit_code == 1
    assert FOREIGN_FINGERPRINT in result.output


def test_a_health_reading_without_a_fingerprint_is_still_accepted(profile, runner, monkeypatch):
    """An engine that predates the field cannot say which profile it runs. Refusing it would turn
    an upgrade into a breakage, so — exactly as `up` does — a missing fingerprint is allowed."""
    health = _health_payload(answers=[{"id": "ovr_a", "active": True, "count": 1}])
    del health["profileFingerprint"]
    monkeypatch.setattr(api, "_health", lambda: health)

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])

    assert result.exit_code == 0


def test_down_still_stops_a_proxy_that_belongs_to_another_profile(profile, runner, fake_network, monkeypatch):
    """`down` is the recovery command: it must put the network back whatever is running, or the
    scoping added everywhere else would strand the Mac pointing at a proxy it may not name."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    monkeypatch.setattr(
        api,
        "_health",
        lambda: None if fake_network["terminated"] else {"pid": 4242, "profileFingerprint": FOREIGN_FINGERPRINT},
    )

    monkeypatch.setattr(
        supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS
    )  # this state directory's

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1, "the record is unreadable, so the previous PAC is unknown — never 0 here"
    assert fake_network["restored"] == ("Wi-Fi", "", False), "ours switched off whoever owns the proxy"
    assert (4242, "addon.py") in fake_network["terminated"]
    assert f"runs another profile ({FOREIGN_FINGERPRINT}) — stopping it anyway" in result.output


def test_override_add_help_lists_every_override_field(profile, runner):
    """Validation now rejects anything outside this vocabulary, so a field missing from the help is
    a rule the author cannot write and cannot find out about."""
    result = runner.invoke(cli.cli, ["override", "add", "--help"])
    assert result.exit_code == 0
    # The block itself, not substrings: `body` is a substring of `bodyContains` in the matcher
    # block below it, so `field in output` passes with the `body` entry deleted.
    block = result.output.split("An override accepts these fields", 1)[1].split("`match` accepts", 1)[0]
    listed = {line.split()[0] for line in block.splitlines() if line.startswith("    ")}
    assert listed == set(rules.OVERRIDE_FIELDS)


def test_override_add_help_lists_every_matcher_field(profile, runner):
    """The capability that already existed but could not be found from the tool itself."""
    result = runner.invoke(cli.cli, ["override", "add", "--help"])
    for field in rules.MATCHER_FIELDS:
        assert field in result.output


def test_status_json_says_null_when_the_engine_cannot_report(profile, runner, monkeypatch):
    """A proxy still running from before these fields existed cannot answer the question. Reporting
    `[]` would say "nothing has answered", which is a different claim from "I could not ask"."""
    monkeypatch.setattr(api, "_health", lambda: {"activeScenario": "default", "scenarios": []})
    _status_network(monkeypatch)
    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)
    assert payload["answers"] is None and payload["sequences"] is None


def test_status_json_says_empty_when_the_engine_reports_nothing_to_show(profile, runner, monkeypatch):
    """The other half of the distinction: a current engine sends one entry per rule, so an empty
    list is a real state and must not be confused with the case above."""
    monkeypatch.setattr(api, "_health", _health_payload)
    _status_network(monkeypatch)
    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)
    assert payload["answers"] == [] and payload["sequences"] == []


def test_status_json_reports_a_discovery_timeout_rather_than_printing_nothing(profile, runner, monkeypatch):
    """`--json` is what a script reads. An uncaught error from discovery printed no JSON at all,
    so the caller could not tell "not intercepting" from "the command fell over" — and the exit
    code is 1 for both. The reason goes where every other unproven PAC goes."""
    monkeypatch.setattr(api, "_health", _health_payload)
    monkeypatch.setattr(netproxy, "active_service", _discovery_times_out)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["service"] is None
    assert payload["intercepting"] is False
    assert "did not finish within 5s" in payload["pacError"]
