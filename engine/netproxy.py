"""macOS network-service + PAC helpers, shared by the CLI (supervisor) and the addon (honest status).

Kept in one place so `lyrebird status`, the watchdog, and `/health` all agree on whether
the PAC is *actually* routing configured-host traffic to us — not just whether the proxy process is
alive.

`networksetup` failures are raised rather than ignored: reporting "direct networking restored"
while a stale PAC still points at a dead port is worse than reporting nothing.
"""

from __future__ import annotations

import re
import subprocess
from typing import NamedTuple

import config


class NetworkSetupError(RuntimeError):
    pass


# Per *command*, not per operation: a `networksetup` call normally takes tens of milliseconds, so
# five seconds is already a command that is never coming back. Unbounded, one of these hanging took
# the whole caller with it — including `/health`, which runs on the proxy's event loop, and whose
# silence the watchdog reads as a dead proxy. It bounds the operations built out of these too: one
# watchdog restore attempt is four commands (`cli._restore_previous_pac`), so ~20s at worst.
_COMMAND_TIMEOUT = 5.0


def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True,
                                timeout=_COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Raised, never returned as empty output: a `networksetup` that did not answer has told us
        # nothing about the PAC, and reading that as "no PAC" is the mistake `pac_status` documents.
        raise NetworkSetupError(
            f"`{' '.join(args)}` did not finish within {_COMMAND_TIMEOUT:g}s"
        ) from None
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise NetworkSetupError(f"`{' '.join(args)}` failed: {detail or result.returncode}")
    return result


def active_service() -> str | None:
    """The network service carrying the default route (e.g. 'Wi-Fi')."""
    match = re.search(r"interface:\s*(\S+)", _run(["route", "-n", "get", "default"]).stdout)
    if not match:
        return None
    interface = match.group(1)
    order = _run(["networksetup", "-listnetworkserviceorder"]).stdout
    for name, device in re.findall(r"\(\d+\)\s*(.+?)\n\(Hardware Port:.*?Device:\s*(\w+)\)", order):
        if device == interface:
            return name.strip()
    return None


def pac_url() -> str:
    return f"{config.CONTROL_ORIGIN}/proxy.pac"


class PacStatus(NamedTuple):
    """The service's current PAC URL and enabled state, and whether that URL is ours.

    A NamedTuple rather than a dict because every consumer subscripted string literals, and a
    typo failed only at runtime — inside `down`, at the moment it is meant to be restoring the
    user's network settings.
    """

    url: str
    enabled: bool
    ours: bool


def pac_status(service: str) -> PacStatus:
    """Raises when `networksetup` fails. A PAC that could not be read is not a PAC that is absent
    or somebody else's: reading it as `("", False, False)` made `down` announce "not ours — left
    untouched" over a PAC it never saw, and the watchdog delete the only record of what to put
    back. Callers that can carry on without the answer catch this; the ones named for restoring
    the network do not."""
    out = _run(["networksetup", "-getautoproxyurl", service], check=True).stdout
    url_match = re.search(r"URL:\s*(\S+)", out)
    url = url_match.group(1) if url_match else ""
    if url.lower() == "(null)":
        url = ""
    enabled = "Enabled: Yes" in out
    return PacStatus(url=url, enabled=enabled, ours=url == pac_url())


def set_pac(service: str) -> None:
    _run(["networksetup", "-setautoproxyurl", service, pac_url()], check=True)
    _run(["networksetup", "-setautoproxystate", service, "on"], check=True)
    status = pac_status(service)
    if not (status.enabled and status.ours):
        raise NetworkSetupError(f"PAC did not take effect on '{service}' (now: {status})")


def restore_pac(service: str, url: str, enabled: bool) -> None:
    """Put back whatever was configured before we touched it, and confirm it took.

    Only ever called while the current PAC is still ours, so a PAC the user set by hand mid-session
    is left alone.

    URL first, then the state: `-setautoproxyurl` switches the PAC on as a side effect, so the
    state has to be set after it. That means a failure between the two calls leaves the user's URL
    with the wrong flag — which is why the caller that retries (`cli._restore_previous_pac`) treats
    a PAC at the recorded previous URL as still restorable, not as one somebody set by hand.

    No URL means "there was no PAC before", and that is restored as *off* whatever the recorded
    flag says: macOS rejects an empty URL, so "on" here could only mean leaving ours enabled.

    Read back at the end, as `set_pac` does: `networksetup` can exit 0 and change nothing.
    """
    enabled = bool(url) and enabled
    if url:
        _run(["networksetup", "-setautoproxyurl", service, url], check=True)
    _run(["networksetup", "-setautoproxystate", service, "on" if enabled else "off"], check=True)
    status = pac_status(service)
    if status.enabled != enabled or (url and status.url != url):
        raise NetworkSetupError(
            f"PAC on '{service}' did not restore (wanted url={url!r} enabled={enabled}, now: {status})"
        )


def intercepting(service: str | None) -> bool:
    """True only when the PAC is enabled *and* points at our proxy."""
    if not service:
        return False
    status = pac_status(service)
    return status.enabled and status.ours
