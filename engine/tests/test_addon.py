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
