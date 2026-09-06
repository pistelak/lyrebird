"""Addon-level behaviour that only fails at runtime.

The options below are applied inside mitmproxy's `running` hook. A wrong type there kills the
proxy at startup with a traceback in a log file — not somewhere a normal unit test would look —
so they are validated here against mitmproxy's own option manager.
"""

import asyncio
import json

import pytest
from mitmproxy.addons import proxyserver
from mitmproxy.test import taddons, tflow, tutils

import addon


def test_proxy_options_are_accepted_by_mitmproxy(hosts):
    """Regression: stream_large_bodies is Optional[str] ("understands k/m/g"), not an int.
    Passing an int raised TypeError inside `running` and the proxy exited on startup."""
    with taddons.context(proxyserver.Proxyserver()) as tctx:
        tctx.options.update(**addon.proxy_options())
        assert tctx.options.stream_large_bodies == addon.STREAM_LARGE_BODIES
        assert tctx.options.allow_hosts == config_allow_hosts()


def config_allow_hosts():
    import config
    return config.allow_hosts_regexes()


def test_connection_strategy_is_lazy(hosts):
    """Regression: under mitmproxy's default "eager" strategy the upstream connection is opened
    before the request hook runs, so a `replace` override — which never needs the upstream — still
    failed with 502 for a host that does not resolve. Mocking an endpoint the backend does not
    implement yet, or working off-VPN, depends on this."""
    with taddons.context(proxyserver.Proxyserver()) as tctx:
        tctx.options.update(**addon.proxy_options())
        assert tctx.options.connection_strategy == "lazy"


# MARK: - Response construction

@pytest.mark.parametrize("headers,expected", [
    ({}, {"Content-Type": "application/json"}),
    ({"Content-Type": "application/json;charset=UTF-8"}, {"Content-Type": "application/json;charset=UTF-8"}),
    ({"content-type": "text/plain"}, {"content-type": "text/plain"}),
])
def test_content_type_is_merged_case_insensitively(headers, expected):
    """A session spelling the header `Content-Type` used to emit both that and a lowercase
    `content-type` on the wire."""
    assert addon.Lyrebird._headers_with_default_content_type(headers, json_body=True) == expected


def test_no_content_type_is_added_for_a_bodyless_response():
    assert addon.Lyrebird._headers_with_default_content_type({}, json_body=False) == {}




# MARK: - The wire behaviour, exercised through mitmproxy's own flow objects
#
# The request/response hooks are where a silent regression hurts most: a dropped patch or a
# malformed bodyless response looks exactly like "the rule didn't match".


def _flow(method="GET", path="/api/v1/orders/1", host="api.example.com"):
    flow = tflow.tflow(req=tutils.treq(method=method.encode(), path=path.encode(), host=host))
    flow.request.headers["Host"] = host
    return flow


def run_request(subject, flow):
    asyncio.run(subject.request(flow))


def test_replace_short_circuits_without_an_upstream(hosts, profile):
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "o", "mode": "replace", "status": 503,
                                "match": {"method": "GET", "path": "/api/v1/orders/*"},
                                "body": {"error": "mocked"}})
    flow = _flow()
    run_request(subject, flow)
    assert flow.response is not None
    assert flow.response.status_code == 503
    assert flow.metadata["mock_matched"] == "o"
    assert json.loads(flow.response.get_text()) == {"error": "mocked"}


def test_bodyless_status_carries_no_body_or_length(hosts, profile):
    """204 with a Content-Length is malformed, and clients do notice."""
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "d", "mode": "replace", "status": 204,
                                "match": {"method": "DELETE", "path": "/api/v1/orders/*"}})
    flow = _flow(method="DELETE")
    run_request(subject, flow)
    assert flow.response.status_code == 204
    assert flow.response.raw_content in (b"", None)
    assert "content-length" not in {k.lower() for k in flow.response.headers}


def test_a_non_intercepted_host_is_left_alone(hosts, profile):
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "o", "mode": "replace", "status": 503,
                                "match": {"path": "/api/v1/orders/*"}})
    flow = _flow(host="elsewhere.example.com")
    run_request(subject, flow)
    assert flow.response is None, "only hosts listed in the profile may be touched"


def test_patch_defers_to_the_response_hook(hosts, profile):
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "p", "mode": "patch", "match": {"path": "/api/v1/orders/*"},
                                "patch": {"extra": True}})
    flow = _flow()
    run_request(subject, flow)
    assert flow.response is None, "patch needs the upstream response, so it must not short-circuit"
    assert flow.metadata["mock_patch"]["id"] == "p"


def test_a_server_sent_event_stream_is_never_patched(hosts, profile):
    """Patching a stream would mean buffering it, which is what stalls long-poll flows."""
    subject = addon.Lyrebird()
    flow = _flow()
    flow.response = tutils.tresp(headers=((b"content-type", b"text/event-stream"),))
    flow.metadata["mock_patch"] = {"id": "p", "patch": {}}
    subject.responseheaders(flow)
    assert flow.metadata["mock_patch"] is None
    assert flow.metadata["mock_patch_skipped"] == "event_stream"
    assert flow.response.stream is True


def test_a_non_json_response_is_never_patched(hosts, profile):
    subject = addon.Lyrebird()
    flow = _flow()
    flow.response = tutils.tresp(headers=((b"content-type", b"text/html"),))
    flow.metadata["mock_patch"] = {"id": "p", "patch": {}}
    subject.responseheaders(flow)
    assert flow.metadata["mock_patch_skipped"] == "not_json"


# MARK: - Sequences
#
# These go through real flow objects because the ordering inside `request()` is the whole design:
# select, then answer, then advance. A unit test of the store cannot catch getting that wrong.

def _body(flow):
    return json.loads(flow.response.get_text()) if flow.response else None


LIST_RULES = [
    {"id": "ovr_list", "mode": "replace", "match": {"method": "GET", "path": "/api/v1/items"},
     "sequence": {"advanceOn": {"method": "DELETE", "path": "/api/v1/items/*"},
                  "steps": [{"body": {"items": ["a", "b", "c"]}},
                            {"body": {"items": ["a", "c"]}}]}},
    {"id": "ovr_del", "mode": "replace", "status": 204,
     "match": {"method": "DELETE", "path": "/api/v1/items/*"}},
]


def test_repeated_fetches_do_not_advance_but_a_delete_does(hosts, profile):
    """The delete-then-refresh scenario end to end. The screen may fetch the list twice on appear —
    a prefetch, a re-render — and that must not move the sequence; only the DELETE may."""
    subject = addon.Lyrebird()
    for rule in LIST_RULES:
        subject.store.add_override(dict(rule))

    first, second = _flow(path="/api/v1/items"), _flow(path="/api/v1/items")
    run_request(subject, first)
    run_request(subject, second)
    assert _body(first) == _body(second) == {"items": ["a", "b", "c"]}

    deletion = _flow(method="DELETE", path="/api/v1/items/b")
    run_request(subject, deletion)
    assert deletion.response.status_code == 204
    assert deletion.metadata["mock_advanced"] == ["ovr_list"]

    third = _flow(path="/api/v1/items")
    run_request(subject, third)
    assert _body(third) == {"items": ["a", "c"]}


def test_a_request_no_override_answers_still_advances_a_sequence(hosts, profile):
    """The advance scan sits outside the 'an override matched' branch on purpose: a DELETE going
    straight to the real backend must still move a rule that is watching for it."""
    subject = addon.Lyrebird()
    subject.store.add_override(dict(LIST_RULES[0]))   # the list rule only — nothing answers DELETE

    deletion = _flow(method="DELETE", path="/api/v1/items/b")
    run_request(subject, deletion)
    assert deletion.response is None, "nothing should have answered it"
    assert deletion.metadata["mock_advanced"] == ["ovr_list"]

    after = _flow(path="/api/v1/items")
    run_request(subject, after)
    assert _body(after) == {"items": ["a", "c"]}


def _retry_subject(policy=None):
    sequence = {"steps": [{"status": 503}, {"status": 201}]}
    if policy:
        sequence["onExhausted"] = policy
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "ovr_retry", "mode": "replace",
                                "match": {"method": "GET", "path": "/api/v1/orders/*"},
                                "sequence": sequence})
    return subject


def test_a_self_triggered_sequence_answers_each_call_differently(hosts, profile):
    subject = _retry_subject()
    statuses = []
    for _ in range(2):
        flow = _flow()
        run_request(subject, flow)
        statuses.append(flow.response.status_code)
    assert statuses == [503, 201]


def test_exhaustion_defaults_to_a_loud_error(hosts, profile):
    """Repeating the last step would let an unplanned extra request pass for a successful one."""
    subject = _retry_subject()
    for _ in range(2):
        run_request(subject, _flow())

    overrun = _flow()
    run_request(subject, overrun)
    assert overrun.response.status_code == 500
    message = _body(overrun)["error"]["message"]
    assert "ovr_retry" in message and "2 step(s)" in message
    assert overrun.metadata["mock_sequence"]["overrun"] is True


def test_repeat_last_serves_the_final_step_but_is_still_marked_overrun(hosts, profile):
    subject = _retry_subject("repeatLast")
    for _ in range(2):
        run_request(subject, _flow())

    overrun = _flow()
    run_request(subject, overrun)
    assert overrun.response.status_code == 201, "the last step answers again"
    assert overrun.metadata["mock_sequence"]["overrun"] is True, "but it is not pretending to be step 2"


def test_pass_through_is_distinguishable_from_no_rule_matching(hosts, profile):
    """An exhausted passThrough answers nothing, so it has no `matched` — and `recent --matched`
    filters on that. Without a separate sequence id the overrun would be invisible to exactly the
    command used to check the scenario."""
    subject = _retry_subject("passThrough")
    for _ in range(2):
        run_request(subject, _flow())

    overrun = _flow()
    run_request(subject, overrun)
    assert overrun.response is None, "the real upstream must answer"
    assert overrun.metadata.get("mock_matched") is None
    assert overrun.metadata["mock_sequence"]["sequenceId"] == "ovr_retry"
    assert overrun.metadata["mock_sequence"]["overrun"] is True


# MARK: - The delay boundary
#
# `delayMs` is parent-level only, so the sleep happens BEFORE the step is chosen. That is what makes
# selection and response construction one synchronous block, and it is why sequences need no
# generation counter or staleness tracking. Both tests below fail if the order is reversed.

def _delayed_subject():
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "ovr_slow", "mode": "replace", "delayMs": 60,
                                "match": {"method": "GET", "path": "/api/v1/orders/*"},
                                "sequence": {"steps": [{"status": 201}, {"status": 202}]}})
    return subject


def test_a_reset_during_the_delay_is_honoured(hosts, profile):
    """An operator who rewinds while a delayed flow is in the air gets the rewind they asked for,
    not the step that was current when the request arrived."""
    subject = _delayed_subject()
    subject.store.bump_selected(subject.store.find_override("GET", "/api/v1/orders/1", {}, ""))
    assert subject.store.sequence_states()[0]["nextStep"] == 2

    async def reset_mid_flight():
        flow = _flow()
        task = asyncio.create_task(subject.request(flow))
        await asyncio.sleep(0.01)              # let it reach the sleep
        subject.store.reset_runtime()
        await task
        return flow

    flow = asyncio.run(reset_mid_flight())
    assert flow.response.status_code == 201, "the step was chosen after the delay, so the reset won"


def test_two_concurrent_delayed_flows_take_different_steps(hosts, profile):
    """Each continuation selects and advances synchronously on waking, so they cannot both read the
    same cursor."""
    subject = _delayed_subject()

    async def both():
        first, second = _flow(), _flow()
        await asyncio.gather(subject.request(first), subject.request(second))
        return first, second

    first, second = asyncio.run(both())
    assert sorted([first.response.status_code, second.response.status_code]) == [201, 202]


# MARK: - The error hook

def test_a_failed_pass_through_overrun_still_reaches_recent(hosts, profile):
    """The case this hook exists for: the sequence stood aside, the real upstream was down, and
    without a record the overrun looks like a rule that simply never fired."""
    subject = _retry_subject("passThrough")
    for _ in range(2):
        run_request(subject, _flow())

    overrun = _flow()
    run_request(subject, overrun)
    assert overrun.response is None
    subject.error(overrun)

    entry = subject.store.recent_list()[0]
    assert entry["sequenceId"] == "ovr_retry"
    assert entry["overrun"] is True
    assert entry["status"] == 0
    assert entry["matched"] is None, "nothing answered it, and `matched` must keep meaning that"


def test_a_mock_answered_flow_that_errors_keeps_its_matched_id(hosts, profile):
    """A client that hangs up mid-write reaches `error` with the override's response already made.
    Recording `matched: null` there would say nothing answered — the same signal as a rule that
    never fired, which is the collision this codebase keeps having to fix."""
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "s", "mode": "replace",
                                "match": {"path": "/api/v1/orders/*"},
                                "sequence": {"steps": [{"status": 201}]}})
    flow = _flow()
    run_request(subject, flow)
    assert flow.response.status_code == 201

    subject.error(flow)
    entry = subject.store.recent_list()[0]
    assert entry["matched"] == "s", "an override did answer, and the record must keep saying so"
    assert entry["sequenceId"] == "s"
    assert entry["status"] == 0


def test_the_error_hook_ignores_flows_with_no_sequence(hosts, profile):
    """Recording every failed flow would change what /recent means; that is a separate change."""
    subject = addon.Lyrebird()
    subject.error(_flow())
    assert subject.store.recent_list() == []


# MARK: - The rules can move while a delayed request sleeps
#
# The delay is awaited before the step is chosen, which keeps selection and the response one
# synchronous block. But the *rule* was chosen before the sleep, so it has to be re-selected
# afterwards or a delayed flow answers from a definition that is no longer live.

def _replaceable(statuses):
    return {"id": "s", "mode": "replace", "delayMs": 60, "match": {"path": "/api/v1/orders/*"},
            "sequence": {"steps": [{"status": status} for status in statuses]}}


def _mid_flight(subject, disturb):
    async def scenario():
        flow = _flow()
        task = asyncio.create_task(subject.request(flow))
        await asyncio.sleep(0.01)     # let it reach the sleep
        disturb()
        await task
        return flow
    return asyncio.run(scenario())


def test_replacing_a_rule_during_its_delay_serves_the_replacement(hosts, profile):
    """Held across the sleep, the old rule answered 201 and then advanced the new rule's cursor, so
    the replacement's first step was never served at all."""
    subject = addon.Lyrebird()
    subject.store.add_override(_replaceable([201, 202]))

    flow = _mid_flight(subject, lambda: subject.store.add_override(_replaceable([501, 502])))
    assert flow.response.status_code == 501, "the live rule's first step"

    after = _flow()
    run_request(subject, after)
    assert after.response.status_code == 502


def test_removing_a_rule_during_its_delay_stops_it_answering(hosts, profile):
    subject = addon.Lyrebird()
    subject.store.add_override(_replaceable([201, 202]))
    flow = _mid_flight(subject, lambda: subject.store.remove_override("s"))
    assert flow.response is None, "a rule that no longer exists must not answer"


def test_switching_sessions_during_a_delay_uses_the_new_session(hosts, profile):
    subject = addon.Lyrebird()
    subject.store.add_override(_replaceable([201, 202]))
    subject.store.create_session("empty")
    flow = _mid_flight(subject, lambda: subject.store.set_active("empty"))
    assert flow.response is None


def test_a_failed_advance_only_request_still_reaches_recent(hosts, profile):
    """A request no override answered can still move a sequence. If its upstream then fails, the
    cursor has changed with nothing in /recent to explain why."""
    subject = addon.Lyrebird()
    subject.store.add_override(dict(LIST_RULES[0]))   # nothing answers the DELETE

    deletion = _flow(method="DELETE", path="/api/v1/items/b")
    run_request(subject, deletion)
    assert deletion.response is None
    subject.error(deletion)

    entry = subject.store.recent_list()[0]
    assert entry["advanced"] == ["ovr_list"]
    assert entry["status"] == 0


# MARK: - Crediting the rule that answered
#
# Counted where the answer is produced, not where it is recorded. Everything below is a case where
# recording-time counting gave the wrong answer: a rule credited for a response it never produced,
# or a response produced and never credited.

def _answers(subject):
    return {state["id"]: state["count"] for state in subject.store.answer_states()}


def test_a_replace_is_credited_once(hosts, profile):
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "o", "mode": "replace", "status": 200,
                                "match": {"path": "/api/v1/orders/*"}})
    run_request(subject, _flow())
    assert _answers(subject) == {"o": 1}


def test_a_rule_that_matched_but_lost_is_never_credited(hosts, profile):
    """`find_override` returns only the most specific match, so a rule whose matcher fits the
    request may not be the rule that answered it."""
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "broad", "mode": "replace", "status": 200,
                                "match": {"path": "/api/v1/*"}})
    subject.store.add_override({"id": "narrow", "mode": "replace", "status": 201,
                                "match": {"path": "/api/v1/orders/1"}})
    run_request(subject, _flow())
    assert _answers(subject) == {"broad": 0, "narrow": 1}


def test_a_patch_is_not_credited_until_it_has_actually_merged(hosts, profile):
    """A patch that is selected has answered nothing yet — the upstream may turn out to be a
    stream, or not JSON, in which case the request is served by the real backend."""
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "p", "mode": "patch", "match": {"path": "/api/v1/orders/*"},
                                "patch": {"extra": True}})
    flow = _flow()
    run_request(subject, flow)
    assert _answers(subject) == {"p": 0}, "selected is not answered"

    flow.response = tutils.tresp(headers=((b"content-type", b"application/json"),),
                                 content=b'{"a": 1}')
    subject.response(flow)
    assert _answers(subject) == {"p": 1}
    assert _body(flow) == {"a": 1, "extra": True}


def test_a_skipped_patch_is_never_credited(hosts, profile):
    """`patchSkipped` means the real backend answered. Crediting it would report a mock in play for
    a screen that was served live — the precise thing an assertion exists to rule out."""
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "p", "mode": "patch", "match": {"path": "/api/v1/orders/*"},
                                "patch": {"extra": True}})
    flow = _flow()
    run_request(subject, flow)
    flow.response = tutils.tresp(headers=((b"content-type", b"text/html"),), content=b"<html>")
    subject.responseheaders(flow)
    subject.response(flow)
    assert flow.metadata["mock_patch_skipped"] == "not_json"
    assert _answers(subject) == {"p": 0}


def test_an_exhausted_pass_through_is_never_credited(hosts, profile):
    """It stands aside on purpose: the real upstream answers, so the rule answered nothing."""
    subject = addon.Lyrebird()
    subject.store.add_override({
        "id": "seq", "mode": "replace", "match": {"path": "/api/v1/orders/*"},
        "sequence": {"onExhausted": "passThrough", "steps": [{"status": 200}]}})
    run_request(subject, _flow())
    overrun = _flow()
    run_request(subject, overrun)
    assert overrun.response is None
    assert _answers(subject) == {"seq": 1}, "the first request answered; the overrun did not"


def test_an_exhausted_error_is_credited_because_it_did_answer(hosts, profile):
    """Lyrebird's own 500 is still an answer from that rule, and the test that gets it should be
    told the mock was in play — it explains the 500."""
    subject = addon.Lyrebird()
    subject.store.add_override({
        "id": "seq", "mode": "replace", "match": {"path": "/api/v1/orders/*"},
        "sequence": {"steps": [{"status": 200}]}})
    run_request(subject, _flow())
    overrun = _flow()
    run_request(subject, overrun)
    assert overrun.response.status_code == 500
    assert _answers(subject) == {"seq": 2}


def test_a_replace_whose_flow_dies_is_credited_and_recorded(hosts, profile):
    """The override produced the response; the client hanging up does not un-answer it. It must
    also reach /recent, or a rule shows answers with no traffic to account for them."""
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "o", "mode": "replace", "status": 200,
                                "match": {"path": "/api/v1/orders/*"}})
    flow = _flow()
    run_request(subject, flow)
    subject.error(flow)
    assert _answers(subject) == {"o": 1}
    assert [entry["matched"] for entry in subject.store.recent_list()] == ["o"]


def test_a_replacement_installed_during_a_delay_is_the_rule_credited(hosts, profile):
    """The delayed path re-selects after the sleep. Crediting a slot captured before it would
    report an answer for the rule that was displaced and never served."""
    subject = addon.Lyrebird()
    subject.store.add_override(_replaceable([201, 202]))
    flow = _mid_flight(subject, lambda: subject.store.add_override(_replaceable([501, 502])))
    assert flow.response.status_code == 501
    assert _answers(subject) == {"s": 1}, "credited once, to the definition that actually answered"


def test_a_patch_landing_after_a_session_switch_credits_nobody(hosts, profile):
    """The whole reason the slot is captured rather than looked up by id: session B has its own
    rule under the same id, and it never saw this request."""
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "shared", "mode": "patch",
                                "match": {"path": "/api/v1/orders/*"}, "patch": {"a": 1}})
    flow = _flow()
    run_request(subject, flow)

    subject.store.create_session("other")
    subject.store.set_active("other")
    subject.store.add_override({"id": "shared", "mode": "patch",
                                "match": {"path": "/api/v1/orders/*"}, "patch": {"a": 1}})

    flow.response = tutils.tresp(headers=((b"content-type", b"application/json"),),
                                 content=b'{"b": 2}')
    subject.response(flow)
    assert _answers(subject) == {"shared": 0}, "session B's rule never answered this request"


def test_a_patch_landing_after_its_rule_is_replaced_credits_nobody(hosts, profile):
    subject = addon.Lyrebird()
    subject.store.add_override({"id": "r", "mode": "patch",
                                "match": {"path": "/api/v1/orders/*"}, "patch": {"a": 1}})
    flow = _flow()
    run_request(subject, flow)
    subject.store.add_override({"id": "r", "mode": "patch",
                                "match": {"path": "/api/v1/orders/*"}, "patch": {"a": 2}})
    flow.response = tutils.tresp(headers=((b"content-type", b"application/json"),),
                                 content=b'{"b": 2}')
    subject.response(flow)
    assert _answers(subject) == {"r": 0}


def test_a_repeated_last_step_is_credited_like_any_other_answer(hosts, profile):
    """`repeatLast` answers from the rule, so each repeat is an answer — a test asserting the mock
    was in play must not stop counting the moment the sequence runs out of planned steps."""
    subject = _retry_subject("repeatLast")
    for _ in range(4):        # two planned steps, then two repeats of the last
        run_request(subject, _flow())
    assert _answers(subject) == {"ovr_retry": 4}


# MARK: - Health under a PAC that cannot be read

def test_health_reports_an_unreadable_pac_instead_of_dying(profile, monkeypatch):
    """The CLI reads "no health" as "no proxy": the watchdog would restore the network over a live
    proxy and `up` would start a second one. So `/health` must answer, saying the PAC is unproven
    rather than off."""
    import netproxy

    def boom(service):
        raise netproxy.NetworkSetupError("`networksetup -getautoproxyurl Wi-Fi` failed: 1")

    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", boom)
    meta = addon.Lyrebird()._meta()
    assert meta["proxyUp"] is True
    assert meta["intercepting"] is False
    assert "networksetup" in meta["pacError"]


def test_the_addon_refuses_to_load_on_a_malformed_profile(profile):
    """The proxy is the thing that intercepts, so it is the thing that must not start on a profile
    it cannot read — a SystemExit here kills mitmdump at startup, which `up` reports."""
    (profile / "profile.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        addon.Lyrebird()
