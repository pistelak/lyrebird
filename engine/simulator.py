"""Which simulator to act on, and the two things Lyrebird does to one: trust the CA, relaunch."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import NamedTuple

import click

import config
import ui

# MARK: - Small helpers


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def _ca_cert() -> Path:
    return config.mitmproxy_confdir() / "mitmproxy-ca-cert.pem"


class SimulatorError(Exception):
    """No simulator to act on, and why. The message is the sentence the operator reads."""


class Simulator(NamedTuple):
    """A device simctl commands are addressed to *by UDID* — never by `booted`."""

    udid: str
    name: str

    def __str__(self) -> str:
        return f"{self.name} ({self.udid})"


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
    if result.returncode != 0:
        raise SimulatorError(f"`{printable}` failed: {_first_line(result, f'exit {result.returncode}')}")
    try:
        listing = json.loads(result.stdout)["devices"]
        return [{**device, "runtime": runtime} for runtime, devices in listing.items() for device in devices]
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SimulatorError(f"could not read the output of `{printable}`") from None


def _is_named(device: dict, wanted: str) -> bool:
    """UDID or full device name, either case. Never a substring: `--simulator 'iPhone 17'` picking
    an 'iPhone 17 Pro' is the same wrong-device bug this option exists to end."""
    return wanted.casefold() in (str(device.get("udid", "")).casefold(), str(device.get("name", "")).casefold())


def _platform(device: dict) -> str:
    """`iOS`, `watchOS`, `tvOS`… — the platform simctl files the device's runtime under.

    Read from the runtime identifier the device was listed beneath: a reverse-DNS string whose
    last dot-separated segment is the platform and its version joined by hyphens — "…SimRuntime."
    then "iOS-26-4" — which is the only place the listing says what a device is.

    An identifier this cannot read returns "", and "" is never treated as iOS: guessing here would
    hand the CA to a device Lyrebird cannot relaunch an iOS app on.
    """
    tail = str(device.get("runtime", "")).rsplit(".", 1)[-1]
    return tail.split("-")[0] if tail else ""


def _is_eligible(device: dict) -> bool:
    """A device both operations can be performed on: an iOS simulator simctl calls usable."""
    return _platform(device) == "iOS" and bool(device.get("isAvailable", True))


def _listing(devices: list[dict]) -> str:
    return "".join(
        f"\n     · {device.get('name', '?')}  {device.get('udid', '?')}  "
        f"[{_platform(device) or 'unknown runtime'} · {device.get('state', 'unknown')}]"
        for device in devices
    )


def resolve_simulator(selector: str | None) -> Simulator:
    """The booted simulator to trust the CA in and relaunch on, or raise saying why there is none.

    Nothing here is addressed to simctl's `booted` keyword, because that keyword answers a
    question it was never asked: `simctl help` says that "if multiple devices are booted … simctl
    will choose one of them", and it does not report which. A CA trusted on a device nobody chose
    looks exactly like a CA trusted on the right one, right up to the point where the app under
    test rejects the certificate. So: exactly one booted *candidate* is unambiguous and stays the
    default, more than one is a question only the caller can answer, and `--simulator` is the
    answer. Selection is by UDID from here on, whichever way the device was picked.

    A candidate is a booted iOS simulator simctl calls available — the two things this tool does
    are trusting a CA and relaunching an iOS app, and neither is something to do to a watch.

    Device selection binds the CA and the relaunch to one simulator. It does *not* isolate that
    simulator's traffic — the PAC is installed on a network service and scoped by hostname, so
    every simulator on this Mac, and the Mac itself, follows it for those hosts.
    """
    devices = _simctl_devices()
    booted = [device for device in devices if device.get("state") == "Booted"]
    eligible = [device for device in booted if _is_eligible(device)]

    if selector is None:
        # Candidates, not merely booted devices. A paired Apple Watch boots alongside its phone,
        # and counting it would make "ambiguous" the normal state of such a Mac; a watch booted on
        # its own is not something to hand an iOS app's CA to just because it is the only thing up.
        if not eligible:
            if booted:
                raise SimulatorError(
                    f"no booted iOS simulator — what is booted is not something Lyrebird can "
                    f"trust a CA in and relaunch an iOS app on:{_listing(booted)}\n"
                    f"   boot an iOS simulator, or name one with `--simulator <udid-or-name>`"
                )
            raise SimulatorError(
                "no booted simulator — boot one (Simulator.app, or `xcrun simctl boot <udid>`), then run this again"
            )
        if len(eligible) > 1:
            raise SimulatorError(
                f"{len(eligible)} iOS simulators are booted, and which one you meant is not "
                f"simctl's guess to make:{_listing(eligible)}\n"
                f"   name one with `--simulator <udid-or-name>`"
            )
        return _as_simulator(eligible[0])

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
        # Booted is not the same as usable, and a device named by hand deserves the reason it
        # cannot be used rather than a quiet promotion of some other device in its place.
        _require_eligible(matches[0], wanted)
        return _as_simulator(matches[0])

    # Not booted, or not there at all — two different problems with two different answers, so the
    # rest of the same listing is consulted rather than reporting whichever is shorter to say.
    known = [device for device in devices if _is_named(device, wanted)]
    if not known:
        raise SimulatorError(f"no simulator '{wanted}' on this Mac — `xcrun simctl list devices` shows what there is")
    usable = [device for device in known if _is_eligible(device)]
    if not usable:
        # Booting it would not make it usable, so the answer here is not "boot it": say what is
        # wrong with the device itself, taking the first one when a name covers several.
        _require_eligible(known[0], wanted)
    # One name can belong to several devices — the same model on two runtimes — so the UDID to
    # boot is only named when there is one of them to name.
    which = usable[0].get("udid", wanted) if len(usable) == 1 else "<udid above>"
    raise SimulatorError(f"'{wanted}' is not booted:{_listing(usable)}\n   boot it with `xcrun simctl boot {which}`")


def _require_eligible(device: dict, wanted: str) -> None:
    """Raise unless this named device is one both device operations can be performed on."""
    if not device.get("isAvailable", True):
        reason = device.get("availabilityError") or "its runtime is not installed"
        raise SimulatorError(f"'{wanted}' cannot be used: {reason}")
    platform = _platform(device)
    if platform != "iOS":
        raise SimulatorError(
            f"'{wanted}' is a {platform or 'non-iOS'} simulator — Lyrebird trusts its CA in, and "
            f"relaunches an iOS app on, an iOS simulator. Name one with `--simulator <udid>`."
        )


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
        recorded = config.read_runtime().get("simulator")
        if isinstance(recorded, dict) and recorded.get("udid"):
            selector = str(recorded["udid"])
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


@click.command(name="trust-ca")
@click.option(
    "--simulator",
    "simulator_selector",
    default=None,
    metavar="UDID-OR-NAME",
    help="Which simulator to trust the CA in. Defaults to the booted one; required when more than one is booted.",
)
def trust_ca_cmd(simulator_selector: str | None) -> None:
    """(Re)trust the Lyrebird CA in a booted simulator.

    With one simulator booted that is the one; with several, name it — `booted` would let simctl
    pick, and a CA trusted on the wrong device is indistinguishable from one trusted on the right
    device until the app rejects the certificate.
    """
    try:
        simulator = resolve_simulator(simulator_selector)
    except SimulatorError as error:
        click.echo(f"{ui.RED}✗ {error}{ui.R}")
        raise SystemExit(1) from None
    ok, message = trust_ca_in_sim(simulator)
    click.echo(f"{'✓' if ok else '✗'} {message}")
    if not ok:
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
