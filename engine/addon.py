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

        delay_ms = override.get("delayMs") if override else None
        if delay_ms:
            delay_ms = min(int(delay_ms), config.MAX_DELAY_MS)
            flow.metadata["mock_delay_ms"] = delay_ms  # await sleeps only this flow; other flows keep serving
            # Awaited *before* the step is chosen. Selection and the response it produces are then
            # one synchronous block, so nothing can interleave between picking a step and committing
            # it — which is why sequences need no generation or staleness tracking. A per-step delay
            # would break this, because you would have to pick the step first.
            await asyncio.sleep(delay_ms / 1000)
            # The rules can move while we sleep: replaced, removed, or the whole session switched.
            # Re-select so the answer comes from what is live when it is produced — the same instant
            # the cursor is read. Holding the rule we captured before the sleep would serve the old
            # definition and then advance the new one, so the replacement's first step never runs.
            # The new rule's delay is deliberately not applied again: one request waits once.
            override = self.store.find_override(flow.request.method, pathname, query, body_text)

        if override is not None:
            action, resolved, progress = self.store.resolve_override(override)
            if progress is not None:
                flow.metadata["mock_sequence"] = progress
            self._answer(flow, action, resolved, override)
            # Cursors move only after the response is chosen, so a request is answered from the
            # state it arrived in — you never see your own write reflected in its own response.
            self.store.bump_selected(override)

        # Unconditional, and deliberately outside the block above: a request that no override
        # answered — a DELETE going straight to the real backend — must still advance a rule that
        # is watching for it. That is the whole point of an explicit `advanceOn`.
        advanced = self.store.advance_matching(flow.request.method, pathname, query, body_text)
        if advanced:
            flow.metadata["mock_advanced"] = advanced

    def _answer(self, flow: http.HTTPFlow, action: str, resolved: dict | None, override: dict) -> None:
        if action == rules.PASS_THROUGH:
            return   # sequence exhausted and told to stand aside: the real upstream answers
        if action == rules.EXHAUSTED_ERROR:
            flow.response = self._exhausted_response(flow, override)
            flow.metadata["mock_matched"] = override["id"]   # an override did answer, with a 500
            return
        if resolved is None:
            return

        if resolved.get("mode") == "replace":
            status = int(resolved.get("status") or 200)
            body = resolved.get("body")
            bodyless = body is None or status in BODYLESS_STATUSES
            headers = self._headers_with_default_content_type(resolved.get("headers"), json_body=not bodyless)
            payload = b"" if bodyless else (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
            response = http.Response.make(status, payload, headers)
            if status in BODYLESS_STATUSES:
                response.headers.pop("content-length", None)  # bodyless statuses must not carry a body/length
            flow.response = response
            flow.metadata["mock_matched"] = resolved["id"]
        elif resolved.get("mode") == "patch":
            flow.metadata["mock_patch"] = resolved

    @staticmethod
    def _exhausted_response(flow: http.HTTPFlow, override: dict) -> http.Response:
        """The `error` exhaustion policy: say so loudly rather than serve a plausible answer.

        A repeat of the last step would let an unplanned extra request pass for a successful one,
        which is the failure mode this project exists to avoid. The body carries the numbers, so the
        first question anyone asks ("why a 500?") is answered by the 500 itself.
        """
        progress = flow.metadata.get("mock_sequence") or {}
        steps = len(rules.sequence_steps(override) or [])
        body = {"error": {
            "code": "SEQUENCE_EXHAUSTED",
            "message": (
                f"Lyrebird: sequence '{override['id']}' defines {steps} step(s) and has seen "
                f"{progress.get('advanceEvents', steps)} advance event(s). Set "
                f"sequence.onExhausted to 'repeatLast' or 'passThrough' if more requests are "
                f"expected."
            ),
        }}
        return http.Response.make(
            500, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}
        )

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

    def error(self, flow: http.HTTPFlow) -> None:
        """Record a sequence flow that failed before there was any response to record.

        `_record` is otherwise reachable only from `response()`, so a request that never got one
        leaves no trace. That matters most for an exhausted `passThrough`: it stands aside and sends
        the request to the real upstream — which is frequently the very thing that is down, since
        being down is why you were mocking it. Without this the overrun would be invisible in
        `/recent`, and the operator would be looking for a rule that appeared never to fire.

        Deliberately scoped to flows carrying sequence metadata. Recording *every* failed flow is a
        worthwhile change, but a separate one: it would alter what `/recent` means for everybody.
        """
        # `mock_advanced` counts too: a request no override answered can still have moved a
        # sequence, and if its upstream then fails the cursor has changed with nothing in /recent
        # to explain why.
        if not (flow.metadata.get("mock_sequence") or flow.metadata.get("mock_advanced")):
            return
        if not config.is_intercepted_host(flow.request.pretty_host):
            return
        # 0 is already how `response` reports "no status". `matched` is read from metadata the same
        # way `response` reads it: an override that set a response before the flow died (a client
        # that hung up mid-write) did answer, and recording None here would say nothing did.
        self._record(flow, 0, flow.metadata.get("mock_matched"))

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

        sequence = flow.metadata.get("mock_sequence")
        if sequence:
            # `sequenceId` is kept separate from `matched`, which means "an override answered this
            # request". An exhausted passThrough answers nothing, so it has no `matched` — and
            # `recent --matched` filters on that, which would hide the overrun from exactly the
            # command used to check the scenario.
            entry["sequenceId"] = sequence["sequenceId"]
            entry["runId"] = sequence["runId"]
            entry["selectedStep"] = sequence["selectedStep"]
            entry["stepCount"] = sequence["stepCount"]
            if sequence["overrun"]:
                entry["overrun"] = True
        advanced = flow.metadata.get("mock_advanced")
        if advanced:
            entry["advanced"] = advanced

        self.store.record_recent(entry)


addons = [Lyrebird()]
