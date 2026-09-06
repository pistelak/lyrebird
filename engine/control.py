"""Embedded control server: admin API + PAC file.

Runs on the mitmproxy asyncio loop (started from addon.running), so it shares the Store instance
with the interception hooks without any cross-process contract.

The API is unauthenticated but loopback-only, which alone is not enough: a web page you visit can
send cross-origin requests to 127.0.0.1, and a DNS-rebinding attack can make a hostile origin
*look* same-origin. Three cheap checks close that (see `_guard`): the Host header must be one of
ours, a cross-origin Origin is refused, and any mutating request with a body must declare JSON —
aiohttp's `request.json()` ignores Content-Type, so without that last check a `text/plain` form
post would reach the API with no CORS preflight.

`_guard` also refuses a request that names a profile other than the running one (409). That is not
a security check — it is scoping, and it stops a CLI pointed at profile B from quietly mutating
profile A, which holds the control port.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Handler

import config
from store import Store, UnsafeName

_CSP = "default-src 'none'; frame-ancestors 'none'"
_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# The header a caller uses to say which profile it means, and the two routes that must answer
# whoever asks. `/__mock__/health` is the discovery endpoint — it is how `down` finds out that some
# *other* profile's proxy holds the port, and that cross-profile recovery is the whole reason `down`
# works after a crash. `/proxy.pac` is fetched by macOS, which knows nothing about profiles.
_PROFILE_HEADER = "X-Lyrebird-Profile"
_UNSCOPED_PATHS = frozenset({"/__mock__/health", "/proxy.pac"})


def _allowed_hosts() -> set[str]:
    return {
        config.CONTROL_HOST_HEADER,
        f"localhost:{config.CONTROL_PORT}",
        f"[::1]:{config.CONTROL_PORT}",
    }


def _allowed_origins() -> set[str]:
    return {f"http://{host}" for host in _allowed_hosts()}


@web.middleware
async def _guard(request: web.Request, handler: Handler) -> web.StreamResponse:
    if (request.headers.get("Host") or "").lower() not in _allowed_hosts():
        return web.json_response({"error": "bad_host"}, status=421)

    origin = request.headers.get("Origin")
    if origin and origin.lower() not in _allowed_origins():
        return web.json_response({"error": "cross_origin_denied"}, status=403)

    # One proxy holds the control port, so a CLI invoked with `--profile B` while profile A is
    # running would otherwise switch A's session, add rules to A and reset A's counters — every
    # call reporting success for work done somewhere the operator was not looking. The header is a
    # scoping declaration, not a credential: it is absent from older CLIs, the menu bar and curl,
    # and those stay unchecked. Fingerprints only — the profile path belongs to whoever wrote it.
    # `is not None`, not truthiness: an empty header is a caller that said *something* and named
    # nobody, and reading it as absent let `X-Lyrebird-Profile:` sail past the check.
    requested = request.headers.get(_PROFILE_HEADER)
    if (requested is not None and request.path not in _UNSCOPED_PATHS
            and requested != config.PROFILE_FINGERPRINT):
        return web.json_response({"error": "profile_mismatch",
                                  "running": config.PROFILE_FINGERPRINT,
                                  "requested": requested}, status=409)

    if request.method in _BODY_METHODS and request.can_read_body:
        content_type = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return web.json_response({"error": "json_content_type_required"}, status=415)

    try:
        response = await handler(request)
    except UnsafeName as error:
        return web.json_response({"error": "invalid_name", "detail": str(error)}, status=400)
    except ValueError as error:  # rules.ValidationError and friends
        return web.json_response({"error": "invalid_payload", "detail": str(error)}, status=400)
    except OSError as error:
        # The store could not write the profile — a full disk, a read-only profile. It publishes
        # nothing it could not write, so the caller's change simply did not happen and a retry is
        # safe. Named and detailed because aiohttp's own 500 is a bare text/plain body that sends
        # the operator to the proxy log instead of to the disk that is full.
        return web.json_response({"error": "persist_failed", "detail": str(error)}, status=500)

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


async def _safe_json(request: web.Request) -> dict:
    """Raises on a body that is present but unusable — `_guard` turns that into a 400.

    Returning {} instead would report the *next* problem it causes ("name_required") rather than
    the real one, sending the caller to look in the wrong place.
    """
    if not request.can_read_body:
        return {}
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise ValueError("malformed JSON body") from None
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    return payload


def make_app(store: Store, meta_provider: Callable[[], dict[str, Any]]) -> web.Application:
    app = web.Application(middlewares=[_guard])
    routes = web.RouteTableDef()

    # MARK: - PAC

    @routes.get("/proxy.pac")
    async def pac(_request: web.Request) -> web.StreamResponse:
        return web.Response(text=config.pac_contents(), content_type="application/x-ns-proxy-autoconfig")

    # MARK: - Health / status

    @routes.get("/__mock__/health")
    async def health(_request: web.Request) -> web.StreamResponse:
        return web.json_response({
            "ok": True,
            "activeSession": store.active_name,
            "overrideCount": len(store.active_overrides()),
            "sessions": list(store.sessions.keys()),
            # What did *not* load, so a caller can tell "this session is here" from "this session
            # is here whole". A session whose invalid overrides were dropped, and a malformed
            # default.json replaced by an empty in-memory default, both appear in `sessions`
            # looking exactly like a session that loaded — which is how `up --use NAME` would
            # relaunch the app against a scenario that had quietly lost half its rules.
            #
            # Two fields for one set of facts, because they answer different questions.
            # `loadProblems` is the flat list a person reads, one entry per problem. `up --use`
            # asks a narrower one — did *this* session load whole — and cannot answer it from
            # those strings: a file named `orders-outage.json: backup.json` produces a line that
            # reads exactly like a problem with `orders-outage`. So the same problems are also
            # sent keyed by the session they belong to.
            "loadProblems": store.load_problems,
            "sessionsNotWhole": store.sessions_not_whole,
            "sequences": store.sequence_states(),
            # Named for what it holds, not for the objects it describes: `overrides` would read as
            # the rules themselves, which is what GET /overrides returns.
            "answers": store.answer_states(),
            **meta_provider(),
        })

    @routes.get("/__mock__/recent")
    async def recent(_request: web.Request) -> web.StreamResponse:
        return web.json_response(store.recent_list())

    # MARK: - Overrides (act on the active session)

    @routes.get("/__mock__/overrides")
    async def overrides_list(_request: web.Request) -> web.StreamResponse:
        return web.json_response(store.active_overrides())

    @routes.post("/__mock__/overrides")
    async def overrides_add(request: web.Request) -> web.StreamResponse:
        override = store.add_override(await _safe_json(request))
        return web.json_response({"id": override["id"], "active": override["active"]})

    @routes.delete("/__mock__/overrides")
    async def overrides_clear(_request: web.Request) -> web.StreamResponse:
        """Deletes every override in the active session and rewrites its file."""
        cleared = len(store.active_overrides())
        store.clear_overrides()
        return web.json_response({"cleared": cleared, "session": store.active_name})

    # MARK: - Run state

    @routes.post("/__mock__/reset")
    async def runtime_reset(request: web.Request) -> web.StreamResponse:
        """Start a fresh run in the active session: rewind sequence cursors and clear answer counts.

        No body resets every rule; {"id": ...} resets one.
        """
        body = await _safe_json(request)
        override_id = body.get("id")
        if override_id is not None and (not isinstance(override_id, str) or not override_id):
            # Checked here rather than left to the store: a non-string would reach a dict lookup and
            # raise TypeError, which `_guard` does not translate — it catches ValueError — so a
            # caller sending {"id": []} would get a 500 describing nothing.
            return web.json_response({"error": "id_must_be_a_non_empty_string"}, status=400)
        result = store.reset_runtime(override_id)
        if result is None:
            return web.json_response({"error": "unknown_override", "id": override_id}, status=404)
        return web.json_response(result)

    # MARK: - Sessions

    @routes.get("/__mock__/sessions")
    async def sessions_list(_request: web.Request) -> web.StreamResponse:
        return web.json_response(store.list_sessions())

    @routes.post("/__mock__/sessions")
    async def sessions_create(request: web.Request) -> web.StreamResponse:
        body = await _safe_json(request)
        if not body.get("name"):
            return web.json_response({"error": "name_required"}, status=400)
        try:
            store.create_session(body["name"], body.get("cloneFrom"))
        except KeyError as error:
            return web.json_response({"error": "unknown_session", "cloneFrom": str(error)}, status=404)
        except FileExistsError:
            return web.json_response({"error": "session_exists", "name": body["name"]}, status=409)
        return web.json_response({"created": body["name"]})

    @routes.put("/__mock__/sessions/active")
    async def sessions_activate(request: web.Request) -> web.StreamResponse:
        body = await _safe_json(request)
        name = body.get("name")
        if not isinstance(name, str) or not name:
            return web.json_response({"error": "name_required"}, status=400)
        previous = store.set_active(name)
        if previous is None:
            # `detail` as well as the slug: the CLI prints `detail` when there is one, and
            # "unknown_session" on its own names the category without naming the mistake.
            return web.json_response({"error": "unknown_session", "name": name,
                                      "detail": f"no session named '{name}' in this profile"},
                                     status=404)
        return web.json_response({"active": store.active_name, "previous": previous})

    @routes.delete("/__mock__/sessions/{name}")
    async def sessions_delete(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if not store.delete_session(name):
            return web.json_response({"error": "cannot_delete", "name": name}, status=400)
        return web.json_response({"deleted": name})

    app.add_routes(routes)
    return app


async def start(store: Store, meta_provider: Callable[[], dict[str, Any]]) -> web.AppRunner:
    app = make_app(store, meta_provider)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.CONTROL_HOST, config.CONTROL_PORT)
    await site.start()
    return runner
