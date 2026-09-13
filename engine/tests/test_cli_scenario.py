"""`scenario reload`, the one command that makes an edited file reach the proxy.

The proxy is a recording double: what matters is the request the command puts on the wire, and the
sentence the CLI shows when the API refuses.
"""

import pytest
from click.testing import CliRunner

import api
import scenario


@pytest.fixture
def sent(monkeypatch):
    """Records every control call, answering with whatever the test queued."""
    calls = []
    answers = {}

    def control(path, method="GET", payload=None, timeout=3.0):
        calls.append((path, method, payload))
        answer = answers.get((path.split("?")[0], method), {})
        # BaseException, not Exception: `api._control` reports a refusal by raising `SystemExit`,
        # which is not an Exception — a double that tested for one would answer normally and let a
        # command report success on the very path this file exists to check.
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(api, "_control", control)
    return {"calls": calls, "answers": answers}


def _run(command, *args):
    return CliRunner().invoke(command, list(args))


def test_scenario_reload_without_use_sends_no_selection(sent):
    """An absent `use` and an empty one are different requests: one keeps the active scenario, the
    other would be a client naming a scenario and sending nothing."""
    sent["answers"][("/__mock__/scenarios/reload", "POST")] = {"reloaded": 3, "active": "default"}

    result = _run(scenario.scenario_reload)

    assert sent["calls"][0] == ("/__mock__/scenarios/reload", "POST", {})
    assert "reloaded 3 scenario(s), active: default" in result.output
    assert "run evidence was reset" in result.output, "counts from before the reload belong to nothing"


def test_scenario_reload_passes_use_through(sent):
    sent["answers"][("/__mock__/scenarios/reload", "POST")] = {"reloaded": 1, "active": "checkout/x"}

    _run(scenario.scenario_reload, "--use", "checkout/x")

    assert sent["calls"][0][2] == {"use": "checkout/x"}


def test_a_refused_reload_reports_nothing_reloaded(sent):
    """`api._control` prints the API's `detail` and exits non-zero. A command named for an outcome
    must not print its success line after that — see the `_control` failure path in
    test_cli_offline.py."""
    sent["answers"][("/__mock__/scenarios/reload", "POST")] = SystemExit(1)

    result = _run(scenario.scenario_reload)

    assert result.exit_code == 1
    assert "reloaded" not in result.output
