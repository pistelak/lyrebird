"""The control API's browser-facing boundary.

The API is unauthenticated and loopback-only. That alone does not protect it: any web page you
visit can send cross-origin requests to 127.0.0.1, and DNS rebinding can make a hostile origin
look same-origin. These tests pin the three checks that close that gap.
"""

import asyncio
import errno
import json

import pytest
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
    status, _, _ = call(profile, "POST", "/__mock__/scenarios", json_body={"name": "evil"}, content_type="text/plain")
    assert status == 415


def test_cross_origin_json_post_is_refused(profile):
    status, _, _ = call(
        profile, "POST", "/__mock__/scenarios", json_body={"name": "evil"}, headers={"Origin": "https://attacker.test"}
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
        "/__mock__/scenarios",
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
    """A read answered from the wrong profile is not harmless: it reports a stranger's scenarios and
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


def test_traversal_in_a_scenario_name_is_rejected(profile):
    status, _, body = call(profile, "POST", "/__mock__/scenarios", json_body={"name": "../../ESCAPED"})
    assert status == 400
    assert body["error"] == "invalid_name"
    assert not (profile.parent / "ESCAPED.json").exists()


def test_missing_scenario_name_is_a_bad_request(profile):
    status, _, body = call(profile, "PUT", "/__mock__/scenarios/active", json_body={})
    assert status == 400 and body["error"] == "name_required"


def test_unknown_scenario_is_not_found(profile):
    status, _, body = call(profile, "PUT", "/__mock__/scenarios/active", json_body={"name": "nope"})
    assert status == 404 and body["error"] == "unknown_scenario"
    # The CLI prints `detail` when there is one and the slug otherwise, so without this the
    # operator is told "unknown_scenario" — the category, not the mistake.
    assert body["detail"] == "no scenario named 'nope' in this profile"


def test_health_reports_a_scenario_that_did_not_load_whole(profile):
    """`scenarios` cannot carry this: a scenario whose invalid overrides were dropped is listed
    there exactly like one that loaded whole. `up --use NAME` refuses to relaunch the app on the
    strength of this, so it has to reach the CLI — keyed by scenario, and once per problem."""
    (profile / "scenarios" / "orders-outage.json").write_text(
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
    assert "orders-outage" in body["scenarios"], "it loaded, which is exactly the trap"
    assert len(body["loadProblems"]) == 2, "one entry per problem, not per file"
    assert body["scenariosNotWhole"] == {"orders-outage": body["loadProblems"]}


def test_health_does_not_blame_a_scenario_for_a_file_merely_named_after_it(profile):
    """The reason the answer is keyed by scenario rather than read out of the strings. This file's
    name is rejected, so no scenario is reported against it — but the diagnostic it leaves behind
    begins exactly like one about `orders-outage`, whose own file is perfectly good."""
    good = {
        "name": "orders-outage",
        "overrides": [{"match": {"method": "GET", "path": "/api/v1/orders/*"}, "mode": "replace", "status": 500}],
    }
    (profile / "scenarios" / "orders-outage.json").write_text(json.dumps(good), encoding="utf-8")
    (profile / "scenarios" / "orders-outage.json: backup.json").write_text("{", encoding="utf-8")

    status, _, body = call(profile, "GET", "/__mock__/health")

    assert status == 200
    assert body["loadProblems"] and body["loadProblems"][0].startswith("skipped orders-outage.json: backup.json:"), (
        "the trap, verbatim"
    )
    assert body["scenariosNotWhole"] == {}, "orders-outage loaded whole and must not be blamed"


def test_health_forgets_a_scenario_once_a_good_one_is_created_over_it(profile):
    """The recovery path. A malformed `orders-outage.json` is how the entry gets there; creating
    a scenario under that name is what an operator does about it, and the point of doing it is
    that `up --use orders-outage` stops refusing. An entry left behind would go on refusing to
    launch the app over a file that no longer decides anything."""
    (profile / "scenarios" / "orders-outage.json").write_text("{ not json", encoding="utf-8")

    def create_a_good_one(subject):
        assert subject.scenarios_not_whole["orders-outage"], "the malformed file was recorded"
        subject.create_scenario("orders-outage")

    status, _, body = call(profile, "GET", "/__mock__/health", prepare=create_a_good_one)

    assert status == 200
    # Empty is what the CLI reads: `up --use orders-outage` finds no problems and goes on to relaunch.
    assert body["scenariosNotWhole"] == {}
    assert "orders-outage" in body["scenarios"]
    assert body["loadProblems"], "what startup found stays on the record; it just no longer decides"


def test_health_reports_no_load_problems_when_every_scenario_loaded(profile):
    """The other half: empty is a real answer, and the CLI treats it as one."""
    status, _, body = call(profile, "GET", "/__mock__/health")
    assert status == 200
    assert body["loadProblems"] == [] and body["scenariosNotWhole"] == {}


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

    assert asyncio.run(main()) == {"cleared": 1, "scenario": "default"}


def test_malformed_json_says_so(profile):
    """Returning {} instead reported the next problem it caused ("name_required") rather than the
    real one, sending the caller to look in the wrong place."""
    status, _, body = call(profile, "POST", "/__mock__/scenarios", raw_body="{not json")
    assert status == 400
    assert "malformed" in body["detail"].lower()


def test_a_non_object_body_says_so(profile):
    status, _, body = call(profile, "POST", "/__mock__/scenarios", raw_body="[1,2,3]")
    assert status == 400
    assert "object" in body["detail"].lower()


# MARK: - Sequences

SEQ_SCENARIO = {
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
    """A scenario on disk, because `call` builds a fresh Store that loads from the profile."""
    (profile / "scenarios").mkdir(parents=True, exist_ok=True)
    (profile / "scenarios" / "default.json").write_text(json.dumps(SEQ_SCENARIO), encoding="utf-8")


def test_health_reports_live_sequence_state(profile):
    seed(profile)
    status, _, body = call(profile, "GET", "/__mock__/health")
    assert status == 200
    assert [state["id"] for state in body["sequences"]] == ["ovr_seq"]
    assert body["sequences"][0]["nextStep"] == 1


def test_health_reports_an_empty_list_when_nothing_is_sequenced(profile):
    status, _, body = call(profile, "GET", "/__mock__/health")
    assert body["sequences"] == []


OTHER_SCENARIO = {
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
    """`meta_provider` suspends — it observes the network off this loop — so a scenario switch can
    land inside that await. Reading the store first paired the outgoing scenario's counters with
    the incoming scenario's meta: a snapshot describing no moment that ever existed."""
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    (profile / "scenarios").mkdir(parents=True, exist_ok=True)
    (profile / "scenarios" / "default.json").write_text(json.dumps(SEQ_SCENARIO), encoding="utf-8")
    (profile / "scenarios" / "other.json").write_text(json.dumps(OTHER_SCENARIO), encoding="utf-8")
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
    assert body["activeScenario"] == "other"
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


def test_creating_a_scenario_that_already_exists_is_a_conflict(profile):
    """AGENTS.md promises the refusal: creating a scenario that already exists must not silently
    replace it."""
    seed(profile)
    payload = {"name": "scratch"}
    status, _, _ = call(profile, "POST", "/__mock__/scenarios", json_body=payload)
    assert status == 200
    status, _, body = call(profile, "POST", "/__mock__/scenarios", json_body=payload)
    assert status == 409
    assert body["error"] == "scenario_exists"


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
    assert body["answers"] == [], "an empty scenario has no rules to report on"


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


# MARK: - The rules snapshot
#
# One read that hands a client everything a rules window shows: the rules as stored, the engine's
# own description of what each answers with, and the runtime state each has. The point of the
# description travelling with the rule is that no client has to re-derive step inheritance, the
# exhaustion default or the wire encoding of a body — a client that did would drift, silently, and
# describe behaviour this engine no longer has.

RULES_SCENARIO = {
    "name": "default",
    "overrides": [
        {
            "id": "ovr_orders",
            "mode": "replace",
            "match": {"method": "GET", "path": "/api/v1/orders"},
            "status": 200,
            "body": {"orders": []},
        },
        {
            "id": "ovr_flags",
            "mode": "patch",
            "match": {"method": "GET", "path": "/api/v1/features"},
            "patch": {"checkout_v2": True},
        },
        {
            "id": "ovr_seq",
            "mode": "replace",
            "match": {"path": "/api/v1/items"},
            "sequence": {"steps": [{"status": 201}, {"status": 202}]},
        },
    ],
}


def seed_rules(profile):
    (profile / "scenarios").mkdir(parents=True, exist_ok=True)
    (profile / "scenarios" / "default.json").write_text(json.dumps(RULES_SCENARIO), encoding="utf-8")


def test_the_rules_route_stays_behind_the_guard(profile):
    """A new route is a new way in, and this one reads a whole scenario — every rule, every saved
    body. `_guard` is global; this pins that the route did not arrive outside it."""
    seed_rules(profile)
    status, _, body = call(profile, "GET", "/__mock__/rules", headers={"Host": "evil.example.com"})
    assert status == 421 and body["error"] == "bad_host"
    status, _, body = call(profile, "GET", "/__mock__/rules", headers={"Origin": "https://attacker.test"})
    assert status == 403 and body["error"] == "cross_origin_denied"
    status, _, body = call(profile, "GET", "/__mock__/rules", headers=_FOREIGN)
    assert status == 409 and body["error"] == "profile_mismatch"


def test_rules_lists_the_same_rules_in_the_same_order_as_the_overrides_listing(profile):
    """The two reads describe one list. A window showing rules in another order, or missing one,
    would send an operator looking for a rule by a position the proxy does not use."""
    seed_rules(profile)
    _, _, listed = call(profile, "GET", "/__mock__/overrides")
    status, _, body = call(profile, "GET", "/__mock__/rules")
    assert status == 200
    assert body["scenario"] == "default"
    assert [rule["id"] for rule in body["rules"]] == [override["id"] for override in listed]


def test_a_rules_row_carries_the_rule_exactly_as_stored(profile):
    """The row claims to hold the rule as written, so nothing added beside it may overwrite a field
    of it — `sequenceState` is spelled that way because `sequence` already holds the steps, and a
    row that dropped them would be a rules window unable to show what a sequence answers with."""
    seed_rules(profile)
    _, _, body = call(profile, "GET", "/__mock__/rules")
    rows = {rule["id"]: rule for rule in body["rules"]}
    assert rows["ovr_seq"]["sequence"] == RULES_SCENARIO["overrides"][2]["sequence"]
    assert rows["ovr_orders"]["body"] == {"orders": []}
    assert rows["ovr_flags"]["patch"] == {"checkout_v2": True}


def test_a_rules_row_describes_what_the_rule_answers_with(profile):
    """`rewrite` is the engine's description, not the client's reading of the schema. It is here so
    that a rules window can show 'json, 14 bytes' or 'step 2 of 2' without reimplementing either."""
    seed_rules(profile)
    _, _, body = call(profile, "GET", "/__mock__/rules")
    rows = {rule["id"]: rule for rule in body["rules"]}
    assert rows["ovr_orders"]["rewrite"]["bodyKind"] == "json"
    assert rows["ovr_flags"]["rewrite"]["patchKeys"] == 1
    sequence = rows["ovr_seq"]["rewrite"]["sequence"]
    assert [step["status"] for step in sequence["steps"]] == [201, 202]
    # Each step as the wire would answer it, over the wire: a pane composes nothing, and `inherited`
    # is what lets it say which of those values the step did not write itself.
    assert sequence["steps"][0] == {
        "status": 201,
        "headers": {},
        "body": None,
        "bodyKind": "none",
        "bodyBytes": None,
        "inherited": ["body", "headers"],
    }
    assert sequence["advanceOn"] is None, "this sequence advances on its own answer"


def test_a_rule_with_no_sequence_reports_no_sequence_state(profile):
    """Null, not an empty cursor: a plain rule has no step to be on, and a zeroed one would read as
    a sequence waiting at step 1."""
    seed_rules(profile)
    _, _, body = call(profile, "GET", "/__mock__/rules")
    rows = {rule["id"]: rule for rule in body["rules"]}
    assert rows["ovr_orders"]["sequenceState"] is None
    assert rows["ovr_flags"]["sequenceState"] is None
    assert rows["ovr_seq"]["sequenceState"]["nextStep"] == 1


def test_rules_and_health_report_the_same_runtime_state(profile):
    """One store, two reads, and they must agree — including the `runId`, which is what binds a
    count to the reset that started it. Separate readings of the same counters that could disagree
    would leave 'the window says 1, assert-answered says 0' with no way to tell which is right."""
    seed_rules(profile)
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    subject = store.Store()
    store.credit(subject.answer_slot("ovr_orders"))
    app = control.make_app(subject, _meta)

    async def main():
        async with TestClient(TestServer(app)) as client:
            headers = {"Host": config.CONTROL_HOST_HEADER}
            health = await (await client.get("/__mock__/health", headers=headers)).json()
            snapshot = await (await client.get("/__mock__/rules", headers=headers)).json()
            return health, snapshot

    health, snapshot = asyncio.run(main())
    assert [rule["answer"] for rule in snapshot["rules"]] == health["answers"]
    assert [rule["sequenceState"] for rule in snapshot["rules"] if rule["sequenceState"]] == health["sequences"]


def test_a_rules_row_reports_a_request_the_rule_answered(profile):
    """The count is the evidence that a mock was in play. It reaches the window from the same slot
    `assert-answered` reads, so a rule shown as never used is a rule that never answered."""
    seed_rules(profile)
    status, _, body = call(
        profile, "GET", "/__mock__/rules", prepare=lambda s: store.credit(s.answer_slot("ovr_orders"))
    )
    assert status == 200
    rows = {rule["id"]: rule for rule in body["rules"]}
    assert rows["ovr_orders"]["answer"]["count"] == 1
    assert rows["ovr_orders"]["answer"]["runId"], "the run it was counted in, not merely a number"
    assert rows["ovr_flags"]["answer"]["count"] == 0


def test_rules_lists_what_loaded_and_names_what_did_not(profile):
    """A rule dropped at load time leaves a window that looks complete: the rules that survived are
    all there, and nothing on the screen says one is missing. `notWhole` is that sentence."""
    (profile / "scenarios" / "default.json").write_text(
        json.dumps(
            {
                "name": "default",
                "overrides": [
                    {"id": "ovr_orders", "match": {"path": "/api/v1/orders"}, "mode": "replace", "status": 500},
                    {"id": "ovr_typo", "match": {"path": "/api/v1/items"}, "mode": "replace", "statsu": 500},
                ],
            }
        ),
        encoding="utf-8",
    )

    status, _, body = call(profile, "GET", "/__mock__/rules")

    assert status == 200
    assert [rule["id"] for rule in body["rules"]] == ["ovr_orders"], "what loaded is still served"
    assert len(body["notWhole"]) == 1
    assert "'statsu'" in body["notWhole"][0], "the problem itself, so the window can say which rule"


def test_rules_blames_no_scenario_for_a_file_merely_named_after_one(profile):
    """Why `notWhole` is read from the keyed map and never filtered out of the flat strings: this
    file's name is rejected, and the line it leaves begins exactly like a problem with `default`,
    whose own file is perfectly good."""
    seed_rules(profile)
    (profile / "scenarios" / "default.json: backup.json").write_text("{", encoding="utf-8")

    status, _, body = call(profile, "GET", "/__mock__/rules")

    assert status == 200
    assert body["notWhole"] == [], "default loaded whole and must not be blamed"
    assert len(body["rules"]) == 3


def test_rules_reports_no_problems_when_the_scenario_loaded_whole(profile):
    """The other half: empty is a real answer, and a window that greys itself out on any non-empty
    `notWhole` needs it to mean exactly nothing went wrong."""
    seed_rules(profile)
    status, _, body = call(profile, "GET", "/__mock__/rules")
    assert status == 200 and body["notWhole"] == []


def test_a_fresh_snapshot_gives_a_sequenced_rule_one_run_id(profile):
    """The first read of a store nobody has touched. `sequence_states` mints a rule's runtime slot
    where `answer_states` only reads one, so reading answers first reported `answer.runId: null`
    beside a live `sequenceState.runId` for the same rule — two run ids for one run, and a client
    binding a count to a boundary cannot tell which of them it drew.

    No health call and no credit first, deliberately: anything that touches the store beforehand
    creates the slot and hides this."""
    seed_rules(profile)
    status, _, body = call(profile, "GET", "/__mock__/rules")

    assert status == 200
    sequenced = [rule for rule in body["rules"] if rule["sequenceState"]]
    assert sequenced, "the scenario has a sequenced rule; without one this proves nothing"
    for rule in sequenced:
        assert rule["answer"]["runId"] == rule["sequenceState"]["runId"], rule["id"]


# MARK: - Browsing a scenario that is not the active one
#
# A sidebar selection is a look, not a switch. The window shows what a scenario would do while the
# proxy goes on answering from whichever one is active, so the snapshot has to say which of the two
# it is describing — a browsed scenario presented as the running one is a person reading rules that
# are not in force and concluding the proxy is broken.

# The ids are `default`'s on purpose. A snapshot that filled a browsed row's runtime from the
# active scenario's maps — the obvious way to write this handler — would look right against a
# scenario whose ids are all its own, and be wrong for every profile where two scenarios describe
# the same endpoints, which is what scenarios of one app usually are.
BROWSED_SCENARIO = {
    "name": "orders-outage",
    "overrides": [
        {
            "id": "ovr_orders",
            "mode": "replace",
            "match": {"method": "GET", "path": "/api/v1/orders"},
            "status": 500,
        },
        {
            "id": "ovr_seq",
            "mode": "replace",
            "match": {"path": "/api/v1/items"},
            "sequence": {"steps": [{"status": 500}, {"status": 500}, {"status": 200}]},
        },
    ],
}


def seed_browsable(profile):
    seed_rules(profile)  # `default`, which stays active
    (profile / "scenarios" / "orders-outage.json").write_text(json.dumps(BROWSED_SCENARIO), encoding="utf-8")


def test_browsing_a_scenario_reports_its_rules_with_no_run(profile):
    """The rules are real; the runtime is not. Cursors and answer counts belong to the scenario the
    proxy is serving, and a browsed rule that borrowed them would report a run it has never had.

    Both scenarios name their rules `ovr_orders` and `ovr_seq` — two scenarios for one app describe
    the same endpoints — so a handler keying the active runtime by id would hand the browsed rows
    the active scenario's count and cursor, and look correct anywhere the ids happened to differ."""
    seed_browsable(profile)
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    subject = store.Store()
    store.credit(subject.answer_slot("ovr_orders"))  # the *active* rule answered a request
    subject.bump_selected(next(o for o in subject.active_overrides() if o["id"] == "ovr_seq"))
    app = control.make_app(subject, _meta)

    async def main():
        async with TestClient(TestServer(app)) as client:
            headers = {"Host": config.CONTROL_HOST_HEADER}
            browsed = await (await client.get("/__mock__/rules?scenario=orders-outage", headers=headers)).json()
            live = await (await client.get("/__mock__/rules", headers=headers)).json()
            return browsed, live

    browsed, live = asyncio.run(main())

    assert browsed["scenario"] == "orders-outage" and browsed["active"] is False
    assert [rule["id"] for rule in browsed["rules"]] == ["ovr_orders", "ovr_seq"]
    for rule in browsed["rules"]:
        assert rule["answer"] is None and rule["sequenceState"] is None, rule["id"]
    browsed_rules = {rule["id"]: rule for rule in browsed["rules"]}
    assert len(browsed_rules["ovr_seq"]["rewrite"]["sequence"]["steps"]) == 3, (
        "the browsed scenario's own steps, not the active scenario's two"
    )

    live_rules = {rule["id"]: rule for rule in live["rules"]}
    assert live["active"] is True
    assert live_rules["ovr_orders"]["answer"]["count"] == 1, "the run is still reported where it exists"
    assert live_rules["ovr_seq"]["sequenceState"]["nextStep"] == 2


def test_browsing_a_scenario_does_not_switch_the_proxy_to_it(profile):
    """The failure this closes is the whole reason the parameter exists: a read that activated what
    it was asked about would repoint the running proxy at every scenario a pointer moved over."""
    seed_browsable(profile)
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    subject = store.Store()
    app = control.make_app(subject, _meta)

    async def main():
        async with TestClient(TestServer(app)) as client:
            headers = {"Host": config.CONTROL_HOST_HEADER}
            browsed = await (await client.get("/__mock__/rules?scenario=orders-outage", headers=headers)).json()
            after = await (await client.get("/__mock__/rules", headers=headers)).json()
            return browsed, after

    browsed, after = asyncio.run(main())
    assert browsed["scenario"] == "orders-outage"
    assert after["scenario"] == "default" and after["active"] is True
    assert subject.active_name == "default", "and the store itself never moved"


def test_naming_the_active_scenario_returns_the_parameterless_snapshot(profile):
    """One payload, two ways of asking for it. A window that names the scenario it is showing —
    which is what it does once a sidebar exists — must not get a different answer from the same
    read, including the run ids that bind a count to a boundary."""
    seed_browsable(profile)
    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()
    app = control.make_app(store.Store(), _meta)

    async def main():
        async with TestClient(TestServer(app)) as client:
            headers = {"Host": config.CONTROL_HOST_HEADER}
            named = await (await client.get("/__mock__/rules?scenario=default", headers=headers)).json()
            plain = await (await client.get("/__mock__/rules", headers=headers)).json()
            return named, plain

    named, plain = asyncio.run(main())
    assert named == plain
    assert named["active"] is True


def test_browsing_an_unknown_scenario_is_a_404_that_names_it(profile):
    """Not an empty snapshot: a typo must not look like a scenario with no rules, which is a real
    thing a profile can contain and reads exactly the same on screen."""
    seed_browsable(profile)
    status, _, body = call(profile, "GET", "/__mock__/rules?scenario=nope")
    assert status == 404
    assert body["error"] == "unknown_scenario" and body["name"] == "nope"
    assert body["detail"] == "no scenario named 'nope' in this profile"


def test_browsing_with_an_empty_scenario_name_is_a_bad_request(profile):
    """`?scenario=` is a client that meant to name one and sent nothing. Reported as that, not as
    an unknown scenario called "" — the second sends someone looking for a file they never wrote."""
    seed_browsable(profile)
    status, _, body = call(profile, "GET", "/__mock__/rules?scenario=")
    assert status == 400 and body["error"] == "name_required"


def test_browsing_is_still_scoped_to_the_running_profile(profile):
    """The parameter is a new way into the same read, and the read is another profile's rules and
    saved bodies. `_guard` is global; this pins that the parameter did not step around it."""
    seed_browsable(profile)
    status, _, body = call(profile, "GET", "/__mock__/rules?scenario=orders-outage", headers=_FOREIGN)
    assert status == 409 and body["error"] == "profile_mismatch"


def test_browsing_reports_a_scenario_that_did_not_load_whole(profile):
    """Keyed by the scenario being browsed, not by the active one — otherwise a window would show
    `default`'s problems beside another scenario's rules and blame the wrong file."""
    seed_rules(profile)
    (profile / "scenarios" / "orders-outage.json").write_text(
        json.dumps(
            {
                "name": "orders-outage",
                "overrides": [
                    {"id": "ovr_kept", "match": {"path": "/api/v1/orders"}, "mode": "replace", "status": 500},
                    {"id": "ovr_typo", "match": {"path": "/api/v1/items"}, "mode": "replace", "statsu": 500},
                ],
            }
        ),
        encoding="utf-8",
    )

    status, _, body = call(profile, "GET", "/__mock__/rules?scenario=orders-outage")

    assert status == 200
    assert [rule["id"] for rule in body["rules"]] == ["ovr_kept"]
    assert len(body["notWhole"]) == 1 and "'statsu'" in body["notWhole"][0]

    _, _, plain = call(profile, "GET", "/__mock__/rules")
    assert plain["notWhole"] == [], "`default` loaded whole and must not wear another file's problem"


# MARK: - Identity for the recent list


def test_recent_rows_carry_an_id(profile):
    """The id has to survive to the HTTP boundary: it exists so a client can keep a selection
    across polls, and a client only ever sees this list through here."""

    def record(subject):
        subject.record_recent({"method": "GET", "path": "/api/v1/orders"})
        subject.record_recent({"method": "GET", "path": "/api/v1/orders"})

    status, _, body = call(profile, "GET", "/__mock__/recent", prepare=record)
    assert status == 200
    assert [entry["id"] for entry in body] == ["evt-2", "evt-1"]


def test_browsing_a_name_that_differs_only_by_case_is_unknown(profile):
    """Scenario names are exact everywhere else — they are filenames, and `set_active` matches them
    exactly — so a read that quietly resolved `Orders-Outage` would be the one place in the tool
    where two names mean one scenario, and the first place a case-sensitive filesystem disagrees."""
    seed_browsable(profile)
    status, _, body = call(profile, "GET", "/__mock__/rules?scenario=Orders-Outage")
    assert status == 404 and body["name"] == "Orders-Outage"


def test_browsing_a_scenario_whose_file_was_rejected_is_unknown(profile):
    """A file that would not parse leaves a name that is *mentioned* — health reports the problem
    against it — but no scenario. 404 is the honest answer: there are no rules to show. An empty
    snapshot would present a broken file as a scenario with nothing in it."""
    seed_rules(profile)
    (profile / "scenarios" / "broken.json").write_text("{ not json", encoding="utf-8")

    status, _, body = call(profile, "GET", "/__mock__/rules?scenario=broken")
    assert status == 404 and body["error"] == "unknown_scenario"

    _, _, health = call(profile, "GET", "/__mock__/health")
    assert "broken" not in health["scenarios"], "it never became one"
    assert list(health["scenariosNotWhole"]) == ["broken"], "but the name is still named"
    assert health["loadProblems"] and "broken" in health["loadProblems"][0]


# MARK: - When a sequence's trigger is another rule
#
# A scenario reads as a story: the steps of a sequence with the request that moves it on between
# them, and where that request is one the scenario answers itself, the rule that answers it drawn in
# the trigger's place. Whether a trigger *is* another rule is the engine's statement — a client
# comparing matchers would be comparing them by rules of its own.

STORY_OVERRIDES = [
    {
        "id": "ovr_place",
        "mode": "replace",
        "match": {"method": "post", "path": "/api/v1/orders"},
        "status": 201,
    },
    {
        "id": "ovr_orders",
        "mode": "replace",
        "match": {"method": "GET", "path": "/api/v1/orders"},
        "sequence": {
            # Spelled differently from the rule above on purpose: same requests, other letters.
            "steps": [{"status": 200}, {"status": 200}],
            "advanceOn": {"method": "POST", "path": "/api/v1/orders"},
        },
    },
    {
        "id": "ovr_items",
        "mode": "replace",
        "match": {"method": "GET", "path": "/api/v1/items"},
        "sequence": {"steps": [{"status": 200}], "advanceOn": {"method": "PUT", "path": "/api/v1/nothing"}},
    },
    {
        "id": "ovr_self",
        "mode": "replace",
        "match": {"method": "GET", "path": "/api/v1/features"},
        "sequence": {"steps": [{"status": 200}]},
    },
    {
        "id": "ovr_cancel",
        "active": False,
        "mode": "replace",
        "match": {"method": "DELETE", "path": "/api/v1/orders"},
        "status": 204,
    },
    {
        "id": "ovr_cancelled",
        "mode": "replace",
        "match": {"method": "GET", "path": "/api/v1/cancelled"},
        "sequence": {"steps": [{"status": 200}], "advanceOn": {"method": "DELETE", "path": "/api/v1/orders"}},
    },
]


def seed_story(profile):
    """The same rules under two names: one active, one only ever browsed."""
    (profile / "scenarios").mkdir(parents=True, exist_ok=True)
    for name in ("default", "story"):
        (profile / "scenarios" / f"{name}.json").write_text(
            json.dumps({"name": name, "overrides": STORY_OVERRIDES}), encoding="utf-8"
        )


def sequences_of(body):
    return {rule["id"]: rule["rewrite"]["sequence"] for rule in body["rules"] if rule["rewrite"]["sequence"]}


@pytest.mark.parametrize("path", ["/__mock__/rules", "/__mock__/rules?scenario=story"])
def test_a_sequence_names_the_rule_its_trigger_is(profile, path):
    """Both forms, because the window draws a browsed scenario exactly as it draws the active one —
    and the answer cannot come from runtime state, which a browsed scenario does not have."""
    seed_story(profile)
    status, _, body = call(profile, "GET", path)
    assert status == 200
    assert sequences_of(body)["ovr_orders"]["advanceOnRule"] == "ovr_place", (
        "the trigger and the rule are spelled differently and describe the same requests"
    )


def test_a_trigger_no_rule_answers_names_nothing(profile):
    """Null, not a guess. The request that advances this sequence comes from somewhere outside the
    scenario, and a window drawing some near-enough rule in its place would be inventing the story."""
    seed_story(profile)
    _, _, body = call(profile, "GET", "/__mock__/rules")
    assert sequences_of(body)["ovr_items"]["advanceOnRule"] is None


def test_a_self_advancing_sequence_names_no_trigger_rule(profile):
    """`self` is not a request at all — the rule moves when it answers — so there is no rule to
    draw between the steps."""
    seed_story(profile)
    _, _, body = call(profile, "GET", "/__mock__/rules")
    sequence = sequences_of(body)["ovr_self"]
    assert sequence["advanceOn"] is None and sequence["advanceOnRule"] is None


def test_an_inactive_rule_is_still_the_rule_a_trigger_is(profile):
    """The field describes the scenario as written, not what is switched on: that endpoint still
    belongs to `ovr_cancel` on screen, and a story with a hole in it where a disabled rule sits
    would send someone looking for a rule that is right there."""
    seed_story(profile)
    _, _, body = call(profile, "GET", "/__mock__/rules")
    assert sequences_of(body)["ovr_cancelled"]["advanceOnRule"] == "ovr_cancel"
    assert next(rule for rule in body["rules"] if rule["id"] == "ovr_cancel")["rewrite"]["active"] is False


@pytest.mark.parametrize("foreign", [False, True])
def test_clear_recent_preserves_rules_evidence_and_event_identity(profile, foreign):
    subjects = []

    def prepare(subject):
        subject.add_override({"id": "ovr_orders", "match": {"path": "/api/orders"}, "mode": "replace"})
        subject.reset_runtime("ovr_orders")
        store.credit(subject.answer_slot("ovr_orders"))
        store.credit(subject.answer_slot("ovr_orders"))
        subject.record_recent({"method": "GET", "path": "/api/orders"})
        subjects.append((subject, subject.answer_states(), subject.active_overrides().copy()))

    status, _, _ = call(profile, "DELETE", "/__mock__/recent", headers=_FOREIGN if foreign else {}, prepare=prepare)
    subject, answers, overrides = subjects[0]
    assert status == (409 if foreign else 200)
    assert len(subject.recent_list()) == (1 if foreign else 0)
    assert answers[0]["count"] == 2
    assert subject.answer_states() == answers
    assert subject.active_overrides() == overrides
    subject.record_recent({"method": "GET", "path": "/api/orders"})
    assert subject.recent_list()[0]["id"] == "evt-2"
