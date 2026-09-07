"""Calls to the proxy's control API, and the profile scoping every one of them carries."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import click

import config
import ui

CONTROL = config.CONTROL_ORIGIN
_PROFILE_HEADER = "X-Lyrebird-Profile"  # says which profile this call means; see `_profile_mismatch`


def _profile_mismatch(running: str) -> str:
    """The one sentence for "the port is held by someone else's proxy", wherever we learn it.

    It names both fingerprints and both remedies: "a different profile" without them leaves the
    operator no way to tell which one they are looking at.
    """
    return (
        f"{ui.RED}a different profile is already running on port {config.CONTROL_PORT}{ui.R}\n"
        f"  running: {running}   requested: {config.PROFILE_FINGERPRINT}\n"
        f"  stop it first (`lyrebird down`) or use a different --profile, or another port via\n"
        f"  LYREBIRD_CONTROL_PORT."
    )


def _error_body(error: urllib.error.HTTPError) -> dict:
    """The API's JSON body, or {} when there is nothing usable to read — never a partial dict."""
    try:
        body = json.loads(error.read().decode())
    except (ValueError, OSError):
        return {}
    return body if isinstance(body, dict) else {}


def _refuse_a_foreign_profile(error: urllib.error.HTTPError, body: dict, unproven_exit: int = 1) -> None:
    """Exits when the API says the request named a profile it is not running.

    The 409 counterpart of `_require_same_profile`, which explains the scoping; shared by both
    callers so a scoped read fails the same way a scoped mutation does. `unproven_exit` is that
    function's, and matters here because this refusal can arrive at any call a command makes,
    including its last.
    """
    if error.code == 409 and body.get("error") == "profile_mismatch":
        click.echo(_profile_mismatch(body.get("running") or "unknown"), err=True)
        raise SystemExit(unproven_exit)


def _get_json(path: str, timeout: float = 1.5, *, unproven_exit: int = 1) -> Any:
    request = urllib.request.Request(
        f"{CONTROL}{path}", headers={"Host": config.CONTROL_HOST_HEADER, _PROFILE_HEADER: config.PROFILE_FINGERPRINT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        # A mismatch is an answer, not an outage: returning None would point at a dead port.
        _refuse_a_foreign_profile(error, _error_body(error), unproven_exit)
        return None
    except Exception:  # any other failure means 'not reachable', which is the answer
        return None


def _require_same_profile(health: dict, *, unproven_exit: int = 1) -> None:
    """Exits when a health reading describes a proxy running some other profile.

    `/health` is deliberately unscoped at the API — that is how `down` recovers across profiles —
    so a command that goes on to *interpret* a health reading has to make the comparison itself,
    or it reports another profile's scenarios, counters and traffic as this profile's. A reading
    with no fingerprint is accepted, as `up` accepts one: an older engine cannot say.

    A command that polls makes the comparison on every reading, not only the first: the proxy that
    answered the first read can be stopped and another profile's started on the port mid-wait, so
    a baseline taken from one store would end up compared against a stranger's.

    `unproven_exit` exists for a caller whose exit codes already separate "I asked and the answer is
    no" from "I could not ask". The port changing hands says nothing about the question that was
    put, so answering it under the code for a failed check would invent a result — and this is the
    one refusal a command cannot see coming, since the reading it is about looks perfectly healthy.
    """
    running = health.get("profileFingerprint")
    if running and running != config.PROFILE_FINGERPRINT:
        click.echo(_profile_mismatch(running), err=True)
        raise SystemExit(unproven_exit)


def _health() -> dict | None:
    return _get_json("/__mock__/health")


def _control(path: str, method: str = "GET", payload: Any = None, timeout: float = 3.0) -> Any:
    """Call the control API, or exit with its error message.

    Exists so nothing outside this function has to remember the loopback Host header, the profile
    this call means, or the JSON content-type the API requires — the first and last of which
    otherwise fail as a bare 421 or 415, and the middle of which would mutate a stranger's profile.
    """
    headers = {"Host": config.CONTROL_HOST_HEADER, _PROFILE_HEADER: config.PROFILE_FINGERPRINT}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["content-type"] = "application/json"
    request = urllib.request.Request(f"{CONTROL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        body = _error_body(error)
        # Before the generic path: a mismatch carries no `detail`, so it would print a bare slug.
        _refuse_a_foreign_profile(error, body)
        # `detail` first: the API sends the sentence that names the problem ("match: unknown
        # field 'kind' — a matcher may only carry method, path, query, bodyContains") and
        # `error` only a slug for it. Printing the slug throws away the half that tells you
        # what to do, which is how a supported matcher field ends up looking unsupported.
        detail = body.get("detail") or body.get("error") or error.reason
        click.echo(f"{ui.RED}✗ {detail}{ui.R}")
        raise SystemExit(1) from None
    except OSError:
        click.echo(f"{ui.RED}✗ proxy not reachable — is it running? (`lyrebird up`){ui.R}")
        raise SystemExit(1) from None
