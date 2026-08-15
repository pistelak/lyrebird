"""macOS network-service + PAC helpers, shared by the CLI (supervisor) and the addon (honest status).

Kept in one place so `lyrebird status`, the watchdog, and the dashboard banner all agree on whether
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


def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
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
    out = _run(["networksetup", "-getautoproxyurl", service]).stdout
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
    """Put back whatever was configured before we touched it.

    Only ever called while the current PAC is still ours, so a PAC the user set by hand mid-session
    is left alone.
    """
    if url:
        _run(["networksetup", "-setautoproxyurl", service, url], check=True)
        _run(["networksetup", "-setautoproxystate", service, "on" if enabled else "off"], check=True)
    else:
        _run(["networksetup", "-setautoproxystate", service, "off"], check=True)


def intercepting(service: str | None) -> bool:
    """True only when the PAC is enabled *and* points at our proxy."""
    if not service:
        return False
    status = pac_status(service)
    return status.enabled and status.ours
