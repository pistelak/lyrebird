"""The whole interception path, end to end: a real app in a real simulator, the Mac's PAC, TLS
terminated by a CA trusted in that simulator, and an override answering locally.

Everything else in `engine/tests/` replaces something — mitmproxy flows are constructed, the
network is a fake, `simctl` is never called. Those tests cannot see a regression in CA trust,
relaunch ordering, PAC routing or teardown, because each of those lives in the part they stub.
This module is where those are checked, which is why it needs a machine with Xcode and is not run
in CI (see CONTRIBUTING.md, "Acceptance checks").

It is **one** test, not five, because what it checks is one procedure: a scenario is selected, an
app meets it, the scenario moves on, the run is torn down. Every phase depends on the state the
last one left — one proxy, one app, one container of evidence — so five test functions would only
have looked independent: picking one of them off a clean machine would fail for want of a proxy,
and a failure in the first would cascade into five more. Each phase prints its name and fails with
its own message, so the report still says which one gave way.

Both sides of every claim are read — the proxy's own evidence (`assert-answered --run`,
`sequence wait`, `status --json`) and the app's record of what it launched, received and put on
screen — because either alone can be true while the path is broken:

* the proxy answering proves a rule fired, not that the app ever saw it;
* the app showing the right body proves it got *an* answer, not that Lyrebird produced it.
"""

from __future__ import annotations

import time

import pytest

import ownership

pytestmark = pytest.mark.acceptance

DECOY = "LYREBIRD-FIXTURE-DECOY"
REPLACED = "LYREBIRD-FIXTURE-REPLACED"
STEP_ONE = "LYREBIRD-FIXTURE-STEP-ONE"
STEP_TWO = "LYREBIRD-FIXTURE-STEP-TWO"

# Long enough for a launch that was never going to happen to have happened. The app records a
# launch before it does any networking, so this waits for a launch, not for a request.
UNLAUNCHED_GRACE = 15.0


def _body(record: dict) -> str:
    return str(record.get("body") or "")


def test_the_simulator_interception_path(harness):
    """One app, one proxy, five phases, in order."""
    scenarios_load_whole(harness)
    up_refuses_an_unknown_scenario_and_puts_the_network_back(harness)
    launch_traffic_is_answered_by_the_scenario_up_selected(harness)
    a_sequence_moves_to_its_second_step_on_the_next_launch(harness)
    down_restores_the_proxy_settings_that_were_there_before(harness)


def scenarios_load_whole(harness):
    """`validate` first, with no proxy and no network change: a rule silently dropped at load is
    the one failure that makes every phase below fail for the wrong reason."""
    harness.phase("the acceptance scenarios load whole")
    for name in ("fixture-decoy", "fixture-replaced", "fixture-sequence"):
        harness.run("validate", name)


def launch_traffic_is_answered_by_the_scenario_up_selected(harness):
    """A locally replaced HTTPS response, reaching the app's screen and not merely its socket.

    The decoy is activated first and answers the same request with a different marker and a
    different status, so the launch that follows `use fixture-replaced && relaunch` getting the
    replaced body cannot be luck: it is the scenario that was selected, not the one that was active
    a moment before. (That `up --use` *runs* the selection before the launch rather than merely
    fast enough is pinned deterministically in engine/tests/test_cli_launch.py; what this adds is
    that the app really is answered by it. There is one session per user, so switching scenario on
    a running session is `use` then `relaunch`, not a second `up`.)
    """
    harness.phase("launch traffic is answered by the scenario `up` selected")
    harness.up("--use", "fixture-decoy")

    decoy = harness.wait_for_results(1)[-1]
    assert decoy.get("error") is None, f"the launch request never reached the proxy: {decoy}"
    assert decoy["status"] == 200, decoy
    assert DECOY in _body(decoy), decoy
    # The channel the "nothing was launched" phase read, proved to record something when a launch
    # does happen — an assertion about an empty file is worth nothing on its own.
    assert len(harness.launches()) >= 1, "the app recorded no launch at all"

    before, launches = len(harness.results()), len(harness.launches())
    harness.run("use", "fixture-replaced")
    harness.relaunch()

    replaced = harness.wait_for_results(before + 1)[-1]
    assert replaced.get("error") is None, replaced
    # 503 is not a status this request could have got any other way: no upstream serves this host.
    assert replaced["status"] == 503, replaced
    assert REPLACED in _body(replaced), replaced
    assert DECOY not in _body(replaced), (
        "the app was answered by the scenario that was active before `--use` — the scenario `up` "
        "selected is not the one the launch met"
    )
    assert len(harness.launches()) == launches + 1, "relaunch did not relaunch the app"

    shown = harness.wait_for_displayed(REPLACED)
    assert str(replaced["status"]) in shown, f"the body reached the label but the status did not: {shown!r}"

    state = harness.status()
    assert state["intercepting"] is True, state
    assert state["activeScenario"] == "fixture-replaced", state
    # `up --simulator` bound the CA and the relaunch to the device this run means, and recorded it.
    # Everything above is evidence read out of *that* simulator, so a run that had silently acted
    # on another booted device would be reading one app and asserting about another.
    assert (state.get("simulator") or {}).get("udid") == harness.udid, state

    # The proxy's own side of it, bound to the run `up --use` started: not "has this rule ever
    # answered", but "did it answer inside the boundary this check drew", which nothing the decoy
    # did can satisfy.
    run_id = next(s["runId"] for s in state["answers"] if s["id"] == "ovr_fixture_replaced")
    harness.run("assert-answered", "ovr_fixture_replaced", "--run", run_id)


def a_sequence_moves_to_its_second_step_on_the_next_launch(harness):
    """Step 1 to step 2 across two launches, checked from both ends.

    `sequence wait` is what proves the transition rather than `assert-answered`, which proves the
    rule answered but not which step it served, and so cannot express "step 1, then step 2".
    """
    harness.phase("a sequence moves to its second step on the next launch")
    before = len(harness.results())
    harness.run("use", "fixture-sequence")  # activation rewinds the sequence
    harness.relaunch()

    step_one = harness.wait_for_results(before + 1)[-1]
    assert step_one.get("error") is None, step_one
    assert STEP_ONE in _body(step_one), step_one

    served_once = len(harness.results())
    harness.relaunch()
    step_two = harness.wait_for_results(served_once + 1)[-1]
    assert step_two.get("error") is None, step_two
    assert step_two["status"] == 200, step_two
    assert STEP_TWO in _body(step_two), step_two
    harness.wait_for_displayed(STEP_TWO)

    # Already served by the time this runs, which `sequence wait` answers immediately rather than
    # burning its timeout on.
    served = harness.run("sequence", "wait", "ovr_fixture_sequence", "--step", "2", "--timeout", "30")
    assert "step 2" in served.stdout, served.stdout


def up_refuses_an_unknown_scenario_and_puts_the_network_back(harness):
    """The failure path that must not put the app in front of the wrong scenario — and, on a real
    Mac, the unwind: `up --use nosuch` cannot achieve what it was asked for, so it exits non-zero,
    relaunches nothing, and puts the proxy settings back, stops its proxy and releases the journal
    before it exits. The shape this codebase gets wrong most often is the one where it would have
    exited 0 having launched the app against whatever was active before; the next most common is
    exit 1 with the Mac still routed at a proxy nobody will stop.

    Runs before any session exists: there is one per user, so on a running session a second `up`
    is refused on the journal before it ever looks at the scenario name. (That the launch channel
    records launches at all is proved by the phase after this one.)
    """
    harness.phase("`up --use <unknown>` refuses, launches nothing and puts the network back")
    launches, results = len(harness.launches()), len(harness.results())
    refused = harness.up("--use", "no-such-scenario", expect=1)
    assert "no-such-scenario" in refused.stdout, refused.stdout
    assert "NOT relaunched" in refused.stdout, refused.stdout

    time.sleep(UNLAUNCHED_GRACE)
    # Launches, not results: the app records a launch before it does any networking, so this
    # cannot be satisfied by an app that was started and then never got an answer.
    started = harness.launches()
    assert len(started) == launches, (
        f"the app was launched anyway: {len(started) - launches} extra launch(es), last {started[-1]}"
    )
    assert len(harness.results()) == results

    # Unwound: the settings are back as macOS reports them, the journal is gone, nothing answers.
    assert harness.restored(), (
        f"before: {harness.describe(harness.baseline)} · after: {harness.describe(harness.pac())}"
    )
    assert isinstance(harness.journal(), ownership.Absent), harness.journal()
    state = harness.status()
    assert state["proxyUp"] is False, state
    assert state["intercepting"] is False, state


def down_restores_the_proxy_settings_that_were_there_before(harness):
    """Normal teardown. The comparison is against macOS, not against Lyrebird's own account of
    what it did."""
    harness.phase("`down` restores the proxy settings that were there before")
    assert harness.pac_is_ours(), f"the run was not intercepting before `down`: {harness.describe(harness.pac())}"

    harness.run("down")

    assert harness.restored(), (
        f"before: {harness.describe(harness.baseline)} · after: {harness.describe(harness.pac())}"
    )
    state = harness.status()
    assert state["proxyUp"] is False, state
    assert state["intercepting"] is False, state
