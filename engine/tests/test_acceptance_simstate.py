"""The diagnosis a failed acceptance cleanup carries.

The module under test lives in `tests/acceptance/`, but this check cannot: that directory's
`conftest.py` marks everything it collects as `acceptance`, so a check placed beside the code would
be deselected from the fast suite and would only ever run on a machine with a simulator. Loaded by
path rather than by adding the directory to `sys.path`, so nothing else in the run sees it.

Every case here is a cleanup that already failed. The thing being pinned is that the failure still
says what happened: the command, its output, and the device's state — which nothing recorded
before, so a 405 (a state error) could not be read at all. The state is an observation and settles
no cause; it is context for the command that failed, and must never displace it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "simstate", Path(__file__).resolve().parent / "acceptance" / "simstate.py"
)
simstate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(simstate)

# Short and readable, like the other fixtures here: a real udid in the repository is a
# machine-specific value nothing can reproduce, and the privacy gate refuses it.
UDID = "SIM-1"


def _listing(*devices: dict) -> subprocess.CompletedProcess:
    payload = json.dumps({"devices": {"iOS-26-4": list(devices)}})
    return subprocess.CompletedProcess(["xcrun", "simctl", "list", "devices", "-j"], 0, payload, "")


@pytest.mark.parametrize("state", ["Booted", "Shutdown"])
def test_the_state_simctl_reports_is_the_state_named(state):
    described = simstate.describe_state(UDID, run=lambda timeout: _listing({"udid": UDID, "state": state}))
    assert state in described


def test_a_device_missing_from_the_listing_is_not_reported_as_a_state():
    """ "Shutdown" and "gone" are different facts, and a cleanup that could not find its device at
    all is the one most worth saying out loud."""
    described = simstate.describe_state(UDID, run=lambda timeout: _listing({"udid": "other", "state": "Booted"}))
    assert "is not in `simctl list devices`" in described
    assert "Shutdown" not in described


def test_a_refused_query_is_not_read_as_a_state():
    """`simctl` exiting non-zero says nothing about the device. Reporting that as a state would
    invent a fact, and reporting nothing would lose the reason."""
    refused = subprocess.CompletedProcess(["xcrun", "simctl"], 2, "", "simctl: no such device")
    described = simstate.describe_state(UDID, run=lambda timeout: refused)
    assert "state unknown" in described
    assert "no such device" in described


def test_unparseable_output_says_so_rather_than_raising():
    broken = subprocess.CompletedProcess(["xcrun", "simctl"], 0, "not json", "")
    described = simstate.describe_state(UDID, run=lambda timeout: broken)
    assert "did not parse" in described


def test_a_query_that_cannot_run_at_all_still_returns_a_phrase():
    """This runs inside a finalizer with cleanup still to do: an exception thrown out of it
    abandons the rest, which is how a run ends with the app installed and the simulator booted."""

    def explode(timeout):
        raise subprocess.TimeoutExpired(["xcrun", "simctl"], timeout)

    assert "could not be run" in simstate.describe_state(UDID, run=explode)


def test_the_command_that_failed_survives_a_diagnosis_that_also_fails():
    """The finding is the failed cleanup. The device state is context for it, so a snapshot that
    cannot be taken must not take the reason down with it."""

    def explode(timeout):
        raise OSError("xcrun is gone")

    failed = subprocess.CompletedProcess(
        ["xcrun", "simctl", "uninstall", UDID, "com.example.lyrebird-fixture"], 1, "", "code=405"
    )
    message = simstate.cleanup_failure("could not uninstall", failed, UDID, run=explode)
    assert "could not uninstall" in message
    assert "code=405" in message
    assert "exit: 1" in message
    assert "simctl uninstall" in message
    assert "could not be run" in message


def test_a_cleanup_whose_command_never_ran_says_that_much():
    message = simstate.cleanup_failure("could not shut it down", None, UDID, run=lambda timeout: _listing())
    assert "simctl did not run at all" in message
    assert "could not shut it down" in message


def test_a_malformed_device_entry_does_not_raise_out_of_the_finalizer():
    """A listing is whatever `simctl` happened to print. `null` in place of a device used to reach
    `.get` and raise, which in a finalizer means the failure being reported is replaced by this
    one and the rest of the cleanup never runs."""
    broken = subprocess.CompletedProcess(["xcrun"], 0, json.dumps({"devices": {"r": [None, 7, "x"]}}), "")
    assert "entries that are not devices" in simstate.describe_state(UDID, run=lambda timeout: broken)


def test_an_undecodable_answer_does_not_raise_out_of_the_finalizer():
    """`subprocess.run(text=True)` decodes, and decoding raises `UnicodeDecodeError` — a
    `ValueError`, so naming the process and OS errors alone did not cover it."""

    def undecodable(timeout):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    assert "device state unknown" in simstate.describe_state(UDID, run=undecodable)


def test_a_listing_shaped_wrongly_does_not_raise_out_of_the_finalizer():
    wrong = subprocess.CompletedProcess(["xcrun"], 0, json.dumps({"devices": "not a mapping"}), "")
    assert "device state unknown" in simstate.describe_state(UDID, run=lambda timeout: wrong)


@pytest.mark.parametrize(
    "query",
    [
        lambda timeout: subprocess.CompletedProcess(["xcrun"], 0, json.dumps({"devices": {"r": [None]}}), ""),
        lambda timeout: subprocess.CompletedProcess(["xcrun"], 0, "not json", ""),
        lambda timeout: (_ for _ in ()).throw(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")),
    ],
    ids=["malformed-entry", "unparseable", "undecodable"],
)
def test_the_failed_command_survives_every_way_the_diagnosis_can_go_wrong(query):
    """The finding is the cleanup that failed. However badly the state query goes, the command,
    its exit status and both its streams have to reach the reader."""
    failed = subprocess.CompletedProcess(
        ["xcrun", "simctl", "uninstall", UDID, "com.example.lyrebird-fixture"],
        1,
        "some stdout worth keeping",
        "error code=405):\nUnable to lookup in current state: Shutdown",
    )
    message = simstate.cleanup_failure("could not uninstall", failed, UDID, run=query)
    assert "could not uninstall" in message
    assert "simctl uninstall" in message
    assert "exit: 1" in message
    assert "some stdout worth keeping" in message
    assert failed.stderr in message, "the original stderr must survive whole, not just its last line"


def test_a_wall_of_simctl_output_cannot_push_the_failure_out_of_view():
    """Supplementary detail is capped; the failure it accompanies is not."""
    noisy = subprocess.CompletedProcess(["xcrun"], 2, "", "x" * 5000)
    message = simstate.cleanup_failure("could not shut it down", None, UDID, run=lambda timeout: noisy)
    assert "could not shut it down" in message
    assert "characters in all" in message
    assert len(message) < 1200


def test_the_state_query_is_given_a_bound_of_its_own():
    """It runs while a finalizer is already reporting a failure: a diagnosis that hangs turns a
    legible failure into no failure at all."""
    seen = []

    def record(timeout):
        seen.append(timeout)
        return _listing({"udid": UDID, "state": "Booted"})

    simstate.describe_state(UDID, run=record)
    assert seen == [simstate.STATE_TIMEOUT]
    assert 0 < simstate.STATE_TIMEOUT <= 60


def test_a_listing_it_could_not_read_is_not_reported_as_the_device_being_absent():
    """Skipping entries and then saying "not there" states a fact nobody established: a listing
    this cannot read says nothing about whether the device is in it. Two different answers."""
    unreadable = subprocess.CompletedProcess(["xcrun"], 0, json.dumps({"devices": {"r": [None, 7]}}), "")
    described = simstate.describe_state(UDID, run=lambda timeout: unreadable)
    assert "entries that are not devices" in described
    assert "is not in `simctl list devices` at all" not in described


@pytest.mark.parametrize(
    "query",
    [
        lambda timeout: subprocess.CompletedProcess(
            ["xcrun"], 0, json.dumps({"devices": {"r": [{"udid": "SIM-1", "state": "S" * 5000}]}}), ""
        ),
        lambda timeout: subprocess.CompletedProcess(["xcrun"], 2, "", "e" * 5000),
        lambda timeout: (_ for _ in ()).throw(OSError("o" * 5000)),
    ],
    ids=["state", "refusal", "exception"],
)
def test_every_supplemental_answer_is_bounded(query):
    """Not just the one that happened to be noticed first: a state, a refusal and an exception can
    each arrive with a wall of text behind it."""
    assert len(simstate.describe_state(UDID, run=query)) < 600


def test_an_interrupt_is_not_swallowed():
    """The suite turns SIGTERM and SIGHUP into `KeyboardInterrupt` so every finalizer runs. A
    diagnosis that ate one would strand the machine it was trying to hand back."""

    def interrupted(timeout):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        simstate.describe_state(UDID, run=interrupted)


def test_the_real_query_hands_its_bound_to_subprocess(monkeypatch):
    """The timeout has to reach `subprocess.run`, not merely exist as a constant."""
    seen = {}

    def capture(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, json.dumps({"devices": {}}), "")

    monkeypatch.setattr(simstate.subprocess, "run", capture)
    simstate.describe_state(UDID)
    assert seen["timeout"] == simstate.STATE_TIMEOUT
