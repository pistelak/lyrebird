"""The scenario subcommands that reach the proxy: listing, moving, reloading, deleting.

A grouped name carries a `/`, which is exactly the character an HTTP path cannot hold — so these
pin what each command puts on the wire, not just what it prints. The proxy is a recording double:
what matters is the request, and the sentence the CLI shows when the API refuses.
"""

import json

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


def test_scenario_rm_sends_the_name_in_the_query_encoded(sent):
    """A `/` in a path segment matches no route at all, so interpolating the name would make the
    CLI report a scenario that is right there as one that does not exist."""
    result = _run(scenario.scenario_rm, "checkout/scratch")

    assert result.exit_code == 0
    path, method, _payload = sent["calls"][0]
    assert method == "DELETE"
    assert path == "/__mock__/scenarios?name=checkout%2Fscratch"


def test_scenario_mv_sends_both_names_in_the_body(sent):
    result = _run(scenario.scenario_mv, "checkout/scratch", "archive/scratch")

    assert result.exit_code == 0
    assert sent["calls"][0] == (
        "/__mock__/scenarios/move",
        "POST",
        {"name": "checkout/scratch", "to": "archive/scratch"},
    )
    assert "checkout/scratch → archive/scratch" in result.output


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


def test_scenario_list_prints_root_scenarios_first_then_each_folder(sent):
    sent["answers"][("/__mock__/scenarios", "GET")] = {
        "active": "checkout/orders-outage",
        "scenarios": [
            {"name": "default", "group": "", "overrideCount": 0, "verified": False},
            {"name": "checkout/orders-outage", "group": "checkout", "overrideCount": 2, "verified": True},
            {"name": "archive/old", "group": "archive", "overrideCount": 1, "verified": False},
        ],
    }

    result = _run(scenario.scenario_list)

    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines[0].strip().startswith("default")
    # The full name on every row: it is what `use`, `mv` and `rm` take, and a listing nobody can
    # copy a name out of is a listing that sends people to `ls`.
    assert any("archive/old" in line for line in lines)
    assert any("checkout/orders-outage" in line for line in lines)
    assert [line for line in lines if line.strip().rstrip("/").endswith("archive")], "a folder heading"
    active = next(line for line in lines if "checkout/orders-outage" in line and line.startswith(" *"))
    assert active


def test_scenario_list_json_echoes_the_payload(sent):
    payload = {"active": "default", "scenarios": [{"name": "default", "group": "", "overrideCount": 0}]}
    sent["answers"][("/__mock__/scenarios", "GET")] = payload

    result = _run(scenario.scenario_list, "--json")

    assert json.loads(result.output) == payload


def test_scenario_list_says_so_when_a_profile_has_none(sent):
    """Not a silent empty listing: a command that printed nothing reads as one that failed."""
    sent["answers"][("/__mock__/scenarios", "GET")] = {"active": "default", "scenarios": []}

    result = _run(scenario.scenario_list)

    assert "no scenarios" in result.output


def test_a_refused_move_reports_nothing_moved(sent):
    """`api._control` prints the API's `detail` and exits non-zero. A command named for an outcome
    must not print its success line after that — see the `_control` failure path in
    test_cli_offline.py."""
    sent["answers"][("/__mock__/scenarios/move", "POST")] = SystemExit(1)

    result = _run(scenario.scenario_mv, "checkout/a", "archive/a")

    assert result.exit_code == 1
    assert "moved" not in result.output


def test_a_refused_reload_reports_nothing_reloaded(sent):
    sent["answers"][("/__mock__/scenarios/reload", "POST")] = SystemExit(1)

    result = _run(scenario.scenario_reload)

    assert result.exit_code == 1
    assert "reloaded" not in result.output
