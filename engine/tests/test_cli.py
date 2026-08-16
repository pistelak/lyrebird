"""Teardown behaviour.

`down` is the command that has to work when everything else has gone wrong — the proxy crashed,
the runtime file is unreadable, the machine was rebooted mid-session. If it silently does nothing,
the user is left with a PAC pointing at a dead port and no indication why.
"""


import os

import pytest
from click.testing import CliRunner

import cli
import config
import netproxy


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fake_network(monkeypatch):
    """A Wi-Fi service whose PAC is currently ours and enabled."""
    state = {"service": "Wi-Fi", "restored": None, "terminated": []}

    monkeypatch.setattr(netproxy, "active_service", lambda: state["service"])
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))

    def restore(service, url, enabled):
        state["restored"] = (service, url, enabled)

    monkeypatch.setattr(netproxy, "restore_pac", restore)
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: state["terminated"].append((pid, marker)))
    return state


def health_until_terminated(monkeypatch, state, pid=4242):
    """Health answers until the proxy is terminated, then stops — as a real proxy does."""
    monkeypatch.setattr(cli, "_health",
                        lambda: None if state["terminated"] else {"pid": pid})


def test_down_recovers_when_the_runtime_file_is_unreadable(profile, runner, fake_network, monkeypatch):
    """Regression: a corrupt runtime file made `down` find nothing to do and report "stopped"
    while the proxy was still running and the PAC still pointing at it. Verified against a real
    proxy before this test existed."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    health_until_terminated(monkeypatch, fake_network)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert fake_network["restored"] is not None, "the PAC must be restored even with no runtime state"
    assert (4242, "addon.py") in fake_network["terminated"], "the pid must come from health"


def test_down_uses_the_runtime_file_when_it_is_readable(profile, runner, fake_network, monkeypatch):
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "watchdogPid": 98, "service": "Wi-Fi",
                          "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": True}})
    health_until_terminated(monkeypatch, fake_network, pid=99)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert fake_network["restored"] == ("Wi-Fi", "http://proxy.example.com/corp.pac", True)
    assert (98, "_watchdog") in fake_network["terminated"]
    assert not config.runtime_file().exists()


def test_down_says_so_when_there_is_nothing_to_stop(profile, runner, monkeypatch):
    """Better than claiming success: the user needs to know their PAC was not touched."""
    monkeypatch.setattr(cli, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", lambda: None)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert "nothing to stop" in result.output


def test_down_leaves_a_foreign_pac_alone(profile, runner, monkeypatch):
    """A PAC the user set by hand mid-session must survive teardown."""
    touched = []
    stopped = {"terminated": []}
    monkeypatch.setattr(cli, "_health", lambda: None if stopped["terminated"] else {"pid": 1})
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus("http://proxy.example.com/corp.pac", True, False))
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: touched.append(a))
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: stopped["terminated"].append(pid))

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert touched == [], "a PAC that is not ours must never be rewritten"
    assert "not ours" in result.output




def test_down_reports_a_proxy_that_did_not_stop(profile, runner, fake_network, monkeypatch):
    """SIGTERM is a request. Printing "stopped" over a proxy that is still serving is the lie this
    check exists to prevent."""
    monkeypatch.setattr(cli, "_health", lambda: {"pid": 4242})   # never dies
    monkeypatch.setattr(cli, "_DOWN_WAIT_SECONDS", 0.3)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "still responding" in result.output


# `status` is a query, but its exit code is a claim about the machine, and an agent acts on it.
# The pair below exists because the two output formats once disagreed: --json exited 1 when
# nothing was being intercepted while the human form exited 0, so `lyrebird status && …` ran
# happily against a proxy that was mocking nothing.

@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_fails_when_not_intercepting_in_either_format(profile, runner, monkeypatch, args):
    monkeypatch.setattr(cli, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))

    assert runner.invoke(cli.cli, ["status", *args]).exit_code == 1


@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_succeeds_only_when_up_and_intercepting(profile, runner, monkeypatch, args):
    monkeypatch.setattr(cli, "_health",
                        lambda: {"pid": 1, "sessions": ["default"], "activeSession": "default",
                                 "overrideCount": 0, "simBundleId": None, "proxyPort": 8080})
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))

    assert runner.invoke(cli.cli, ["status", *args]).exit_code == 0


def test_status_fails_when_the_proxy_is_up_but_the_pac_is_off(profile, runner, monkeypatch):
    """Up is not the same as intercepting, and this is the gap the exit code exists to report."""
    monkeypatch.setattr(cli, "_health",
                        lambda: {"pid": 1, "sessions": [], "activeSession": None,
                                 "overrideCount": 0, "simBundleId": None})
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))

    assert runner.invoke(cli.cli, ["status"]).exit_code == 1


def test_up_starts_the_log_on_a_new_inode(profile, tmp_path):
    """A log left at 0644 by an older version cannot be made private by chmod alone.

    Anyone already holding it keeps reading, because a descriptor carries its own access. Only a
    new inode cuts them off — so this asserts the identity of the file changed, not just its mode.
    """
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.LOG_FILE.write_text("from an older run\n")
    os.chmod(config.LOG_FILE, 0o644)
    stale_inode = config.LOG_FILE.stat().st_ino

    with open(config.LOG_FILE) as reader:
        reader.read()
        cli._start_fresh_log()
        with open(config.LOG_FILE, "a", encoding="utf-8") as sink:
            sink.write("a host and a path\n")
        overheard = reader.read()

    assert config.LOG_FILE.stat().st_ino != stale_inode, "the lax inode was truncated, not replaced"
    assert config.LOG_FILE.stat().st_mode & 0o777 == 0o600
    assert overheard == "", f"a reader of the old log still saw traffic: {overheard!r}"


# MARK: - Sequence commands
#
# `sequence wait` is the command an agent leans on to prove a transition happened, so its failure
# modes matter more than its happy path: a wait that hangs for its full timeout on something that
# already happened sends the operator looking in the wrong place.

_BASE_SEQ = {"id": "ovr_a", "runId": "r1", "advanceOn": "self", "nextStep": 1, "stepCount": 2,
             "exhausted": False, "hasOverrun": False, "serves": {}}


def _health_over(*states):
    """Live state that moves under the poll: each call serves the next state, the last repeats.

    `None` for a state means the rule is gone from the session."""
    queue = list(states)

    def payload():
        state = queue.pop(0) if len(queue) > 1 else queue[0]
        sequences = [] if state is None else [{**_BASE_SEQ, **state}]
        return {"pid": 1, "sessions": ["default"], "activeSession": "default",
                "overrideCount": 1, "simBundleId": None, "proxyPort": 8080,
                "sequences": sequences}
    return payload


def _health_with(**state):
    return _health_over(state)


def test_sequence_reset_names_what_it_rewound(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_control",
                        lambda *a, **k: {"session": "default", "reset": {"ovr_a": "abc123"}})
    result = runner.invoke(cli.cli, ["sequence", "reset"])
    assert result.exit_code == 0
    assert "ovr_a" in result.output


def test_sequence_reset_says_so_when_there_is_nothing_to_rewind(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_control", lambda *a, **k: {"session": "default", "reset": {}})
    result = runner.invoke(cli.cli, ["sequence", "reset"])
    assert result.exit_code == 0
    assert "nothing to reset" in result.output


def test_sequence_wait_rejects_an_unknown_sequence(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", lambda: {"sequences": []})
    result = runner.invoke(cli.cli, ["sequence", "wait", "nope", "--step", "1"])
    assert result.exit_code == 1
    assert "no sequence" in result.output


def test_sequence_wait_rejects_a_step_that_does_not_exist(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _health_with())
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "5"])
    assert result.exit_code == 1
    assert "out of range" in result.output


def test_sequence_wait_fails_at_once_when_the_step_already_passed(profile, runner, monkeypatch):
    """This has to come from live state, not the traffic buffer: /recent holds a bounded window, so
    the event may be long evicted while the fact that it happened is still true."""
    monkeypatch.setattr(cli, "_health", _health_with(nextStep=None, exhausted=True))
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "30"])
    assert result.exit_code == 1
    assert "already past" in result.output
    assert "sequence reset ovr_a" in result.output, "say how to fix it"


SERVED = {"sequenceId": "ovr_a", "runId": "r1", "selectedStep": 1, "stepCount": 2,
          "method": "GET", "path": "/api/items", "status": 200,
          "time": "2026-08-16T10:00:00+00:00"}


def test_sequence_wait_succeeds_when_the_step_is_served(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _health_over({}, {"serves": {"1": 1}}))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [SERVED])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 0
    assert "served step 1/2" in result.output
    assert "/api/items" in result.output, "the /recent detail when the entry is still there"


def test_sequence_wait_survives_the_serve_being_evicted_from_recent(profile, runner, monkeypatch):
    """The failure that motivated the serve counter: /recent is a 200-entry window, so under enough
    traffic the serve's entry is gone before the next poll — and a wait reading only the traffic
    buffer timed out on something that happened. The counter in live state cannot be evicted."""
    monkeypatch.setattr(cli, "_health", _health_over({}, {"serves": {"1": 1}}))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 0
    assert "served step 1/2" in result.output


def test_sequence_wait_succeeds_at_once_when_the_step_was_already_served_this_run(profile, runner, monkeypatch):
    """The serve counter lives in the runtime entry a reset drops, so everything in it happened
    after the last reset — it IS the postcondition, verified. The documented workflow is
    reset → trigger → wait, and when the action lands before the wait starts, refusing or timing
    out would report failure on a transition that completed."""
    monkeypatch.setattr(cli, "_health", _health_with(advanceOn="match", serves={"1": 3}))
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "30"])
    assert result.exit_code == 0
    assert "served step 1/2 this run (3×, before the wait began)" in result.output


def test_sequence_wait_fails_fast_when_the_run_advances_past_the_step_without_serving_it(profile, runner, monkeypatch):
    """Two advance requests can march the cursor over the awaited step while an `advanceOn` rule
    serves nothing. That wait can never be satisfied, and burning the rest of the timeout would
    blame the sequence for not serving rather than the traffic for advancing it."""
    monkeypatch.setattr(cli, "_health", _health_over({}, {"nextStep": None, "exhausted": True}))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "30"])
    assert result.exit_code == 1
    assert "advanced past step 1 without serving it" in result.output


def test_sequence_wait_survives_a_transient_health_failure(profile, runner, monkeypatch):
    """One dropped health poll must read as "could not check right now", not "the rule is gone" —
    those are different claims, and the second one exits the wait."""
    responses = [_health_with()(), None, _health_with(serves={"1": 1})()]
    monkeypatch.setattr(cli, "_health", lambda: responses.pop(0) if len(responses) > 1 else responses[0])
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 0
    assert "served step 1/2" in result.output


def test_sequence_wait_says_so_when_the_control_api_is_down(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", lambda: None)
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 1
    assert "cannot reach the control API" in result.output
    assert "no sequence" not in result.output, "'could not read it' must not claim 'nothing here'"


def test_sequence_wait_reports_losing_the_control_api_mid_wait(profile, runner, monkeypatch):
    responses = [_health_with()(), None]
    monkeypatch.setattr(cli, "_health", lambda: responses.pop(0) if len(responses) > 1 else responses[0])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "0"])
    assert result.exit_code == 1
    assert "lost the control API" in result.output
    assert "did not serve" not in result.output, "whether it served is unknown, and the message must not decide"


def test_sequence_wait_ignores_an_event_from_an_earlier_run(profile, runner, monkeypatch):
    """A reset clears the serve counter with the runtime entry, so a leftover /recent event from
    the previous run must not satisfy the wait on its own."""
    monkeypatch.setattr(cli, "_health", _health_with(runId="r2"))
    monkeypatch.setattr(cli, "_get_json", lambda path, timeout=2: [
        {"sequenceId": "ovr_a", "runId": "r1", "selectedStep": 1, "stepCount": 2,
         "method": "GET", "path": "/api/items", "status": 200}])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "0"])
    assert result.exit_code == 1


def test_sequence_wait_fails_fast_when_the_run_changes_mid_wait(profile, runner, monkeypatch):
    """A reset mid-wait invalidates the baseline. Polling on regardless would burn the timeout and
    then blame the sequence for not serving."""
    monkeypatch.setattr(cli, "_health", _health_over({}, {"runId": "r2"}))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 1
    assert "was reset" in result.output


def test_sequence_wait_fails_fast_when_the_rule_vanishes_mid_wait(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _health_over({}, None))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 1
    assert "was removed" in result.output


def test_status_json_carries_sequences(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _health_with())
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    result = runner.invoke(cli.cli, ["status", "--json"])
    assert result.exit_code == 0
    assert '"sequences"' in result.output and "ovr_a" in result.output
