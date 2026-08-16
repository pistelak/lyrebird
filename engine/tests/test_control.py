"""The control API's browser-facing boundary.

The API is unauthenticated and loopback-only. That alone does not protect it: any web page you
visit can send cross-origin requests to 127.0.0.1, and DNS rebinding can make a hostile origin
look same-origin. These tests pin the three checks that close that gap.
"""

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

import config
import control
import store


def call(profile, method, path, *, headers=None, json_body=None, raw_body=None, content_type=None):
    """One request against a fresh control app on a throwaway profile.

    TestClient is already an async context manager, so there is no start/close bookkeeping to
    repeat per test. Returns (status, headers, parsed-body-or-None).
    """
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    app = control.make_app(store.Store(), lambda: {"proxyUp": True})

    sent = {"Host": config.CONTROL_HOST_HEADER, **(headers or {})}
    body = raw_body if raw_body is not None else (json.dumps(json_body) if json_body is not None else None)
    if body is not None and content_type is not False:
        sent.setdefault("Content-Type", content_type or "application/json")

    async def main():
        async with TestClient(TestServer(app)) as client:
            response = await client.request(method, path, data=body, headers=sent)
            try:
                parsed = await response.json()
            except Exception:  # noqa: BLE001 — some responses are not JSON, which is fine here
                parsed = None
            return response.status, dict(response.headers), parsed

    return asyncio.run(main())


# MARK: - The browser-facing guard

def test_cross_origin_text_plain_post_is_refused(profile):
    """aiohttp's request.json() ignores Content-Type, so without this check a plain form post —
    which needs no CORS preflight — would reach the API."""
    status, _, _ = call(profile, "POST", "/__mock__/sessions",
                        json_body={"name": "evil"}, content_type="text/plain")
    assert status == 415


def test_cross_origin_json_post_is_refused(profile):
    status, _, _ = call(profile, "POST", "/__mock__/sessions", json_body={"name": "evil"},
                        headers={"Origin": "https://attacker.test"})
    assert status == 403


def test_unknown_host_header_is_refused(profile):
    """Blocks DNS rebinding: the attacker controls the hostname, not the Host we accept."""
    status, _, _ = call(profile, "GET", "/__mock__/health", headers={"Host": "attacker.test"})
    assert status == 421


def test_same_origin_json_post_is_allowed(profile):
    status, _, _ = call(profile, "POST", "/__mock__/sessions", json_body={"name": "scratch"},
                        headers={"Origin": f"http://{config.CONTROL_HOST_HEADER}"})
    assert status == 200


def test_bodyless_mutation_needs_no_content_type(profile):
    """The dashboard sends bodyless DELETE; the guard must not break it."""
    status, _, _ = call(profile, "DELETE", "/__mock__/overrides")
    assert status == 200


def test_security_headers_are_present(profile):
    _, headers, _ = call(profile, "GET", "/__mock__/health")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


# MARK: - Names that become paths

def test_traversal_in_a_session_name_is_rejected(profile):
    status, _, body = call(profile, "POST", "/__mock__/sessions", json_body={"name": "../../ESCAPED"})
    assert status == 400
    assert body["error"] == "invalid_name"
    assert not (profile.parent / "ESCAPED.json").exists()


def test_missing_session_name_is_a_bad_request(profile):
    status, _, body = call(profile, "PUT", "/__mock__/sessions/active", json_body={})
    assert status == 400 and body["error"] == "name_required"


def test_unknown_session_is_not_found(profile):
    status, _, body = call(profile, "PUT", "/__mock__/sessions/active", json_body={"name": "nope"})
    assert status == 404 and body["error"] == "unknown_session"


# MARK: - What /health may disclose

def test_health_does_not_leak_the_configured_host_list(profile):
    """The host list is private data belonging to whoever wrote the profile, and no client needs
    it — so no health field may carry it."""
    _, _, body = call(profile, "GET", "/__mock__/health")
    assert not [key for key in body if "host" in key.lower()]


# MARK: - Clearing overrides

def test_clearing_overrides_reports_what_it_deleted(profile):
    """So a client that calls it by mistake at least says so out loud."""
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    app = control.make_app(store.Store(), lambda: {"proxyUp": True})

    async def main():
        async with TestClient(TestServer(app)) as client:
            headers = {"Host": config.CONTROL_HOST_HEADER, "Content-Type": "application/json"}
            await client.post("/__mock__/overrides",
                              data=json.dumps({"mode": "replace", "match": {"path": "/a"}}),
                              headers=headers)
            response = await client.delete("/__mock__/overrides", headers=headers)
            return await response.json()

    assert asyncio.run(main()) == {"cleared": 1, "session": "default"}


def test_malformed_json_says_so(profile):
    """Returning {} instead reported the next problem it caused ("name_required") rather than the
    real one, sending the caller to look in the wrong place."""
    status, _, body = call(profile, "POST", "/__mock__/sessions", raw_body="{not json")
    assert status == 400
    assert "malformed" in body["detail"].lower()


def test_a_non_object_body_says_so(profile):
    status, _, body = call(profile, "POST", "/__mock__/sessions", raw_body="[1,2,3]")
    assert status == 400
    assert "object" in body["detail"].lower()


# MARK: - Sequences

SEQ_SESSION = {
    "name": "default",
    "overrides": [{"id": "ovr_seq", "mode": "replace", "match": {"path": "/api/items"},
                   "sequence": {"steps": [{"status": 201}, {"status": 202}]}}],
}


def seed(profile):
    """A session on disk, because `call` builds a fresh Store that loads from the profile."""
    (profile / "sessions").mkdir(parents=True, exist_ok=True)
    (profile / "sessions" / "default.json").write_text(json.dumps(SEQ_SESSION), encoding="utf-8")


def test_health_reports_live_sequence_state(profile):
    seed(profile)
    status, _, body = call(profile, "GET", "/__mock__/health")
    assert status == 200
    assert [state["id"] for state in body["sequences"]] == ["ovr_seq"]
    assert body["sequences"][0]["nextStep"] == 1


def test_health_reports_an_empty_list_when_nothing_is_sequenced(profile):
    status, _, body = call(profile, "GET", "/__mock__/health")
    assert body["sequences"] == []


def test_reset_reports_what_it_rewound(profile):
    seed(profile)
    status, _, body = call(profile, "POST", "/__mock__/sequences/reset", json_body={})
    assert status == 200
    assert list(body["reset"]) == ["ovr_seq"]


def test_resetting_an_unknown_sequence_is_a_404(profile):
    """Not a 200 with an empty result: a typo must not look like a successful rewind."""
    seed(profile)
    status, _, body = call(profile, "POST", "/__mock__/sequences/reset", json_body={"id": "nope"})
    assert status == 404
    assert body["error"] == "unknown_sequence"


def test_a_non_string_reset_id_is_a_bad_request_not_a_crash(profile):
    """A list would reach a dict lookup and raise TypeError, which `_guard` does not translate — it
    catches ValueError — so this would otherwise surface as a 500 describing nothing."""
    seed(profile)
    status, _, body = call(profile, "POST", "/__mock__/sequences/reset", json_body={"id": []})
    assert status == 400
    assert body["error"] == "id_must_be_a_non_empty_string"


def test_reset_is_still_behind_the_content_type_guard(profile):
    seed(profile)
    status, _, _ = call(profile, "POST", "/__mock__/sequences/reset",
                        raw_body="{}", content_type="text/plain")
    assert status == 415


def test_importing_a_session_that_cannot_be_kept_whole_is_refused(profile):
    """Answering 200 to a payload whose second override was discarded tells the caller their rule
    is installed when it is not."""
    status, _, body = call(profile, "POST", "/__mock__/sessions/import", json_body={
        "session": {"name": "imp", "overrides": [
            {"id": "dup", "mode": "replace", "match": {"path": "/a"}},
            {"id": "dup", "mode": "replace", "match": {"path": "/b"}}]}})
    assert status == 400
    assert "duplicate id" in body["detail"]


def test_importing_over_an_existing_session_is_a_conflict(profile):
    """AGENTS.md promises the refusal: importing a session that already exists must not silently
    replace it."""
    seed(profile)
    payload = {"session": {"name": "imp", "overrides": []}}
    status, _, _ = call(profile, "POST", "/__mock__/sessions/import", json_body=payload)
    assert status == 200
    status, _, body = call(profile, "POST", "/__mock__/sessions/import", json_body=payload)
    assert status == 409
    assert body["error"] == "session_exists"


def test_importing_overrides_that_are_not_a_list_is_refused(profile):
    """`normalise_session` used to swap a malformed `overrides` for [] without recording a
    problem, so the import persisted an empty session and answered 200."""
    seed(profile)
    status, _, body = call(profile, "POST", "/__mock__/sessions/import", json_body={
        "session": {"name": "imp", "overrides": {"keep": {"mode": "replace"}}}})
    assert status == 400
    assert "overrides must be a list" in body["detail"]
