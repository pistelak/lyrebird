"""Sequence commands.

`sequence wait` is the command an agent leans on to prove a transition happened, so its failure
modes matter more than its happy path: a wait that hangs for its full timeout on something that
already happened sends the operator looking in the wrong place.
"""

import json
import time

import api
import cli
import netproxy
from cli_doubles import _BASE_SEQ, _answers_over, _health_payload, _polling


def _health_over(*states):
    """Sequence state under the poll. `None` for a state means the rule is gone from the scenario."""
    return _polling(states, lambda state: _health_payload(sequences=[] if state is None else [{**_BASE_SEQ, **state}]))


def _health_with(**state):
    return _health_over(state)


def test_reset_names_what_it_rewound(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_control", lambda *a, **k: {"scenario": "default", "reset": {"ovr_a": "abc123"}})
    result = runner.invoke(cli.cli, ["reset"])
    assert result.exit_code == 0
    assert "ovr_a" in result.output
    assert "abc123" in result.output, "the run id is what `assert-answered --run` is given"


def test_reset_json_hands_back_the_run_id_to_assert_with(profile, runner, monkeypatch):
    """The boundary has to be retainable by a script, not just readable by a person: an id that
    only exists inside a coloured line is an id no test harness can pass to the assertion."""
    monkeypatch.setattr(api, "_control", lambda *a, **k: {"scenario": "default", "reset": {"ovr_a": "abc123"}})
    result = runner.invoke(cli.cli, ["reset", "ovr_a", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"scenario": "default", "reset": {"ovr_a": "abc123"}}


def test_reset_json_reports_an_empty_reset_as_an_empty_map(profile, runner, monkeypatch):
    """`--json` decides how this is printed and nothing else — same exit, same meaning, and an
    empty map is a real answer rather than the absence of one."""
    monkeypatch.setattr(api, "_control", lambda *a, **k: {"scenario": "default", "reset": {}})
    result = runner.invoke(cli.cli, ["reset", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"scenario": "default", "reset": {}}


def test_reset_says_so_when_there_is_nothing_to_rewind(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_control", lambda *a, **k: {"scenario": "default", "reset": {}})
    result = runner.invoke(cli.cli, ["reset"])
    assert result.exit_code == 0
    assert "nothing to reset" in result.output


def test_sequence_wait_rejects_an_unknown_sequence(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", lambda: {"sequences": []})
    result = runner.invoke(cli.cli, ["sequence", "wait", "nope", "--step", "1"])
    assert result.exit_code == 1
    assert "no sequence" in result.output


def test_sequence_wait_rejects_a_step_that_does_not_exist(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", _health_with())
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "5"])
    assert result.exit_code == 1
    assert "out of range" in result.output


def test_sequence_wait_fails_at_once_when_the_step_already_passed(profile, runner, monkeypatch):
    """This has to come from live state, not the traffic buffer: /recent holds a bounded window, so
    the event may be long evicted while the fact that it happened is still true."""
    monkeypatch.setattr(api, "_health", _health_with(nextStep=None, exhausted=True))
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "30"])
    assert result.exit_code == 1
    assert "already past" in result.output
    assert "reset ovr_a" in result.output, "say how to fix it"


SERVED = {
    "sequenceId": "ovr_a",
    "runId": "r1",
    "selectedStep": 1,
    "stepCount": 2,
    "method": "GET",
    "path": "/api/items",
    "status": 200,
    "time": "2026-08-16T10:00:00+00:00",
}


def test_sequence_wait_succeeds_when_the_step_is_served(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", _health_over({}, {"serves": {"1": 1}}))
    monkeypatch.setattr(api, "_get_json", lambda _path, timeout=2: [SERVED])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 0
    assert "served step 1/2" in result.output
    assert "/api/items" in result.output, "the /recent detail when the entry is still there"


def test_sequence_wait_survives_the_serve_being_evicted_from_recent(profile, runner, monkeypatch):
    """The failure that motivated the serve counter: /recent is a 200-entry window, so under enough
    traffic the serve's entry is gone before the next poll — and a wait reading only the traffic
    buffer timed out on something that happened. The counter in live state cannot be evicted."""
    monkeypatch.setattr(api, "_health", _health_over({}, {"serves": {"1": 1}}))
    monkeypatch.setattr(api, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 0
    assert "served step 1/2" in result.output


def test_sequence_wait_succeeds_at_once_when_the_step_was_already_served_this_run(profile, runner, monkeypatch):
    """The serve counter lives in the runtime entry a reset drops, so everything in it happened
    after the last reset — it IS the postcondition, verified. The documented workflow is
    reset → trigger → wait, and when the action lands before the wait starts, refusing or timing
    out would report failure on a transition that completed."""
    monkeypatch.setattr(api, "_health", _health_with(advanceOn="match", serves={"1": 3}))
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "30"])
    assert result.exit_code == 0
    assert "served step 1/2 this run (3×, before the wait began)" in result.output


def test_sequence_wait_fails_fast_when_the_run_advances_past_the_step_without_serving_it(profile, runner, monkeypatch):
    """Two advance requests can march the cursor over the awaited step while an `advanceOn` rule
    serves nothing. That wait can never be satisfied, and burning the rest of the timeout would
    blame the sequence for not serving rather than the traffic for advancing it."""
    monkeypatch.setattr(api, "_health", _health_over({}, {"nextStep": None, "exhausted": True}))
    monkeypatch.setattr(api, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "30"])
    assert result.exit_code == 1
    assert "advanced past step 1 without serving it" in result.output


def test_sequence_wait_survives_a_transient_health_failure(profile, runner, monkeypatch):
    """One dropped health poll must read as "could not check right now", not "the rule is gone" —
    those are different claims, and the second one exits the wait."""
    responses = [_health_with()(), None, _health_with(serves={"1": 1})()]
    monkeypatch.setattr(api, "_health", lambda: responses.pop(0) if len(responses) > 1 else responses[0])
    monkeypatch.setattr(api, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 0
    assert "served step 1/2" in result.output


def test_sequence_wait_says_so_when_the_control_api_is_down(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", lambda: None)
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 1
    assert "cannot reach the control API" in result.output
    assert "no sequence" not in result.output, "'could not read it' must not claim 'nothing here'"


def test_sequence_wait_reports_losing_the_control_api_mid_wait(profile, runner, monkeypatch):
    responses = [_health_with()(), None]
    monkeypatch.setattr(api, "_health", lambda: responses.pop(0) if len(responses) > 1 else responses[0])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "0"])
    assert result.exit_code == 1
    assert "lost the control API" in result.output
    assert "did not serve" not in result.output, "whether it served is unknown, and the message must not decide"


def test_sequence_wait_ignores_an_event_from_an_earlier_run(profile, runner, monkeypatch):
    """A reset clears the serve counter with the runtime entry, so a leftover /recent event from
    the previous run must not satisfy the wait on its own."""
    monkeypatch.setattr(api, "_health", _health_with(runId="r2"))
    monkeypatch.setattr(
        api,
        "_get_json",
        lambda path, timeout=2: [
            {
                "sequenceId": "ovr_a",
                "runId": "r1",
                "selectedStep": 1,
                "stepCount": 2,
                "method": "GET",
                "path": "/api/items",
                "status": 200,
            }
        ],
    )
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "0"])
    assert result.exit_code == 1


def test_sequence_wait_fails_fast_when_the_run_changes_mid_wait(profile, runner, monkeypatch):
    """A reset mid-wait invalidates the baseline. Polling on regardless would burn the timeout and
    then blame the sequence for not serving."""
    monkeypatch.setattr(api, "_health", _health_over({}, {"runId": "r2"}))
    monkeypatch.setattr(api, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 1
    assert "was reset" in result.output


def test_sequence_wait_fails_fast_when_the_rule_vanishes_mid_wait(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", _health_over({}, None))
    monkeypatch.setattr(api, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 1
    assert "was removed" in result.output


def test_status_json_carries_answer_counts(profile, runner, monkeypatch):
    """AGENTS.md documents `answers` in `status --json`; it was in /health and never forwarded."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 2}))
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    result = runner.invoke(cli.cli, ["status", "--json"])
    assert json.loads(result.output)["answers"] == [{"id": "ovr_a", "active": True, "count": 2, "runId": "run1"}]


def test_status_json_carries_sequences(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", _health_with())
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    result = runner.invoke(cli.cli, ["status", "--json"])
    assert result.exit_code == 0
    assert '"sequences"' in result.output and "ovr_a" in result.output


# MARK: - Proving a mock was in play
#
# A negative UI assertion passes whether or not the mock applied, so the suite needs a command that
# fails. Every test below is a way that command could have reported success it had not earned.


def test_assert_answered_succeeds_when_the_rule_answered(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", _answers_over({"count": 3}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 0
    assert "3 request(s)" in result.output


def test_assert_answered_rejects_an_unknown_id_rather_than_reporting_zero(profile, runner, monkeypatch):
    """A typo and a rule that never fired are different bugs with different fixes, and reading one
    as the other is how you spend an afternoon on a matcher that was always correct."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 1}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_typo"])
    assert result.exit_code == 1
    assert "no rule 'ovr_typo'" in result.output
    assert "has not answered" not in result.output


def test_assert_answered_says_so_when_the_control_api_is_unreachable(profile, runner, monkeypatch):
    """ "I could not ask" is not "it answered nothing"."""
    monkeypatch.setattr(api, "_health", lambda: None)
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "cannot reach the control API" in result.output


def test_assert_answered_refuses_a_proxy_that_cannot_report_counts(profile, runner, monkeypatch):
    """An engine too old to report counts must not be read as a rule that answered nothing — that
    turns a restart into a debugging scenario."""
    monkeypatch.setattr(api, "_health", lambda: {"activeScenario": "default", "sequences": []})
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "does not report answer counts" in result.output


def test_assert_answered_fails_at_once_for_an_inactive_rule(profile, runner, monkeypatch):
    """Matching skips a disabled rule entirely, so waiting cannot help."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 0, "active": False}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--timeout", "30"])
    assert result.exit_code == 1
    assert "never answer" in result.output


def test_assert_answered_lists_the_paths_that_did_arrive(profile, runner, monkeypatch):
    """A count cannot tell "the app went somewhere else" from "the path pattern is wrong"; the
    paths can."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 0}))
    monkeypatch.setattr(
        api,
        "_get_json",
        lambda *a, **k: [
            {"method": "GET", "path": "/api/v2/items"},
            {"method": "GET", "path": "/api/v2/items"},
        ],
    )
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "/api/v2/items" in result.output
    assert "explain-match" in result.output


def test_assert_answered_names_an_empty_proxy_as_a_routing_problem(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", _answers_over({"count": 0}))
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: [])
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "relaunch the app" in result.output


def test_assert_answered_succeeds_on_an_answer_that_lands_mid_wait(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", _answers_over({"count": 0}, {"count": 0}, {"count": 2}))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--timeout", "30"])
    assert result.exit_code == 0
    assert "2 request(s)" in result.output


def test_assert_answered_rejects_a_negative_timeout(profile, runner):
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--timeout", "-1"])
    assert result.exit_code == 1
    assert "must not be negative" in result.output


# MARK: - Binding the assertion to the run the caller started
#
# A count answers "has this rule answered in *some* run". The run a test set up ends whenever
# anything resets the rule, replaces it under the same id, or switches scenario — and the rule id
# looks identical on the other side of that. Every test here is a way the command could report a
# stranger's evidence as the test's own, which is worse than reporting none: it is a green pass.


def test_assert_answered_refuses_a_count_from_another_run(profile, runner, monkeypatch):
    """The defect this option exists for: something reset the rule between the action and the
    assertion, the app fetched again, and the count is now three — for a run the test never set
    up. Reported as success it is a pass nobody earned."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 3, "runId": "run2"}))
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: [])
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 3, "the assertion was not made, which is not 'it answered nothing'"
    assert "run2" in result.output and "run1" in result.output
    assert "answered 3" not in result.output


def test_assert_answered_accepts_a_count_from_the_run_it_was_given(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_health", _answers_over({"count": 2, "runId": "run1"}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 0
    assert "2 request(s)" in result.output and "run1" in result.output


def test_assert_answered_refuses_a_run_that_ended_while_it_waited(profile, runner, monkeypatch):
    """The same substitution, arriving mid-poll. Continuing to watch would eventually see the new
    run answer and return success on evidence produced after the boundary was destroyed."""
    monkeypatch.setattr(
        api,
        "_health",
        _answers_over({"count": 0, "runId": "run1"}, {"count": 0, "runId": "run2"}, {"count": 5, "runId": "run2"}),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: [])
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1", "--timeout", "30"])
    assert result.exit_code == 3
    assert "not run run1" in result.output
    assert "5 request(s)" not in result.output


def test_assert_answered_tells_a_missing_run_from_a_run_with_no_answers(profile, runner, monkeypatch):
    """`runId: null` says the rule has no run at all — its state was dropped by a scenario switch or
    a replacement. That is not "the run you named happened and nothing answered", and the two need
    different fixes, so they must not share an exit code."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 0, "runId": None}))
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: [])
    bound = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert bound.exit_code == 3
    assert "no run at all" in bound.output

    monkeypatch.setattr(api, "_health", _answers_over({"count": 0, "runId": None}))
    plain = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert plain.exit_code == 1, "the weaker assertion still just reports no answers"
    assert "has not answered" in plain.output


def test_assert_answered_refuses_a_proxy_that_cannot_report_run_identity(profile, runner, monkeypatch):
    """An engine old enough to count answers but not to say which run they belong to. Ignoring
    --run there would silently downgrade the assertion to the one it was called to avoid."""
    monkeypatch.setattr(api, "_health", lambda: _health_payload(answers=[{"id": "ovr_a", "active": True, "count": 4}]))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 3
    assert "does not report which run" in result.output
    assert "4 request(s)" not in result.output


def test_assert_answered_without_a_run_reads_whatever_run_is_current(profile, runner, monkeypatch):
    """The documented weaker semantics, pinned so they stay deliberate: no --run means the command
    asks about the run the proxy is in when it looks, whichever run that turns out to be."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 3, "runId": "a-run-nobody-held"}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 0
    assert "3 request(s)" in result.output


def test_assert_answered_rejects_an_empty_run(profile, runner):
    """An empty --run would compare equal to nothing and unequal to everything by accident; a run
    that cannot be named is refused at the boundary instead."""
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", ""])
    assert result.exit_code == 1
    assert "must name a run id" in result.output


def test_assert_answered_refuses_a_rule_that_vanishes_while_it_waits(profile, runner, monkeypatch):
    """A scenario switched mid-wait to one that does not carry this id destroys the boundary rather
    than answering the question about it. Reported as 1 it would read as "the mock did not apply",
    which is a claim this command was in no position to make."""
    monkeypatch.setattr(api, "_health", _answers_over({"count": 0, "runId": "run1"}, None))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1", "--timeout", "30"])
    assert result.exit_code == 3
    assert "no rule 'ovr_a'" in result.output
    assert "cannot be checked" in result.output


def test_assert_answered_without_a_run_still_reports_a_missing_rule_as_one(profile, runner, monkeypatch):
    """The weaker assertion keeps every exit code it had: only --run can produce 3."""
    monkeypatch.setattr(api, "_health", _answers_over(None))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "no rule 'ovr_a'" in result.output


def test_assert_answered_cannot_prove_a_run_against_a_proxy_that_counts_but_cannot_name_runs(
    profile, runner, monkeypatch
):
    """Version skew in the other field. An engine that cannot count at all certainly cannot say
    which run its counts are in, so under --run both refusals have to arrive as the same code — a
    harness branching on 3 must not have to learn which flavour of skew it hit."""
    monkeypatch.setattr(api, "_health", lambda: {"activeScenario": "default", "sequences": []})
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 3
    assert "does not report answer counts" in result.output


def test_assert_answered_with_a_run_reports_an_unreachable_proxy_as_unproven(profile, runner, monkeypatch):
    """ "I could not ask" is not "the rule answered nothing in your run" — and under --run there is
    a code that says so, so the one meaning left for 1 is a real, made assertion that failed."""
    monkeypatch.setattr(api, "_health", lambda: None)
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 3
    assert "cannot reach the control API" in result.output
