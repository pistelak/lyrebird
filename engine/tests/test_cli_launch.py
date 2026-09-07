"""Selecting the scenario before the app is launched.

An app makes its first requests *while it launches*, so a session selected after the relaunch is
a session the launch never saw — and an app that caches its launch response goes on showing the
old scenario however green a later `lyrebird use` looked. These tests therefore do not assert on
call order for its own sake: the fake `_relaunch` records what the app's launch request was
answered with, and that answer is the evidence.
"""

import pytest

import api
import cli
import config
from cli_doubles import _PHONE, _answers_with_a_conflict, _fake_proxy, _up_with_a_proxy


@pytest.mark.parametrize("adopt", [False, True], ids=["fresh start", "adopting a running proxy"])
def test_up_selects_the_session_before_it_relaunches_the_app(profile, runner, monkeypatch, adopt):
    """The bug: `up` relaunched the app and only the documented `use` afterwards selected the
    scenario, so the launch was answered by whatever session happened to be active — and an app
    that cached that response kept the wrong state on screen for the rest of the run."""
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state, adopt=adopt)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 0, result.output
    assert state["launched"] == [{"session": "orders-outage", "status": 500, "step": 1}], (
        "the app's launch request was answered by a session the caller did not ask for"
    )
    assert state["events"] == [("activate", "orders-outage"), ("relaunch", "com.example.Store")]
    assert state["devices"] == [_PHONE["udid"]], "the launch goes to the resolved device, by UDID"


def test_up_rewinds_the_session_it_selects_so_the_launch_starts_at_step_one(profile, runner, monkeypatch):
    """`--use` on the session that is already active is not a no-op: activation rewinds its
    sequences, and without it the relaunch resumes mid-scenario at whatever step the last run
    left behind."""
    state = _fake_proxy(monkeypatch, active="orders-outage", steps={"orders-outage": 3})
    _up_with_a_proxy(profile, monkeypatch, state, adopt=True)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 0, result.output
    assert state["launched"] == [{"session": "orders-outage", "status": 500, "step": 1}]


def test_up_does_not_relaunch_under_a_fallback_when_the_session_is_unknown(profile, runner, monkeypatch):
    """Relaunching anyway would run the whole suite against the previous session and report every
    step of `up` as a success."""
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "nope"])

    assert result.exit_code == 1
    assert state["launched"] == [], "the app was launched against the session already active"
    assert "no session named 'nope'" in result.output
    assert "default, orders-outage" in result.output, "say which sessions there are"
    assert "NOT relaunched" in result.output
    assert "lyrebird down" in result.output


def test_up_does_not_relaunch_a_session_whose_overrides_were_dropped(profile, runner, monkeypatch):
    """The session is in `sessions` and activates happily, but the rules the scenario is made of
    did not survive the load — so the launch would meet a scenario that is not the one on disk."""
    problem = "orders-outage.json: override[1]: unknown field 'statsu'"
    state = _fake_proxy(monkeypatch, load_problems=[problem], not_whole={"orders-outage": [problem]})
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["launched"] == [] and state["events"] == []
    assert "did not load whole" in result.output
    assert "unknown field 'statsu'" in result.output


def test_up_does_not_relaunch_when_the_named_session_was_replaced_by_an_empty_default(profile, runner, monkeypatch):
    """A malformed `default.json` is skipped and an empty in-memory `default` stands in for it.
    The session list cannot tell those two apart, so only the load problem can."""
    problem = "skipped default.json: Expecting value: line 1 column 1"
    state = _fake_proxy(monkeypatch, load_problems=[problem], not_whole={"default": [problem]})
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "default"])

    assert result.exit_code == 1
    assert state["launched"] == [] and state["events"] == []
    assert "session 'default' did not load whole" in result.output


@pytest.mark.parametrize(
    "problem",
    [
        "skipped other.json: Expecting value: line 1 column 1",
        # The store reports the *file* name, and a file may be named `orders-outage.json: backup.json`
        # — a name it rejects, so no session is reported against it at all. The string it leaves in
        # `loadProblems` nonetheless begins exactly like a problem with `orders-outage`, which is why
        # the decision is taken from `sessionsNotWhole` and not from these strings.
        "skipped orders-outage.json: backup.json: invalid session name "
        "'orders-outage.json: backup' — use letters, digits, dot, dash or underscore",
    ],
    ids=["another session", "a file whose name begins with this session's"],
)
def test_up_ignores_a_load_problem_belonging_to_no_session_of_this_name(profile, runner, monkeypatch, problem):
    """Only the named session's own failure decides. A broken file nobody asked for is not a
    reason to refuse to start the scenario they did — and `loadProblems` alone cannot tell the
    two apart."""
    state = _fake_proxy(monkeypatch, load_problems=[problem])
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 0, result.output
    assert state["launched"] == [{"session": "orders-outage", "status": 500, "step": 1}]


def test_up_does_not_relaunch_when_the_proxy_cannot_say_what_loaded(profile, runner, monkeypatch):
    """Adopting a proxy started before `loadProblems` existed. Silence is not "the session loaded
    whole": reading it as an empty list relaunches the app against a session nothing checked, and
    prints exactly what a checked one prints."""
    state = _fake_proxy(monkeypatch, not_whole=None)
    _up_with_a_proxy(profile, monkeypatch, state, adopt=True)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["launched"] == [] and state["events"] == []
    assert "cannot say whether 'orders-outage' loaded whole" in result.output
    assert "restart the proxy" in result.output


@pytest.mark.parametrize("bundle_id", ["com.example.Store", None], ids=["simBundleId set", "unset"])
def test_up_does_not_relaunch_when_the_activation_call_fails(profile, runner, monkeypatch, bundle_id):
    """The proxy stopped answering between the PAC install and the switch. Launching now would put
    the app in front of exactly the session the caller was trying to replace — and asking for the
    launch by hand is that same wrong launch, typed by a person. The final look still runs: the
    proxy is up and the PAC is installed, and an operator not told so walks away believing the
    network was left alone."""
    state = _fake_proxy(monkeypatch, unreachable=True)
    _up_with_a_proxy(profile, monkeypatch, state, bundle_id=bundle_id)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["launched"] == []
    assert state["events"] == [("activate", "orders-outage")]
    assert "proxy not reachable" in result.output, "the API's own detail is kept"
    assert "NOT relaunched" in result.output
    assert "RELAUNCH THE APP NOW" not in result.output
    assert "INTERCEPT ACTIVE" in result.output


def test_up_prints_both_fingerprints_when_the_activation_is_refused_for_another_profile(profile, runner, monkeypatch):
    """The port changed hands before the switch, so the PUT is answered by a proxy running someone
    else's profile. `_control` prints the two fingerprints on its way out; what it cannot know is
    what the refusal costs here, so `up` adds that the app was NOT relaunched — and neither
    sentence may be lost to the other."""
    real_control = api._control  # the 409 has to travel the path it travels in production
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state)
    monkeypatch.setattr(api, "_control", real_control)
    _answers_with_a_conflict(monkeypatch, {"error": "profile_mismatch", "running": "a1b2c3", "requested": "d4e5f6"})

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["launched"] == []
    assert "a1b2c3" in result.output, "say which profile actually holds the port"
    assert config.PROFILE_FINGERPRINT in result.output, "and which one was asked for"
    assert "NOT relaunched" in result.output


@pytest.mark.parametrize("bundle_id", ["com.example.Store", None], ids=["simBundleId set", "unset"])
def test_up_no_relaunch_launches_nothing_and_says_who_owns_it(profile, runner, monkeypatch, bundle_id):
    """With a UI runner owning the app, `up` launching it is a second launch racing the first —
    and the reminder to launch one by hand is the same instruction to the operator."""
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state, bundle_id=bundle_id)

    result = runner.invoke(cli.cli, ["up", "--no-relaunch"])

    assert result.exit_code == 0, result.output
    assert state["launched"] == []
    assert "relaunch skipped" in result.output
    assert "RELAUNCH THE APP NOW" not in result.output


def test_up_use_with_no_relaunch_still_selects_the_session(profile, runner, monkeypatch):
    """The caller launches the app itself, and needs the scenario live before it does."""
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage", "--no-relaunch"])

    assert result.exit_code == 0, result.output
    assert state["events"] == [("activate", "orders-outage")]
    assert state["active"] == "orders-outage"


def test_up_leaves_the_active_session_alone_when_no_use_is_given(profile, runner, monkeypatch):
    """`up` is not a session switch. Selecting one unasked would rewind a scenario the operator
    set up by hand before running it."""
    state = _fake_proxy(monkeypatch, active="orders-outage")
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert state["events"] == [("relaunch", "com.example.Store")]


@pytest.mark.parametrize(
    "args",
    [
        ["--relaunch", "com.example.Store", "--no-relaunch"],
        ["--no-relaunch", "--relaunch", "com.example.Store"],
        ["--use", "  "],
    ],
)
def test_up_refuses_a_contradictory_launch_request_before_it_starts_anything(profile, runner, args):
    """Answering "which of these did you mean" after the network has been rewired is too late.
    There is no profile here, and `up` must not get as far as complaining about that."""
    result = runner.invoke(cli.cli, ["up", *args])

    assert result.exit_code == 2, result.output
    assert "no profile" not in result.output, "the usage error must come before any side effect"
