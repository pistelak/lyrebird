"""Which simulator to act on, and the two things Lyrebird does to one: trust the CA, relaunch."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click

import config
import ownership
import session
import ui
from ownership import Simulator

# MARK: - Small helpers


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def _ca_cert() -> Path:
    return config.mitmproxy_confdir() / "mitmproxy-ca-cert.pem"


class SimulatorError(Exception):
    """No simulator to act on, and why. The message is the sentence the operator reads."""


# One type for a device, defined in the pure core: `resolve_simulator` returns exactly what
# `SessionRecord.simulator` stores, so nothing converts at the journal boundary.
__all__ = ["Simulator"]


def _first_line(result: subprocess.CompletedProcess, fallback: str) -> str:
    """simctl reports failures as several lines of nested domain/code detail; only the first
    carries something a person can act on."""
    text = (result.stderr or result.stdout or "").strip()
    return text.splitlines()[0] if text else fallback


def _simctl_devices() -> list[dict]:
    """Every device `simctl list devices` reports, flattened across runtimes.

    Raises rather than returning `[]` when simctl could not be asked. "There are no simulators"
    and "I could not find out" are different claims, and printing the second as the first sends
    an operator off to boot a device that is already running.

    All of them, in one reading: what is booted is decided from `state` below, so a device cannot
    be called absent by one listing and shut down by another taken a moment later.
    """
    args = ["xcrun", "simctl", "list", "devices", "--json"]
    printable = " ".join(args)
    try:
        result = _run(args)
    except OSError as error:
        raise SimulatorError(f"could not run `{printable}`: {error} — is Xcode installed?") from None
    # One refusal for everything short of a readable listing, carrying what the command said on
    # stderr: a listing that exits 0 with nothing usable on stdout usually said why there.
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        raise SimulatorError(f"`{printable}` failed: {stderr or result.stdout.strip() or f'exit {result.returncode}'}")
    try:
        listing = json.loads(result.stdout)["devices"]
        return [device for devices in listing.values() for device in devices]
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise SimulatorError(f"could not read `{printable}`: {error}{f' — {stderr}' if stderr else ''}") from None


def _is_named(device: dict, wanted: str) -> bool:
    """The exact UDID or full device name. Never a substring: `--simulator 'iPhone 17'` picking
    an 'iPhone 17 Pro' is the same wrong-device bug this option exists to end."""
    return wanted in (str(device.get("udid", "")), str(device.get("name", "")))


def _listing(devices: list[dict]) -> str:
    return "".join(
        f"\n     · {device.get('name', '?')}  {device.get('udid', '?')}  [{device.get('state', 'unknown')}]"
        for device in devices
    )


def resolve_simulator(selector: str | None) -> Simulator:
    """The booted simulator to trust the CA in and relaunch on, or raise saying why there is none.

    Nothing here is addressed to simctl's `booted` keyword, because that keyword answers a
    question it was never asked: `simctl help` says that "if multiple devices are booted … simctl
    will choose one of them", and it does not report which. A CA trusted on a device nobody chose
    looks exactly like a CA trusted on the right one, right up to the point where the app under
    test rejects the certificate. So: exactly one booted device is unambiguous and stays the
    default, more than one is a question only the caller can answer, and `--simulator` is the
    answer. Selection is by UDID from here on, whichever way the device was picked.

    A device that cannot serve — a watch, or one whose runtime is missing — fails at CA trust or at
    the launch, and either unwinds `up`. It is not sorted out here.

    Device selection binds the CA and the relaunch to one simulator. It does *not* isolate that
    simulator's traffic — the PAC is installed on a network service and scoped by hostname, so
    every simulator on this Mac, and the Mac itself, follows it for those hosts.
    """
    devices = _simctl_devices()
    booted = [device for device in devices if device.get("state") == "Booted"]

    if selector is None:
        if not booted:
            raise SimulatorError(
                "no booted simulator — boot one (Simulator.app, or `xcrun simctl boot <udid>`), then run this again"
            )
        if len(booted) > 1:
            raise SimulatorError(
                f"{len(booted)} simulators are booted, and which one you meant is not "
                f"simctl's guess to make:{_listing(booted)}\n"
                f"   name one with `--simulator <udid-or-name>`"
            )
        return _as_simulator(booted[0])

    wanted = selector.strip()
    if not wanted:
        raise SimulatorError("--simulator needs a device UDID or name")
    matches = [device for device in booted if _is_named(device, wanted)]
    if len(matches) > 1:
        raise SimulatorError(
            f"'{wanted}' names {len(matches)} booted simulators:{_listing(matches)}\n"
            f"   name one by UDID with `--simulator <udid>`"
        )
    if matches:
        return _as_simulator(matches[0])

    # Not booted, or not there at all — two different problems with two different answers, so the
    # rest of the same listing is consulted rather than reporting whichever is shorter to say.
    known = [device for device in devices if _is_named(device, wanted)]
    if not known:
        raise SimulatorError(f"no simulator '{wanted}' on this Mac — `xcrun simctl list devices` shows what there is")
    # One name can belong to several devices — the same model on two runtimes — so the UDID to
    # boot is only named when there is one of them to name.
    which = known[0].get("udid", wanted) if len(known) == 1 else "<udid above>"
    raise SimulatorError(f"'{wanted}' is not booted:{_listing(known)}\n   boot it with `xcrun simctl boot {which}`")


def _as_simulator(device: dict) -> Simulator:
    udid = str(device.get("udid") or "")
    if not udid:
        raise SimulatorError("simctl reported a device with no UDID")
    return Simulator(udid, str(device.get("name") or "unnamed simulator"))


def trust_ca_in_sim(simulator: Simulator) -> tuple[bool, str]:
    if not _ca_cert().is_file():
        return False, "CA cert not generated yet (start the proxy first)"
    result = _run(["xcrun", "simctl", "keychain", simulator.udid, "add-root-cert", str(_ca_cert())])
    if result.returncode == 0:
        return True, f"trusted in {simulator}"
    return False, (
        f"could not trust the CA in {simulator}: {_first_line(result, f'simctl exited {result.returncode}')}"
    )


def _relaunch(bundle_id: str, simulator: Simulator) -> tuple[bool, str]:
    """Terminate is allowed to fail — the app may not be running. Launch is not.

    simctl reports failures as several lines of nested domain/code detail. Only the first line
    carries information a person can act on, and the common failure has a much better answer
    than the text simctl produces.
    """
    _run(["xcrun", "simctl", "terminate", simulator.udid, bundle_id])
    result = _run(["xcrun", "simctl", "launch", simulator.udid, bundle_id])
    if result.returncode == 0:
        return True, str(simulator)
    raw = (result.stderr or result.stdout or "launch failed").strip()
    if "failed to launch" in raw or "not find" in raw.lower():
        return False, f"{bundle_id} is not installed in {simulator}"
    return False, raw.splitlines()[0]


def _bound_simulator(selector: str | None) -> Simulator:
    """The device this session is bound to, or raise saying why there is none.

    `--simulator` wins; otherwise it is the device `up` recorded, because that is the one holding
    the CA — relaunching the app anywhere else would put it in front of a proxy whose certificate
    it does not trust. The recorded UDID goes back through `resolve_simulator`, so a device that
    has been shut down since is reported rather than assumed; with nothing recorded, the same
    rule as `up`'s applies (the single booted candidate, or a refusal naming them).
    """
    if selector is None:
        journal = session.Session().read()
        if isinstance(journal, ownership.Unreadable):
            # Not "nothing recorded": that would resolve to whatever is booted, which may not be
            # the device holding the CA. See test_relaunch_refuses_over_a_journal_it_cannot_read.
            raise SimulatorError(
                f"the device this run trusted is recorded in session.json, which cannot be read "
                f"({journal.reason}) — pass --simulator, or run `lyrebird down`"
            )
        if isinstance(journal, ownership.SessionRecord) and journal.simulator is not None:
            selector = journal.simulator.udid
    return resolve_simulator(selector)


@click.command(name="relaunch")
@click.argument("bundle_id", metavar="[BUNDLEID]", required=False)
@click.option(
    "--simulator",
    "simulator_selector",
    default=None,
    metavar="UDID-OR-NAME",
    help="Relaunch on this simulator instead of the one `up` recorded.",
)
def relaunch_cmd(bundle_id: str | None, simulator_selector: str | None) -> None:
    """Terminate and relaunch the app on the simulator this run is bound to.

    The device is the one `up` used — the one carrying the CA — not whatever happens to be booted
    when you run this. That is what makes it safe to call from a button: with two simulators
    booted, `xcrun simctl launch booted` would let simctl choose, and half the time it would
    choose the device that never trusted the CA.

    BUNDLEID defaults to `simBundleId` from the profile.
    """
    if not bundle_id:
        config.reload_profile()
    target = bundle_id or config.PROFILE.sim_bundle_id
    if not target:
        click.echo(
            f"{ui.RED}✗ nothing to relaunch: pass a bundle id, or set simBundleId in {config.PROFILE_FILE}{ui.R}"
        )
        raise SystemExit(1)
    try:
        simulator = _bound_simulator(simulator_selector)
    except SimulatorError as error:
        click.echo(f"{ui.RED}✗ {error}{ui.R}")
        raise SystemExit(1) from None
    launched, detail = _relaunch(target, simulator)
    click.echo(f"{'✓' if launched else '✗'} relaunch {target}: {detail}")
    if not launched:
        raise SystemExit(1)


@click.command(name="untrust-ca")
def untrust_ca_cmd() -> None:
    """Explain how to remove the Lyrebird CA from the simulator."""
    click.echo(
        "simctl exposes no remove-root-cert; to drop trust use either:\n"
        "  xcrun simctl keychain <udid> reset        # clears added certs on that simulator\n"
        "  Device ▸ Erase All Content and Settings   # full reset\n"
        "  (`lyrebird status` names the simulator the last `up` used; `xcrun simctl list\n"
        "   devices booted` lists the rest)\n"
        f"\nLyrebird's CA lives in {config.mitmproxy_confdir()} — delete that directory to\n"
        "rotate it; a new one is generated on the next `up`."
    )
