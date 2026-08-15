"""mitmproxy addon: applies the active session's overrides on the wire and hosts the control server.

Loaded via `mitmdump -s addon.py`. Only the hosts configured in the active profile are intercepted
(mitmproxy `allow_hosts`); everything else is blind-tunnelled. Server-sent-event streams always
pass through un-buffered, and mitmproxy's own `stream_large_bodies` cap streams anything bigger
than MAX_BUFFER_BYTES, so a long-poll or a large download never stalls behind us.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime

from mitmproxy import ctx, http

import config
import netproxy
import rules
from store import Store

# mitmproxy's ctx.log is deprecated (it warns and delegates); mitmproxy 12 routes stdlib logging
# into its own event log, so this reaches the same place without polluting the log with warnings.
_log = logging.getLogger("lyrebird")

BODYLESS_STATUSES = (204, 304)   # must not carry a body or a Content-Length
MAX_BUFFER_BYTES = 512 * 1024
# mitmproxy's stream_large_bodies option is a size *string* ("Understands k/m/g"), not an int.
STREAM_LARGE_BODIES = "512k"


def proxy_options() -> dict:
    """The mitmproxy options Lyrebird sets at startup.

    Separated so it can be type-checked against mitmproxy's own option manager in tests: these are
    applied inside the `running` hook, where a wrong type kills the proxy at startup rather than
    failing anywhere a unit test would normally look.
    """
    return {
        "allow_hosts": config.allow_hosts_regexes(),
        # Bodies above the cap stream instead of buffering; this is the mechanism that keeps
        # "buffer so we can patch" from becoming "buffer without limit".
        "stream_large_bodies": STREAM_LARGE_BODIES,
        # mitmproxy defaults to "eager", opening the upstream connection before our request hook
        # runs. A `replace` override never needs the upstream, but under eager the whole flow still
        # fails with 502 when the host is unreachable or does not resolve — so mocking an endpoint
        # that does not exist yet, or working offline/off-VPN, would not work at all.
        "connection_strategy": "lazy",
    }


class Lyrebird:
    def __init__(self) -> None:
        self.store = Store()
        self.started_at = datetime.now(UTC).isoformat()
        self._control_started = False

    # MARK: - Lifecycle

    async def running(self) -> None:
        if self._control_started:
            return
        self._control_started = True
        ctx.options.update(**proxy_options())

        import control  # local import so `import addon` (tests) doesn't require aiohttp

        await control.start(self.store, self._meta)
        _log.info(
            "control http://%s:%s | proxy :%s | session '%s' (%d overrides)",
            config.CONTROL_HOST, config.CONTROL_PORT, config.PROXY_PORT,
            self.store.active_name, len(self.store.active_overrides()),
        )
        for problem in self.store.load_problems:
            _log.warning("%s", problem)

    def _meta(self) -> dict:
        service = self._service()
        intercepting = netproxy.intercepting(service)
        return {
            "proxyUp": True,
            "intercepting": intercepting,   # PAC enabled AND pointing at us — not merely "process alive"
            "pacEnabled": intercepting,
            "service": service,
            "proxyPort": config.PROXY_PORT,
            "controlPort": config.CONTROL_PORT,
            # The configured host list is deliberately NOT exposed: no client needs it, and it is
            # private data belonging to whoever wrote the profile.
            "profileFingerprint": config.PROFILE_FINGERPRINT,
            "simBundleId": config.PROFILE.sim_bundle_id,
            "pid": os.getpid(),
            "startedAt": self.started_at,
        }

    @staticmethod
    def _service() -> str | None:
        return config.read_runtime().get("service") or netproxy.active_service()

    # MARK: - Interception

    async def request(self, flow: http.HTTPFlow) -> None:
        if not config.is_intercepted_host(flow.request.pretty_host):
            return

        pathname = flow.request.path.split("?", 1)[0]
        query = dict(flow.request.query)
        body_text = flow.request.get_text(strict=False) or ""
        override = self.store.find_override(flow.request.method, pathname, query, body_text)
        if not override:
            return

        delay_ms = override.get("delayMs")
        if delay_ms:
            delay_ms = min(int(delay_ms), config.MAX_DELAY_MS)
            flow.metadata["mock_delay_ms"] = delay_ms  # await sleeps only this flow; other flows keep serving
            await asyncio.sleep(delay_ms / 1000)

        if override.get("mode") == "replace":
            status = int(override.get("status") or 200)
            body = override.get("body")
            bodyless = body is None or status in BODYLESS_STATUSES
            headers = self._headers_with_default_content_type(override.get("headers"), json_body=not bodyless)
            payload = b"" if bodyless else (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
            response = http.Response.make(status, payload, headers)
            if status in BODYLESS_STATUSES:
                response.headers.pop("content-length", None)  # bodyless statuses must not carry a body/length
            flow.response = response
            flow.metadata["mock_matched"] = override["id"]
        elif override.get("mode") == "patch":
            flow.metadata["mock_patch"] = override

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        response = flow.response
        if response is None:
            return

        content_type = response.headers.get("content-type", "").lower()
        pending = flow.metadata.get("mock_patch")

        # Only a semantic stream is force-streamed. Merely lacking a Content-Length (ordinary
        # HTTP/2, or chunked framing) does not mean the body is unbounded — mitmproxy's
        # stream_large_bodies cap handles the genuinely large ones.
        if "text/event-stream" in content_type:
            if pending:
                flow.metadata["mock_patch"] = None
                flow.metadata["mock_patch_skipped"] = "event_stream"
            response.stream = True
            return

        if pending and "json" not in content_type:
            flow.metadata["mock_patch"] = None
            flow.metadata["mock_patch_skipped"] = "not_json"

    def response(self, flow: http.HTTPFlow) -> None:
        if not config.is_intercepted_host(flow.request.pretty_host):
            return

        pending = flow.metadata.get("mock_patch")
        matched = flow.metadata.get("mock_matched")
        status = flow.response.status_code if flow.response else 0

        if pending and flow.response is not None:
            if self._apply_patch(flow.response, pending):
                matched = pending["id"]
                status = flow.response.status_code
            else:
                flow.metadata["mock_patch_skipped"] = "body_unavailable_or_not_json"

        self._record(flow, status, matched)

    # MARK: - Helpers

    @staticmethod
    def _headers_with_default_content_type(headers: dict | None, json_body: bool) -> dict:
        """Merge case-insensitively: a session that spells the header `Content-Type` must not end
        up emitting both that and a lowercase `content-type` on the wire."""
        result = dict(headers or {})
        if json_body and not any(key.lower() == "content-type" for key in result):
            result["Content-Type"] = "application/json"
        return result

    @staticmethod
    def _apply_patch(response: http.Response, override: dict) -> bool:
        try:
            response.decode()
            payload = json.loads(response.get_text(strict=False) or "null")
        except (ValueError, UnicodeDecodeError):
            return False
        if payload is None:
            return False

        status = override.get("status")
        if status and int(status) in BODYLESS_STATUSES:
            response.status_code = int(status)
            response.set_content(b"")
            response.headers.pop("content-length", None)
            response.headers.pop("content-type", None)
            return True

        merged = rules.deep_merge(payload, override.get("patch") or {}, override.get("patchStrategy"))
        response.set_text(json.dumps(merged))
        response.headers["content-type"] = "application/json"
        if status:
            response.status_code = int(status)
        return True

    def _record(self, flow: http.HTTPFlow, status: int, matched: str | None) -> None:
        pathname = flow.request.path.split("?", 1)[0]
        entry = {
            "time": datetime.now(UTC).isoformat(),
            "method": flow.request.method,
            "host": flow.request.pretty_host,
            "path": pathname,
            "status": status,
            "matched": matched,
        }
        delay_ms = flow.metadata.get("mock_delay_ms")
        if delay_ms:
            entry["delayMs"] = delay_ms
        skipped = flow.metadata.get("mock_patch_skipped")
        if skipped:
            entry["patchSkipped"] = skipped  # so a dropped patch never looks like "didn't match"


        self.store.record_recent(entry)


addons = [Lyrebird()]
