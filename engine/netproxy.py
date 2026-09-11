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
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=_COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Raised, never returned as empty output: a `networksetup` that did not answer has told us
        # nothing about the PAC, and reading that as "no PAC" is the mistake `pac_status` documents.
        raise NetworkSetupError(f"`{' '.join(args)}` did not finish within {_COMMAND_TIMEOUT:g}s") from None
    except OSError as error:
        # A command that could not be started is the same silence as one that did not finish; a
        # traceback out of `down` used to be the report — see test_a_command_that_cannot_start_is_a_network_setup_error.
        raise NetworkSetupError(f"could not run `{' '.join(args)}`: {error}") from None
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise NetworkSetupError(f"`{' '.join(args)}` failed: {detail or result.returncode}")
    return result


def active_service() -> str | None:
    """The network service carrying the default route (e.g. 'Wi-Fi'), or None when there is none.

    None is a claim — "this Mac has no default route right now" (Wi-Fi off) — and only one output
    earns it: `route` exits 0 with `not in table` on stderr and nothing on stdout. Everything else
    that yields no interface raises: a non-zero exit, or an exit 0 that answered something we do
    not recognise. Before, all of those were None too, and `down` read "no default route" off a
    `route` that had failed and exited 0 with "nothing to stop" — see
    test_a_failed_route_command_is_not_no_default_route.
    """
    route = _run(["route", "-n", "get", "default"])
    if "not in table" in route.stderr:
        return None  # recognised before the exit status is judged: it is the one answer that is None
    if route.returncode != 0:
        raise NetworkSetupError(f"`route -n get default` failed: {_first_line(route) or route.returncode}")
    match = re.search(r"^\s*interface:\s*(\S+)\s*$", route.stdout, re.MULTILINE)
    if not match:
        raise NetworkSetupError(f"`route -n get default` answered without an interface: {_first_line(route)!r}")
    interface = match.group(1)
    order = _run(["networksetup", "-listnetworkserviceorder"], check=True).stdout
    services = re.findall(r"\(\d+\)\s*(.+?)\n\(Hardware Port:.*?Device:\s*(\w+)\)", order)
    if not services:
        raise NetworkSetupError(f"`networksetup -listnetworkserviceorder` listed no services: {order.strip()[:80]!r}")
    for name, device in services:
        if device == interface:
            return name.strip()
    return None  # a VPN's utun, say: the route is real and no service carries it


def _first_line(result: subprocess.CompletedProcess) -> str:
    return ((result.stderr or result.stdout or "").strip().splitlines() or [""])[0]


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
    # Whole lines, anchored: `\s*` used to cross a newline, so `URL: ` with no value read the next
    # line's `Enabled:` as the URL, and an unanchored `Enabled:` could match inside a URL's path.
    # See test_pac_status_reads_each_field_from_its_own_whole_line.
    url_match = re.search(r"^URL:[ \t]*(\S+)[ \t]*$", out, re.MULTILINE)
    enabled_match = re.search(r"^Enabled:[ \t]*(Yes|No)[ \t]*$", out, re.MULTILINE)
    if url_match is None or enabled_match is None:
        # Exit 0 with neither line is not "no PAC, not ours": `down` read that off a truncated
        # answer and left the PAC in place — see test_pac_status_raises_when_the_answer_has_no_url_or_enabled_line.
        raise NetworkSetupError(
            f"`networksetup -getautoproxyurl {service}` answered without a URL/Enabled line: {out.strip()[:80]!r}"
        )
    url = url_match.group(1)
    if url.lower() == "(null)":
        url = ""
    enabled = enabled_match.group(1) == "Yes"
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
