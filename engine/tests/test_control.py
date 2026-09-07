"""The control API's browser-facing boundary.

The API is unauthenticated and loopback-only. That alone does not protect it: any web page you
visit can send cross-origin requests to 127.0.0.1, and DNS rebinding can make a hostile origin
look same-origin. These tests pin the three checks that close that gap.
"""

import asyncio
import errno
import json

from aiohttp.test_utils import TestClient, TestServer

import config
import control
import store


async def _meta():
    """The stand-in for `addon.Lyrebird._meta`. Async because the real one observes the network in
    a worker thread and therefore suspends — a sync double would hide every ordering bug that
    suspension can cause."""
    return {"proxyUp": True}


def call(profile, method, path, *, headers=None, json_body=None, raw_body=None, content_type=None, prepare=None):
    """One request against a fresh control app on a throwaway profile.

    TestClient is already an async context manager, so there is no start/close bookkeeping to
    repeat per test. Returns (status, headers, parsed-body-or-None).
    """
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    subject = store.Store()
    if prepare is not None:
        prepare(subject)  # the store is built in here, so a test that needs live state seeds it here
    app = control.make_app(subject, _meta)

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
    status, _, _ = call(profile, "POST", "/__mock__/sessions", json_body={"name": "evil"}, content_type="text/plain")
    assert status == 415


def test_cross_origin_json_post_is_refused(profile):
    status, _, _ = call(
        profile, "POST", "/__mock__/sessions", json_body={"name": "evil"}, headers={"Origin": "https://attacker.test"}
    )
    assert status == 403


def test_unknown_host_header_is_refused(profile):
    """Blocks DNS rebinding: the attacker controls the hostname, not the Host we accept."""
    status, _, _ = call(profile, "GET", "/__mock__/health", headers={"Host": "attacker.test"})
    assert status == 421


def test_same_origin_json_post_is_allowed(profile):
    status, _, _ = call(
        profile,
        "POST",
        "/__mock__/sessions",
        json_body={"name": "scratch"},
        headers={"Origin": f"http://{config.CONTROL_HOST_HEADER}"},
    )
    assert status == 200


def test_bodyless_mutation_needs_no_content_type(profile):
    """A bodyless DELETE carries no Content-Type; the guard must not demand one."""
    status, _, _ = call(profile, "DELETE", "/__mock__/overrides")
    assert status == 200


def test_security_headers_are_present(profile):
    """Nothing served here is a page any more, so the policy allows no resource loads at all —
    spelled out rather than read back from `control._CSP`, so loosening it fails this test."""
    _, headers, _ = call(profile, "GET", "/__mock__/health")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"


def test_the_control_server_serves_no_pages(profile):
    """The dashboard is gone; the root and its old assets must not come back as anything."""
    for path in ("/", "/app.js", "/styles.css"):
        status, _, _ = call(profile, "GET", path)
        assert status == 404, path


# MARK: - Which profile the call means
#
# One proxy holds the control port. A CLI run with `--profile B` while profile A is running used to
# reach A and be told everything worked, so the operator watched an unchanged profile B and looked
# for the bug in their rule. The header says which profile the caller means; the API refuses to act
# for any other one.

_FOREIGN = {"X-Lyrebird-Profile": "deadbeefcafe"}


def test_a_call_naming_the_running_profile_is_served(profile):
    status, _, _ = call(
        profile, "GET", "/__mock__/overrides", headers={"X-Lyrebird-Profile": config.PROFILE_FINGERPRINT}
    )
    assert status == 200


def test_a_call_naming_another_profile_is_refused_and_leaves_no_rule_behind(profile):
    """The failure this closes: `lyrebird --profile B override add …` printed the new rule's id
    while the rule went into profile A, which is what the port actually belongs to."""
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    app = control.make_app(store.Store(), _meta)

    async def main():
        async with TestClient(TestServer(app)) as client:
            added = await client.post(
                "/__mock__/overrides",
                data=json.dumps({"mode": "replace", "match": {"path": "/api/items"}, "status": 503}),
                headers={"Host": config.CONTROL_HOST_HEADER, "Content-Type": "application/json", **_FOREIGN},
            )
            listed = await client.get("/__mock__/overrides", headers={"Host": config.CONTROL_HOST_HEADER})
            return added.status, await added.json(), await listed.json()

    status, body, listed = asyncio.run(main())
    assert status == 409
    assert body["error"] == "profile_mismatch"
    assert body["running"] == config.PROFILE_FINGERPRINT
    assert body["requested"] == "deadbeefcafe"
    assert listed == [], "a refused rule must leave nothing behind"


def test_a_read_naming_another_profile_is_refused_too(profile):
    """A read answered from the wrong profile is not harmless: it reports a stranger's sessions and
    counters as yours, which is how you conclude a rule is missing that was never installed here."""
    status, _, body = call(profile, "GET", "/__mock__/overrides", headers=_FOREIGN)
    assert status == 409 and body["error"] == "profile_mismatch"


def test_a_bodyless_delete_naming_another_profile_is_refused(profile):
    """`DELETE /overrides` is the destructive one, and it carries no body — so it must not reach the
    handler through the gap left by a check that only looks at requests with one."""
    status, _, body = call(profile, "DELETE", "/__mock__/overrides", headers=_FOREIGN)
    assert status == 409 and body["error"] == "profile_mismatch"


def test_health_answers_a_call_naming_another_profile(profile):
    """Health is how a caller finds out *which* profile holds the port — `down` restores the network
    whatever is running, and scoping the one endpoint that reports the running profile would break
    exactly the recovery that matters after a crash."""
    status, _, body = call(profile, "GET", "/__mock__/health", headers=_FOREIGN)
    assert status == 200 and body["ok"] is True


def test_the_pac_answers_a_call_naming_another_profile(profile):
    """macOS fetches the PAC and knows nothing about profiles; a 409 here would take the Mac's
    routing down."""
    status, _, _ = call(profile, "GET", "/proxy.pac", headers=_FOREIGN)
    assert status == 200


def test_an_empty_profile_header_is_a_mismatch_not_an_absence(profile):
    """`X-Lyrebird-Profile:` with nothing after it named nobody, and a truthiness check read that as
    "no header" — so a bodyless DELETE reached the handler. Empty is a value, and it is not ours."""
    status, _, body = call(profile, "DELETE", "/__mock__/overrides", headers={"X-Lyrebird-Profile": ""})
    assert status == 409 and body["error"] == "profile_mismatch"


def test_the_profile_header_name_is_matched_case_insensitively(profile):
    """HTTP header names are case-insensitive; a client that lower-cases them must still be scoped,
    and one that names the running profile that way must still be served."""
    status, _, _ = call(
        profile, "GET", "/__mock__/overrides", headers={"x-lyrebird-profile": config.PROFILE_FINGERPRINT}
    )
    assert status == 200
    status, _, body = call(profile, "GET", "/__mock__/overrides", headers={"x-lyrebird-profile": "deadbeefcafe"})
    assert status == 409 and body["error"] == "profile_mismatch"


def test_a_call_that_names_no_profile_is_served(profile):
    """The header is a scoping declaration, not a credential: curl, the menu bar and an older CLI
    send nothing, and refusing them would break clients this change is not about."""
    status, _, _ = call(profile, "GET", "/__mock__/overrides")
    assert status == 200


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
    # The CLI prints `detail` when there is one and the slug otherwise, so without this the
    # operator is told "unknown_session" — the category, not the mistake.
    assert body["detail"] == "no session named 'nope' in this profile"


def test_health_reports_a_session_that_did_not_load_whole(profile):
    """`sessions` cannot carry this: a session whose invalid overrides were dropped is listed
    there exactly like one that loaded whole. `up --use NAME` refuses to relaunch the app on the
    strength of this, so it has to reach the CLI — keyed by session, and once per problem."""
    (profile / "sessions" / "orders-outage.json").write_text(
        json.dumps(
            {
                "name": "orders-outage",
                "overrides": [
                    {"match": {"method": "GET", "path": "/api/v1/orders/*"}, "mode": "replace", "status": 500},
                    {"match": {"method": "GET", "path": "/api/v1/x"}, "mode": "replace", "statsu": 500},
                    {"match": {"method": "GET", "path": "/api/v1/y"}, "mode": "replace", "sttaus": 500},
                ],
            }
        ),
        encoding="utf-8",
    )

    status, _, body = call(profile, "GET", "/__mock__/health")

    assert status == 200
    assert "orders-outage" in body["sessions"], "it loaded, which is exactly the trap"
    assert len(body["loadProblems"]) == 2, "one entry per problem, not per file"
    assert body["sessionsNotWhole"] == {"orders-outage": body["loadProblems"]}


def test_health_does_not_blame_a_session_for_a_file_merely_named_after_it(profile):
    """The reason the answer is keyed by session rather than read out of the strings. This file's
    name is rejected, so no session is reported against it — but the diagnostic it leaves behind
    begins exactly like one about `orders-outage`, whose own file is perfectly good."""
    good = {
        "name": "orders-outage",
        "overrides": [{"match": {"method": "GET", "path": "/api/v1/orders/*"}, "mode": "replace", "status": 500}],
    }
    (profile / "sessions" / "orders-outage.json").write_text(json.dumps(good), encoding="utf-8")
    (profile / "sessions" / "orders-outage.json: backup.json").write_text("{", encoding="utf-8")

    status, _, body = call(profile, "GET", "/__mock__/health")

    assert status == 200
    assert body["loadProblems"] and body["loadProblems"][0].startswith("skipped orders-outage.json: backup.json:"), (
        "the trap, verbatim"
    )
    assert body["sessionsNotWhole"] == {}, "orders-outage loaded whole and must not be blamed"


def test_health_forgets_a_session_once_a_good_one_is_created_over_it(profile):
    """The recovery path. A malformed `orders-outage.json` is how the entry gets there; creating
    a session under that name is what an operator does about it, and the point of doing it is
    that `up --use orders-outage` stops refusing. An entry left behind would go on refusing to
    launch the app over a file that no longer decides anything."""
    (profile / "sessions" / "orders-outage.json").write_text("{ not json", encoding="utf-8")

    def create_a_good_one(subject):
        assert subject.sessions_not_whole["orders-outage"], "the malformed file was recorded"
        subject.create_session("orders-outage")

    status, _, body = call(profile, "GET", "/__mock__/health", prepare=create_a_good_one)

    assert status == 200
    # Empty is what the CLI reads: `up --use orders-outage` finds no problems and goes on to relaunch.
    assert body["sessionsNotWhole"] == {}
    assert "orders-outage" in body["sessions"]
    assert body["loadProblems"], "what startup found stays on the record; it just no longer decides"


def test_health_reports_no_load_problems_when_every_session_loaded(profile):
    """The other half: empty is a real answer, and the CLI treats it as one."""
    status, _, body = call(profile, "GET", "/__mock__/health")
    assert status == 200
    assert body["loadProblems"] == [] and body["sessionsNotWhole"] == {}


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
    app = control.make_app(store.Store(), _meta)

    async def main():
        async with TestClient(TestServer(app)) as client:
            headers = {"Host": config.CONTROL_HOST_HEADER, "Content-Type": "application/json"}
            await client.post(
                "/__mock__/overrides", data=json.dumps({"mode": "replace", "match": {"path": "/a"}}), headers=headers
            )
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
    "overrides": [
        {
            "id": "ovr_seq",
            "mode": "replace",
            "match": {"path": "/api/items"},
            "sequence": {"steps": [{"status": 201}, {"status": 202}]},
        }
    ],
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


OTHER_SESSION = {
    "name": "other",
    "overrides": [
        {
            "id": "ovr_other",
            "mode": "replace",
            "match": {"path": "/api/other"},
            "sequence": {"steps": [{"status": 204}]},
        }
    ],
}


def test_health_reads_the_store_after_the_meta_await_and_not_across_it(profile):
    """`meta_provider` suspends — it observes the network off this loop — so a session switch can
    land inside that await. Reading the store first paired the outgoing session's counters with
    the incoming session's meta: a snapshot describing no moment that ever existed."""
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    (profile / "sessions").mkdir(parents=True, exist_ok=True)
    (profile / "sessions" / "default.json").write_text(json.dumps(SEQ_SESSION), encoding="utf-8")
    (profile / "sessions" / "other.json").write_text(json.dumps(OTHER_SESSION), encoding="utf-8")
    config.reload_profile()
    subject = store.Store()

    async def switch_while_awaited():
        await asyncio.sleep(0)  # the suspension the real provider has
        subject.set_active("other")
        return {"proxyUp": True}

    app = control.make_app(subject, switch_while_awaited)

    async def main():
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/__mock__/health", headers={"Host": config.CONTROL_HOST_HEADER})
            return await response.json()

    body = asyncio.run(main())
    assert body["activeSession"] == "other"
    assert [state["id"] for state in body["sequences"]] == ["ovr_other"]
    assert [state["id"] for state in body["answers"]] == ["ovr_other"]


def test_reset_reports_what_it_rewound(profile):
    seed(profile)
    status, _, body = call(profile, "POST", "/__mock__/reset", json_body={})
    assert status == 200
    assert list(body["reset"]) == ["ovr_seq"]


def test_resetting_an_unknown_rule_is_a_404(profile):
    """Not a 200 with an empty result: a typo must not look like a successful rewind."""
    seed(profile)
    status, _, body = call(profile, "POST", "/__mock__/reset", json_body={"id": "nope"})
    assert status == 404
    assert body["error"] == "unknown_override"


def test_a_non_string_reset_id_is_a_bad_request_not_a_crash(profile):
    """A list would reach a dict lookup and raise TypeError, which `_guard` does not translate — it
    catches ValueError — so this would otherwise surface as a 500 describing nothing."""
    seed(profile)
    status, _, body = call(profile, "POST", "/__mock__/reset", json_body={"id": []})
    assert status == 400
    assert body["error"] == "id_must_be_a_non_empty_string"


def test_reset_is_still_behind_the_content_type_guard(profile):
    seed(profile)
    status, _, _ = call(profile, "POST", "/__mock__/reset", raw_body="{}", content_type="text/plain")
    assert status == 415


def test_creating_a_session_that_already_exists_is_a_conflict(profile):
    """AGENTS.md promises the refusal: creating a session that already exists must not silently
    replace it."""
    seed(profile)
    payload = {"name": "scratch"}
    status, _, _ = call(profile, "POST", "/__mock__/sessions", json_body=payload)
    assert status == 200
    status, _, body = call(profile, "POST", "/__mock__/sessions", json_body=payload)
    assert status == 409
    assert body["error"] == "session_exists"


def test_a_rule_with_an_unknown_field_is_refused_and_not_installed(profile):
    """A 200 here told the caller their 503 rule was live when the proxy would answer 200 — the
    typo'd field was kept, ignored, and never mentioned again."""
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    app = control.make_app(store.Store(), _meta)

    async def main():
        async with TestClient(TestServer(app)) as client:
            headers = {"Host": config.CONTROL_HOST_HEADER, "Content-Type": "application/json"}
            added = await client.post(
                "/__mock__/overrides",
                data=json.dumps({"mode": "replace", "match": {"path": "/api/items"}, "statsu": 503}),
                headers=headers,
            )
            listed = await client.get("/__mock__/overrides", headers=headers)
            return added.status, await added.json(), await listed.json()

    status, body, listed = asyncio.run(main())
    assert status == 400
    assert body["error"] == "invalid_payload"
    assert "'statsu'" in body["detail"]
    assert listed == [], "a refused rule must leave nothing behind"


# MARK: - Answer evidence


def test_health_reports_answer_counts_per_rule(profile):
    status, _, body = call(profile, "GET", "/__mock__/health")
    assert status == 200
    assert body["answers"] == [], "an empty session has no rules to report on"


def test_health_carries_a_rules_answer_count_over_the_wire(profile):
    """The store and addon tests prove the count is right; this proves it survives to the HTTP
    boundary, which is the only place `assert-answered` can read it from."""
    seed(profile)
    status, _, body = call(profile, "GET", "/__mock__/health", prepare=lambda s: store.credit(s.answer_slot("ovr_seq")))
    assert status == 200
    assert body["answers"] == [{"id": "ovr_seq", "active": True, "count": 1, "runId": body["sequences"][0]["runId"]}], (
        "with the run it was counted in, which is what binds an assertion to a reset boundary"
    )


def test_the_reset_route_stays_behind_the_guard(profile):
    """A new route is a new way in. `_guard` is global, and this pins that it stays that way."""
    status, _, body = call(profile, "POST", "/__mock__/reset", headers={"Host": "evil.example.com"}, json_body={})
    assert status == 421
    assert body["error"] == "bad_host"


# MARK: - A profile that cannot be written


def test_a_rule_whose_write_fails_is_reported_and_not_installed(profile, monkeypatch):
    """A bare aiohttp 500 says "Internal Server Error" and sends the operator to the proxy log
    rather than to the disk that is full. One client for both requests, on purpose: a fresh Store
    would reload from disk and hide a rule left live in memory."""
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    app = control.make_app(store.Store(), _meta)

    def refuse(path, text):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(config, "atomic_write", refuse)

    async def main():
        async with TestClient(TestServer(app)) as client:
            headers = {"Host": config.CONTROL_HOST_HEADER, "Content-Type": "application/json"}
            added = await client.post(
                "/__mock__/overrides",
                data=json.dumps({"id": "r", "mode": "replace", "match": {"path": "/a"}}),
                headers=headers,
            )
            listed = await client.get("/__mock__/overrides", headers={"Host": config.CONTROL_HOST_HEADER})
            return added.status, await added.json(), await listed.json()

    status, body, overrides = asyncio.run(main())
    assert status == 500
    assert body["error"] == "persist_failed"
    assert "No space left" in body["detail"]
    assert overrides == [], "the rule was published despite the write that failed"
