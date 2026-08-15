"""Embedded control server: admin API + dashboard + PAC file.

Runs on the mitmproxy asyncio loop (started from addon.running), so it shares the Store instance
with the interception hooks without any cross-process contract.

The API is unauthenticated but loopback-only, which alone is not enough: a web page you visit can
send cross-origin requests to 127.0.0.1, and a DNS-rebinding attack can make a hostile origin
*look* same-origin. Three cheap checks close that (see `_guard`): the Host header must be one of
ours, a cross-origin Origin is refused, and any mutating request with a body must declare JSON —
aiohttp's `request.json()` ignores Content-Type, so without that last check a `text/plain` form
post would reach the API with no CORS preflight.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Handler

import config
from store import Store, UnsafeName

_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")


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

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def _mime_for(path: Path) -> str:
    return {
        ".html": "text/html",
        ".js": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
    }.get(path.suffix, "application/octet-stream")


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

    # MARK: - Dashboard + PAC

    def _serve_file(name: str) -> web.StreamResponse:
        path = config.WEB_DIR / name
        if not path.is_file():
            return web.Response(status=404, text=f"{name} not found")
        return web.Response(body=path.read_bytes(), content_type=_mime_for(path), charset="utf-8")

    @routes.get("/")
    async def index(_request: web.Request) -> web.StreamResponse:
        return _serve_file("index.html")

    @routes.get("/app.js")
    async def app_js(_request: web.Request) -> web.StreamResponse:
        return _serve_file("app.js")

    @routes.get("/styles.css")
    async def styles(_request: web.Request) -> web.StreamResponse:
        return _serve_file("styles.css")

    @routes.get("/proxy.pac")
    async def pac(_request: web.Request) -> web.StreamResponse:
        return web.Response(text=config.pac_contents(), content_type="application/x-ns-proxy-autoconfig")

    # MARK: - Health / status

    @routes.get("/__mock__/health")
    @routes.get("/__mock__/status")
    async def health(_request: web.Request) -> web.StreamResponse:
        return web.json_response({
            "ok": True,
            "activeSession": store.active_name,
            "overrideCount": len(store.active_overrides()),
            "sessions": list(store.sessions.keys()),
            **meta_provider(),
        })

    @routes.get("/__mock__/recent")
    async def recent(_request: web.Request) -> web.StreamResponse:
        return web.json_response(store.recent_list())

    @routes.get("/__mock__/catalog")
    async def catalog(_request: web.Request) -> web.StreamResponse:
        if config.CATALOG_FILE.is_file():
            return web.json_response(json.loads(config.CATALOG_FILE.read_text(encoding="utf-8")))
        return web.json_response([])

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

    @routes.delete("/__mock__/overrides/{id}")
    async def overrides_remove(request: web.Request) -> web.StreamResponse:
        if not store.remove_override(request.match_info["id"]):
            return web.json_response({"error": "unknown_override", "id": request.match_info["id"]},
                                     status=404)
        return web.json_response({"removed": request.match_info["id"]})

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

    @routes.post("/__mock__/sessions/save-active")
    async def sessions_save_active(request: web.Request) -> web.StreamResponse:
        body = await _safe_json(request)
        if not body.get("name"):
            return web.json_response({"error": "name_required"}, status=400)
        store.save_active_as(body["name"], body.get("notes", ""), body.get("verified", False))
        return web.json_response({"saved": body["name"]})

    @routes.put("/__mock__/sessions/active")
    async def sessions_activate(request: web.Request) -> web.StreamResponse:
        body = await _safe_json(request)
        name = body.get("name")
        if not isinstance(name, str) or not name:
            return web.json_response({"error": "name_required"}, status=400)
        previous = store.set_active(name)
        if previous is None:
            return web.json_response({"error": "unknown_session", "name": name}, status=404)
        return web.json_response({"active": store.active_name, "previous": previous})

    @routes.get("/__mock__/sessions/{name}/export")
    async def sessions_export(request: web.Request) -> web.StreamResponse:
        session = store.export_session(request.match_info["name"])
        if session is None:
            return web.json_response({"error": "unknown_session"}, status=404)
        return web.json_response(session)

    @routes.post("/__mock__/sessions/import")
    async def sessions_import(request: web.Request) -> web.StreamResponse:
        name = store.import_session(await _safe_json(request))
        if name is None:
            return web.json_response({"error": "session_name_required"}, status=400)
        return web.json_response({"imported": name})

    @routes.delete("/__mock__/sessions/{name}")
    async def sessions_delete(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if not store.delete_session(name):
            return web.json_response({"error": "cannot_delete", "name": name}, status=400)
        return web.json_response({"deleted": name})

    # MARK: - Presets

    @routes.get("/__mock__/presets/{operationId}")
    async def presets_list(request: web.Request) -> web.StreamResponse:
        return web.json_response(store.list_presets(request.match_info["operationId"]))

    @routes.get("/__mock__/presets/{operationId}/{name}")
    async def presets_get(request: web.Request) -> web.StreamResponse:
        body = store.get_preset(request.match_info["operationId"], request.match_info["name"])
        if body is None:
            return web.json_response({"error": "unknown_preset"}, status=404)
        return web.json_response(body)

    @routes.post("/__mock__/presets/{operationId}/{name}")
    async def presets_save(request: web.Request) -> web.StreamResponse:
        store.save_preset(request.match_info["operationId"], request.match_info["name"], await _safe_json(request))
        return web.json_response({"saved": f"{request.match_info['operationId']}/{request.match_info['name']}"})

    app.add_routes(routes)
    return app


async def start(store: Store, meta_provider: Callable[[], dict[str, Any]]) -> web.AppRunner:
    app = make_app(store, meta_provider)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.CONTROL_HOST, config.CONTROL_PORT)
    await site.start()
    return runner
