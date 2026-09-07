"""The whole interception path, end to end: a real app in a real simulator, the Mac's PAC, TLS
terminated by a CA trusted in that simulator, and an override answering locally.

Everything else in `engine/tests/` replaces something — mitmproxy flows are constructed, the
network is a fake, `simctl` is never called. Those tests cannot see a regression in CA trust,
relaunch ordering, PAC routing or teardown, because each of those lives in the part they stub.
This module is where those are checked, which is why it needs a machine with Xcode and is not run
in CI (see CONTRIBUTING.md, "Acceptance checks").

It is **one** test, not six, because what it checks is one procedure: a scenario is selected, an
app meets it, the scenario moves on, the run is torn down. Every phase depends on the state the
last one left — one proxy, one app, one container of evidence — so six test functions would only
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

import json
import os
import signal
import time

import pytest

pytestmark = pytest.mark.acceptance

DECOY = "LYREBIRD-FIXTURE-DECOY"
REPLACED = "LYREBIRD-FIXTURE-REPLACED"
STEP_ONE = "LYREBIRD-FIXTURE-STEP-ONE"
STEP_TWO = "LYREBIRD-FIXTURE-STEP-TWO"

# Long enough for a launch that was never going to happen to have happened. The app records a
# launch before it does any networking, so this waits for a launch, not for a request.
UNLAUNCHED_GRACE = 15.0
# The watchdog polls health every two seconds and retries `networksetup` on failure.
WATCHDOG_WINDOW = 60.0


def _body(record: dict) -> str:
    return str(record.get("body") or "")


def test_the_simulator_interception_path(harness):
    """One app, one proxy, six phases, in order."""
    scenarios_load_whole(harness)
    launch_traffic_is_answered_by_the_scenario_up_selected(harness)
    a_sequence_moves_to_its_second_step_on_the_next_launch(harness)
    up_refuses_an_unknown_scenario_and_launches_nothing(harness)
    down_restores_the_proxy_settings_that_were_there_before(harness)
    the_watchdog_restores_the_settings_when_the_proxy_is_killed(harness)


def scenarios_load_whole(harness):
    """`validate` first, with no proxy and no network change: a rule silently dropped at load is
    the one failure that makes every phase below fail for the wrong reason."""
    harness.phase("the acceptance scenarios load whole")
    for name in ("fixture-decoy", "fixture-replaced", "fixture-sequence"):
        harness.run("validate", name)


def launch_traffic_is_answered_by_the_scenario_up_selected(harness):
    """A locally replaced HTTPS response, reaching the app's screen and not merely its socket.

    The decoy is activated first and answers the same request with a different marker and a
    different status, so the launch that follows `up --use fixture-replaced` getting the replaced
    body cannot be luck: it is the session `up` selected, not the one that was active a moment
    before. (That `up` *runs* the selection before the launch rather than merely fast enough is
    pinned deterministically in engine/tests/test_cli.py; what this adds is that the app really is
    answered by it.)
    """
    harness.phase("launch traffic is answered by the scenario `up` selected")
    harness.up("--use", "fixture-decoy")

    decoy = harness.wait_for_results(1)[-1]
    assert decoy.get("error") is None, f"the launch request never reached the proxy: {decoy}"
    assert decoy["status"] == 200, decoy
    assert DECOY in _body(decoy), decoy
    # The channel the "nothing was launched" phase reads, proved to record something when a launch
    # does happen — an assertion about an empty file is worth nothing on its own.
    assert len(harness.launches()) >= 1, "the app recorded no launch at all"

    before, launches = len(harness.results()), len(harness.launches())
    harness.up("--use", "fixture-replaced")

    replaced = harness.wait_for_results(before + 1)[-1]
    assert replaced.get("error") is None, replaced
    # 503 is not a status this request could have got any other way: no upstream serves this host.
    assert replaced["status"] == 503, replaced
    assert REPLACED in _body(replaced), replaced
    assert DECOY not in _body(replaced), (
        "the app was answered by the session that was active before `--use` — the scenario `up` "
        "selected is not the one the launch met"
    )
    assert len(harness.launches()) == launches + 1, "up did not relaunch the app"

    shown = harness.wait_for_displayed(REPLACED)
    assert str(replaced["status"]) in shown, f"the body reached the label but the status did not: {shown!r}"

    state = harness.status()
    assert state["intercepting"] is True, state
    assert state["activeSession"] == "fixture-replaced", state
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

    `sequence wait` is what proves the transition rather than `wait-ready --match`, which returns
    on the first rule to fire and cannot express "step 1, then step 2".
    """
    harness.phase("a sequence moves to its second step on the next launch")
    before = len(harness.results())
    harness.up("--use", "fixture-sequence")

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


def up_refuses_an_unknown_scenario_and_launches_nothing(harness):
    """The failure path that must not put the app in front of the wrong scenario.

    `up --use nosuch` cannot achieve what it was asked for, so it exits non-zero, relaunches
    nothing, and leaves the proxy up for `down` to stop — the shape this codebase gets wrong most
    often is the one where it would have exited 0 having launched the app against whatever was
    active before.
    """
    harness.phase("`up --use <unknown>` refuses and launches nothing")
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

    # Still up, still intercepting, still the session the caller had — nothing was torn down and
    # nothing was switched underneath them.
    state = harness.status()
    assert state["proxyUp"] is True, state
    assert state["intercepting"] is True, state
    assert state["activeSession"] == "fixture-sequence", state


def down_restores_the_proxy_settings_that_were_there_before(harness):
    """Normal teardown. The comparison is against macOS, not against Lyrebird's own account of
    what it did."""
    harness.phase("`down` restores the proxy settings that were there before")
    assert harness.pac_is_ours(), f"the run was not intercepting before `down`: {harness.pac().describe()}"

    harness.run("down")

    assert harness.restored(), f"before: {harness.baseline.describe()} · after: {harness.pac().describe()}"
    state = harness.status()
    assert state["proxyUp"] is False, state
    assert state["intercepting"] is False, state


def the_watchdog_restores_the_settings_when_the_proxy_is_killed(harness):
    """The failure path `down` never reaches: the proxy dies without being asked to.

    A crash, or a test runner killed mid-run, leaves the Mac routed at a dead port. The watchdog is
    the only thing that puts it back, and it is invisible until the day it is needed — so it is
    checked here against the same recorded baseline the cleanup uses.
    """
    harness.phase("the watchdog restores the settings when the proxy is killed")
    harness.up("--no-relaunch", "--use", "fixture-replaced")
    assert harness.pac_is_ours(), f"the PAC was not installed: {harness.pac().describe()}"

    pid = harness.runtime().get("proxyPid")
    assert pid, f"no proxy pid in the runtime file: {json.dumps(harness.runtime())}"
    os.kill(pid, signal.SIGKILL)

    deadline = time.time() + WATCHDOG_WINDOW
    while not harness.restored() and time.time() < deadline:
        time.sleep(1)

    assert harness.restored(), (
        f"the proxy was killed and the PAC was still pointing at it {WATCHDOG_WINDOW:g}s later.\n"
        f"  before: {harness.baseline.describe()}\n  now:    {harness.pac().describe()}"
    )
