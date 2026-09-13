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

import config
import ownership


class NetworkSetupError(RuntimeError):
    pass


# Per *command*, not per operation: a `networksetup` call normally takes tens of milliseconds, so
# five seconds is already a command that is never coming back. Unbounded, one of these hanging took
# the whole caller with it — including `/health`, which runs on the proxy's event loop, and whose
# silence the watchdog reads as a dead proxy. It bounds the operations built out of these too: one
# restore recipe (`supervisor._restore`) is at most four commands, so ~20s at worst.
_COMMAND_TIMEOUT = 5.0


def _run(args: list[str], check: bool = False, *, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    """`pass_fds` hands the session lock's descriptor to the child: flock is per open-file
    description, so a `networksetup` that outlives a killed parent keeps the lock until it exits."""
    try:
        result = subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=_COMMAND_TIMEOUT, pass_fds=pass_fds
        )
    except UnicodeDecodeError as error:
        # `text=True` decodes, and a service named in bytes this locale cannot decode raises here.
        # Anything but a NetworkSetupError escapes the observers' mapping and kills the watchdog —
        # see test_a_command_with_undecodable_output_is_a_network_setup_error.
        raise NetworkSetupError(f"`{' '.join(args)}` answered with output that could not be decoded: {error}") from None
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


def _first_line(result: subprocess.CompletedProcess) -> str:
    return ((result.stderr or result.stdout or "").strip().splitlines() or [""])[0]


def pac_url() -> str:
    """The URL this process would install. Built by the pure core, so the string written to
    `networksetup` and the one `ownership.lyrebird_port` recognises cannot drift apart."""
    return ownership.our_url(config.CONTROL_PORT)


def pac_status(service: str) -> ownership.Pac:
    """The service's PAC, verbatim. Raises when `networksetup` fails.

    A PAC that could not be read is not a PAC that is absent or somebody else's: reading it as
    `("", False)` made `down` announce "not ours — left untouched" over a PAC it never saw, and the
    watchdog delete the only record of what to put back. Whether the URL is *ours* is not asked
    here — it is relative to a port and a baseline, which `ownership.classify` owns.
    """
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
    return ownership.Pac(url=url, enabled=enabled_match.group(1) == "Yes")


def intercepting(service: str | None, port: int) -> bool:
    """True only when the PAC is enabled *and* points at the proxy on `port`.

    The port is explicit: "ours" is relative to whichever session is being asked about, and a
    reading taken for one port must never answer for another.
    """
    if not service:
        return False
    pac = pac_status(service)
    return pac.enabled and pac.url == ownership.our_url(port)


# MARK: - Route, service table and resolution
#
# Everything below is addressed by *name* at the moment of the call, and resolved from the recorded
# device immediately before each one: a service renamed mid-session must not have somebody else's
# settings written to whatever now carries the old name.


class RouteAmbiguous(NetworkSetupError):
    """Two network services claim the device carrying the default route: which one holds the PAC is
    not something this Mac can be asked."""


class ServiceChanged(NetworkSetupError):
    """The recorded service could not be resolved to exactly one name, right now."""


class UnexpectedPac(NetworkSetupError):
    """A PAC that is neither the state a recipe admitted before writing nor one it wrote."""


_LEGEND = re.compile(r"^an asterisk \(\*\) denotes", re.IGNORECASE)
_ENTRY = re.compile(r"^\((?:\d+|\*)\)\s*(\S.*?)\s*$")
_HARDWARE = re.compile(r"^\(Hardware Port:\s*(.*?),\s*Device:\s*(.*?)\)$")


def route_device() -> str | None:
    """The interface carrying the default route (e.g. 'en0'), or None when there is none.

    None is a claim — "this Mac has no default route right now" — and only one output earns it:
    `route` exiting 0 with `not in table`. Everything else raises; read as None, `down` reported
    "nothing to stop" off a `route` that had failed (test_a_failed_route_command_is_not_no_default_route).
    """
    route = _run(["route", "-n", "get", "default"])
    if "not in table" in route.stderr:
        return None
    if route.returncode != 0:
        raise NetworkSetupError(f"`route -n get default` failed: {_first_line(route) or route.returncode}")
    match = re.search(r"^\s*interface:\s*(\S+)\s*$", route.stdout, re.MULTILINE)
    if not match:
        raise NetworkSetupError(f"`route -n get default` answered without an interface: {_first_line(route)!r}")
    return match.group(1)


def service_table() -> list[tuple[str, str]]:
    """Every network service as (name, device), parsed as a whole-output grammar.

    A `findall` over this listing drops silently whatever it cannot match, so one truncated entry
    for the journalled device read as `Gone` — and `down` archived a session whose service was
    right there. Any line the grammar does not consume raises instead
    (test_service_table_rejects_a_partially_parsable_listing). A `(*)` entry is a *disabled*
    service: still listed, still holding its PAC, and kept
    (test_service_table_keeps_a_disabled_service).
    """
    out = _run(["networksetup", "-listnetworkserviceorder"], check=True).stdout
    lines = out.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not _LEGEND.match(lines[0].strip()):
        raise NetworkSetupError(
            f"`networksetup -listnetworkserviceorder` answered without its legend line: {out.strip()[:80]!r}"
        )
    services: list[tuple[str, str]] = []
    index = 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        entry = _ENTRY.match(line.strip())
        if entry is None or index + 1 >= len(lines):
            raise NetworkSetupError(f"`networksetup -listnetworkserviceorder` printed a line we cannot read: {line!r}")
        hardware = _HARDWARE.match(lines[index + 1].strip())
        if hardware is None:
            raise NetworkSetupError(
                f"`networksetup -listnetworkserviceorder` printed no hardware line for {entry.group(1)!r}"
            )
        services.append((entry.group(1), hardware.group(2)))
        index += 2
    if not services:
        raise NetworkSetupError(f"`networksetup -listnetworkserviceorder` listed no services: {out.strip()[:80]!r}")
    return services


def active_service() -> ownership.ServiceRef | None:
    """The network service carrying the default route, name *and* device — or None when no service
    carries it (Wi-Fi off, or a VPN's utun nothing is configured for).

    Two services on the route's device raise `RouteAmbiguous` rather than answering with the first
    of them: which one holds the PAC is not something this Mac can be asked, and guessing is how a
    session ends up recorded against a service whose PAC it never installed.
    """
    interface = route_device()
    if interface is None:
        return None
    on_device = [name for name, device in service_table() if device == interface]
    if len(on_device) > 1:
        raise RouteAmbiguous(f"two network services carry device '{interface}': {', '.join(on_device)}")
    if not on_device:
        return None
    return ownership.ServiceRef(name=on_device[0], device=interface)


def resolve(ref: ownership.ServiceRef) -> ownership.Present | ownership.Gone | ownership.ServiceAmbiguous:
    """The name that carries `ref.device` now. Raises `NetworkSetupError` when the table could not
    be read — which is a different answer from `Gone`, and the executors map it to `ServiceFailed`."""
    table = service_table()
    on_device = [name for name, device in table if device == ref.device]
    if ref.name in on_device:
        return ownership.Present(ref.name)
    if len(on_device) == 1:
        return ownership.Present(on_device[0])
    if len(on_device) > 1:
        return ownership.ServiceAmbiguous()
    return ownership.Gone()


def resolved_name(ref: ownership.ServiceRef) -> str:
    """The immediate-before check every mutator makes: one name, or nothing is written."""
    resolved = resolve(ref)
    if isinstance(resolved, ownership.Present):
        return resolved.name
    if isinstance(resolved, ownership.ServiceAmbiguous):
        raise ServiceChanged(f"two network services now carry device '{ref.device}'")
    raise ServiceChanged(f"no network service carries device '{ref.device}' any more")


def list_all_services() -> list[str]:
    """Every service name, disabled ones included — the sweep's only list, since this command
    carries no devices.

    A listing without its legend line, or with no services at all, raises: read as an empty list it
    would be a clean sweep over a machine nobody looked at
    (test_list_all_services_rejects_a_malformed_listing).
    """
    out = _run(["networksetup", "-listallnetworkservices"], check=True).stdout
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines or not _LEGEND.match(lines[0]):
        raise NetworkSetupError(
            f"`networksetup -listallnetworkservices` answered without its legend line: {out.strip()[:80]!r}"
        )
    # A leading `*` marks a service as disabled; it still has a PAC, so the name is kept.
    names = [line[1:].strip() if line.startswith("*") else line for line in lines[1:]]
    if not names:
        raise NetworkSetupError("`networksetup -listallnetworkservices` listed no services")
    return names


def write_pac_url(name: str, url: str, *, lock_fd: int) -> None:
    """One command, no read-back of its own: the read-back belongs to the recipe, which performs it
    against a freshly resolved name."""
    _run(["networksetup", "-setautoproxyurl", name, url], check=True, pass_fds=(lock_fd,))


def write_pac_state(name: str, on: bool, *, lock_fd: int) -> None:
    _run(["networksetup", "-setautoproxystate", name, "on" if on else "off"], check=True, pass_fds=(lock_fd,))
