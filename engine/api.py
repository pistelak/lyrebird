"""Calls to the proxy's control API, and the profile scoping every one of them carries."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import click

import config
import ownership
import ui

CONTROL = config.CONTROL_ORIGIN
_PROFILE_HEADER = "X-Lyrebird-Profile"  # says which profile this call means; see `_profile_mismatch`
_HEALTH_PATH = "/__mock__/health"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect.

    A control port that answers 302 to an unreachable address is a port that *answers*; followed,
    the redirect fails and the whole request reads as "nothing there", and the sweep would switch
    off the PAC of a live session — see test_fetch_does_not_follow_a_redirect_to_an_unreachable_address.
    """

    def redirect_request(self, req: Any, fp: Any, code: int, msg: Any, headers: Any, newurl: str) -> None:
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    """The one transport. `ProxyHandler({})` because a `http_proxy` in the environment (or a macOS
    proxy setting — ours, even) must not route a loopback control request through a proxy whose
    failure would read as "the port is silent" — see test_fetch_ignores_a_configured_http_proxy."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


_OPENER = _build_opener()


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """Every request in this module goes through here — which is also the seam the test suite
    guards, so no test can reach a real control port by accident."""
    return _OPENER.open(request, timeout=timeout)


@dataclass(frozen=True, slots=True)
class Fetched:
    """An HTTP response arrived. `body` is None when the headers came back and the body did not:
    something answered either way."""

    status: int
    body: bytes | None


@dataclass(frozen=True, slots=True)
class Unreachable:
    """No HTTP response at all — a connect or a timeout before any headers."""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class HealthObservation:
    """One reading, two views: what the decision functions consult, and the payload to print.

    They come from the same fetch on purpose: a status taken from one reading and fields printed
    from another can disagree about which proxy answered.
    """

    health: ownership.Health
    raw: dict | None


def _origin(port: int | None) -> str:
    return CONTROL if port is None else f"http://{config.CONTROL_HOST}:{port}"


def _host_header(port: int | None) -> str:
    """Both the URL and the Host header follow the port being asked about: the control guard answers
    421 to any other Host, so a request to the journal's port carrying this process's configured
    header would read as unreachable — see test_health_on_a_port_answers_a_real_control_server."""
    return config.CONTROL_HOST_HEADER if port is None else f"{config.CONTROL_HOST}:{port}"


def _fetch(path: str, port: int | None = None, timeout: float = 1.5) -> Fetched | Unreachable:
    """The transport beneath every health reading: HTTP success kept separate from the decoded body.

    `json.loads(body)` alone cannot tell an HTTP 200 whose body is the literal `null` from a
    connection failure, and a sweep reading that as silence would switch off the PAC of a port that
    answers (test_observe_health_parses_exactly).
    """
    request = urllib.request.Request(
        f"{_origin(port)}{path}",
        headers={"Host": _host_header(port), _PROFILE_HEADER: config.PROFILE_FINGERPRINT},
    )
    try:
        response = _open(request, timeout)
    except urllib.error.HTTPError as error:
        # A status is an answer: a listener that says 500, 421 or 409 is still a listener.
        try:
            body: bytes | None = error.read()
        except (OSError, http.client.HTTPException):
            body = None
        finally:
            error.close()
        return Fetched(error.code, body)
    except (OSError, http.client.HTTPException) as error:
        return Unreachable(str(error))
    status = response.status
    try:
        with response:
            return Fetched(status, response.read())
    except (OSError, http.client.HTTPException):
        return Fetched(status, None)


def _json_object(body: bytes | None) -> dict | None:
    """The body as a JSON object, or None when it is not one — `null`, a list, a string, unreadable
    or not JSON at all. None is "nothing usable here", which an empty object is not."""
    if body is None:
        return None
    try:
        parsed = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _pid_of(data: dict) -> int | None:
    pid = data.get("pid")
    # `bool` is an `int`: `True` read as pid 1 would make a stranger's answer look like the
    # recorded proxy — see test_observe_health_parses_exactly.
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return None
    return pid


def _string_field(data: dict, *names: str) -> str | None:
    for name in names:
        value = data.get(name)
        if isinstance(value, str):
            return value
    return None


def decode_health(fetched: Fetched | Unreachable) -> HealthObservation:
    """Turn one fetch into both views.

    `Silent` only for `Unreachable`. *Every* HTTP response is `Answering` — a 409 profile mismatch,
    a 421, a 404, a 500, a body that is not JSON: something holds the port, and treating any of
    those as silence is how a sweep switches off a live session's PAC.
    """
    if isinstance(fetched, Unreachable):
        return HealthObservation(ownership.Silent(), None)
    parsed = _json_object(fetched.body)
    data = parsed if parsed is not None else {}
    health = ownership.Answering(
        pid=_pid_of(data),
        # `running` is what a 409 body calls the fingerprint it is refusing on behalf of.
        fingerprint=_string_field(data, "profileFingerprint", "running"),
        journal_error=_string_field(data, "journalError"),
    )
    return HealthObservation(health, parsed if 200 <= fetched.status < 300 else None)


def observe_health(port: int | None = None) -> HealthObservation:
    return decode_health(_fetch(_HEALTH_PATH, port))


def _profile_mismatch(running: str) -> str:
    """The one sentence for "the port is held by someone else's proxy", wherever we learn it.

    It names both fingerprints: "a different profile" without them leaves the operator no way to
    tell which one they are looking at.

    It does *not* offer another control port any more. There is one PAC-owning session per user, so
    `LYREBIRD_CONTROL_PORT=…` only changes the owner this run would request while the journal that
    refused it stays exactly where it is — and the next `up` refuses again, with the operator now
    believing the port was the problem. `lyrebird down` first is the remedy that works.
    """
    return (
        f"{ui.RED}a different profile is already running on port {config.CONTROL_PORT}{ui.R}\n"
        f"  running: {running}   requested: {config.PROFILE_FINGERPRINT}\n"
        f"  stop it first (`lyrebird down`), then start this profile — or point at the profile\n"
        f"  that is running with --profile."
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
        with _open(request, timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        # A mismatch is an answer, not an outage: returning None would point at a dead port.
        _refuse_a_foreign_profile(error, _error_body(error), unproven_exit)
        return None
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        # 'not reachable', which is the answer. Named rather than a bare `except Exception`: that
        # also swallowed the test suite's transport guard, so a test that reached the network
        # passed quietly — see test_an_unstubbed_health_observation_fails_before_any_network_access.
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


def _health(port: int | None = None, *, unproven_exit: int = 1) -> dict | None:
    """The raw payload, for the callers that read scenario fields out of it. None means "nothing
    usable answered"; `observe_health` is the reading that can tell those two apart.

    One fetch: the 409 refusal needs the *status*, which a `HealthObservation` does not carry — a
    mismatch and a 500 carrying the same fingerprint decode to the same `Answering` — so it is
    applied to the `Fetched` and the payload comes from that same response
    (test_health_compat_reader_refuses_a_409_mismatch_but_not_a_500).
    """
    fetched = _fetch(_HEALTH_PATH, port)
    if isinstance(fetched, Fetched) and fetched.status == 409:
        body = _json_object(fetched.body) or {}
        if body.get("error") == "profile_mismatch":
            running = body.get("running")
            click.echo(_profile_mismatch(running if isinstance(running, str) else "unknown"), err=True)
            raise SystemExit(unproven_exit)
    return decode_health(fetched).raw


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
        with _open(request, timeout) as response:
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
