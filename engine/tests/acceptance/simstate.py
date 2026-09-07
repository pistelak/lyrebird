"""What a cleanup failure should say about the simulator it was talking to.

Lives in its own module rather than in `conftest.py` so it can be tested without a simulator:
`tests/acceptance/conftest.py` applies the `acceptance` marker to everything it collects, so a
hermetic check placed beside it could never run in the fast suite.

Nothing here raises. It is called from finalizers that still have cleanup to do: an exception
thrown out of one of those replaces the failure being reported and abandons the rest of the
cleanup, which is how a run ends with the app installed and the simulator still booted.
"""

from __future__ import annotations

import json
import subprocess

# Its own bound, and a short one: this runs while a finalizer is already reporting a failure, and a
# diagnosis that hangs turns a legible failure into no failure at all.
STATE_TIMEOUT = 20


def _simctl_list(timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["xcrun", "simctl", "list", "devices", "-j"], capture_output=True, text=True, timeout=timeout)


def describe_state(udid: str, run=_simctl_list) -> str:
    """What `simctl` says this device's state is, as a phrase for a failure message.

    An *observation*, never a cause. A device found `Shutdown` after an uninstall failed does not
    establish that something outside the run shut it down — it is one fact, and a message that
    guessed at the rest would send its reader somewhere nobody has evidence for.

    The five answers are five different facts, and collapsing them would defeat the point: the
    state, a device that is not in the listing at all, a `simctl` that refused, output that did not
    parse, and a query that could not be made.
    """
    try:
        return _describe(udid, run)
    except Exception as error:
        # The catch is broad because the caller is a finalizer reporting a failure: anything raised
        # here replaces that failure with this one and abandons the cleanup still owed. `Exception`
        # rather than `BaseException` on purpose — the suite turns SIGTERM and SIGHUP into
        # `KeyboardInterrupt` to unwind deliberately, and swallowing that would strand the machine.
        return _clip(f"device state unknown: reading it raised {error!r}")


def _describe(udid: str, run) -> str:
    try:
        result = run(STATE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        return _clip(f"device state unknown: `simctl list devices` could not be run ({error!r})")
    if result.returncode != 0:
        return (
            f"device state unknown: `simctl list devices -j` exited {result.returncode}"
            f" — {_clip((result.stderr or result.stdout or '').strip()) or 'no output'}"
        )
    try:
        listing = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return _clip(f"device state unknown: `simctl list devices -j` output did not parse ({error!r})")
    malformed = False
    for runtime in listing["devices"].values():
        for device in runtime:
            # `.get` on whatever the listing happens to hold: a malformed entry is a reason to say
            # so, not to raise out of a finalizer. Reproduced with `{"devices": {"r": [null]}}`.
            if not isinstance(device, dict):
                malformed = True
                continue
            if device.get("udid") == udid:
                return _clip(f"device state when this failed: {device.get('state') or 'reported without one'}")
    if malformed:
        # Skipping the entries and then reporting "not there" would state a fact nobody established:
        # a listing this program could not read says nothing about whether the device is in it.
        return "device state unknown: `simctl list devices -j` listed entries that are not devices"
    return f"device state: {udid} is not in `simctl list devices` at all"


def _clip(text: str, limit: int = 400) -> str:
    """Supplementary detail is capped; the failure it accompanies is not. A `simctl` that answered
    with a wall of text must not push the command that actually failed out of view."""
    return text if len(text) <= limit else f"{text[:limit]}… ({len(text)} characters in all)"


def cleanup_failure(what: str, result: subprocess.CompletedProcess | None, udid: str, run=_simctl_list) -> str:
    """The message a failed cleanup command should carry.

    The same shape `_fail` uses for everything else in the suite — command, exit, stdout, stderr —
    because these two were the least informative failures in the file while being the ones most
    likely to fire on somebody else's machine. The device state is appended, never substituted: the
    command that failed is the finding, and the state is context for it.
    """
    if result is None:
        detail = "  simctl did not run at all"
    else:
        detail = (
            f"  command: {' '.join(result.args)}\n"
            f"  exit: {result.returncode}\n"
            f"  stdout:\n{result.stdout}\n"
            f"  stderr:\n{result.stderr}"
        )
    return f"{what}\n{detail}\n  {describe_state(udid, run=run)}"
