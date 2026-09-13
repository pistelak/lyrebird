"""The control-API transport: one fetch, one observation, two views.

The distinction the tests are about is "nothing answered on this port" versus "something answered
and it was not what we expected". They are not the same fact, and reading the second as the first
is what would let a sweep switch off the PAC of a session that is alive — so an HTTP 500, a 404, a
body that is not JSON, a body that could not be read at all and a 302 to nowhere are all
`Answering`, and only a connection that produced no response is `Silent`.
"""

import http.server
import io
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

import api
import config
import ownership as own


@pytest.fixture
def real_transport(monkeypatch, _no_real_control_transport):
    """Put the real `api._open` back: these tests answer their own requests on a port of their own,
    and a stubbed transport would prove nothing about the opener."""
    monkeypatch.setattr(api, "_open", _no_real_control_transport)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers whatever the class attribute says, so a test can shape a real HTTP response."""

    answer = (200, b'{"pid": 4321}')

    def do_GET(self):  # noqa: N802 — http.server's naming
        status, body = type(self).answer
        self.send_response(status)
        if status in (301, 302):
            self.send_header("Location", "http://127.0.0.1:1/moved")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def serving(answer):
    handler = type("_Answer", (_Handler,), {"answer": answer})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _StubResponse:
    """A response `_fetch` can read the status off and then fail to read the body from."""

    def __init__(self, status, body=None, error=None):
        self.status = status
        self._body = body
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        if self._error is not None:
            raise self._error
        return self._body

    def close(self):
        pass


def answers(monkeypatch, response):
    monkeypatch.setattr(api, "_open", lambda request, timeout: response)


# MARK: - the guard the whole suite depends on


def test_an_unstubbed_health_observation_fails_before_any_network_access():
    """Every request goes through `api._open`, which the autouse guard replaces. If that stopped
    being true, a CLI test would quietly read a contributor's own running proxy."""
    with pytest.raises(AssertionError):
        api.observe_health()


# MARK: - decode_health


def test_unreachable_is_the_only_silence():
    assert api.decode_health(api.Unreachable("connection refused")) == api.HealthObservation(own.Silent(), None)


@pytest.mark.parametrize(
    ("fetched", "pid"),
    [
        (api.Fetched(200, b'{"pid": 4321}'), 4321),
        (api.Fetched(200, b'{"pid": "4321"}'), None),
        (api.Fetched(200, b'{"pid": true}'), None),
        (api.Fetched(200, b'{"pid": 0}'), None),
        (api.Fetched(200, b"null"), None),
        (api.Fetched(200, b"[1, 2]"), None),
        (api.Fetched(200, b"not json at all"), None),
        (api.Fetched(404, b'{"error": "not_found"}'), None),
        (api.Fetched(500, b"<html>"), None),
        (api.Fetched(421, b'{"error": "bad_host"}'), None),
        (api.Fetched(200, None), None),
    ],
    ids=[
        "a pid",
        "a pid as a string",
        "a boolean pid",
        "a pid of zero",
        "a body of null",
        "a list body",
        "a body that is not JSON",
        "a 404",
        "a 500",
        "a 421",
        "a body that could not be read",
    ],
)
def test_observe_health_parses_exactly(fetched, pid):
    """Everything that answered is `Answering`, and a field that is not the exact type it should be
    is `None` — never a value a decision would act on. A boolean pid read as 1 would make a
    stranger's answer look like the recorded proxy."""
    observation = api.decode_health(fetched)
    assert isinstance(observation.health, own.Answering)
    assert observation.health.pid == pid


def test_decode_health_reads_the_fingerprint_and_the_journal_error():
    observation = api.decode_health(
        api.Fetched(200, json.dumps({"pid": 7, "profileFingerprint": "ab12cd", "journalError": "broken"}).encode())
    )
    assert observation.health == own.Answering(pid=7, fingerprint="ab12cd", journal_error="broken")
    assert observation.raw == {"pid": 7, "profileFingerprint": "ab12cd", "journalError": "broken"}


def test_decode_health_reads_a_409s_running_fingerprint():
    observation = api.decode_health(api.Fetched(409, b'{"error": "profile_mismatch", "running": "0123456789ab"}'))
    assert observation.health.fingerprint == "0123456789ab"
    assert observation.raw is None, "a payload is only a payload when the status said it was one"


def test_a_200_with_a_null_body_is_a_listener_not_a_silence(monkeypatch):
    """`json.loads(body)` alone cannot tell this from a connection failure, and a sweep reading it
    as silence would switch off the PAC of a port that answers."""
    answers(monkeypatch, _StubResponse(200, b"null"))
    observation = api.observe_health(8088)
    assert isinstance(observation.health, own.Answering)
    assert observation.raw is None


def test_fetch_reports_a_response_whose_body_read_failed_as_fetched(monkeypatch):
    """Headers arrived, so a listener is there; only a connection that produced no response at all
    is unreachable."""
    answers(monkeypatch, _StubResponse(200, error=OSError("connection reset")))
    fetched = api._fetch("/__mock__/health", 8088)
    assert fetched == api.Fetched(200, None)


def test_fetch_reports_a_refused_connection_as_unreachable(monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(api, "_open", refuse)
    assert isinstance(api._fetch("/__mock__/health", 8088), api.Unreachable)


def test_an_http_error_is_an_answer(monkeypatch):
    def raise_http(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "Server Error", {}, io.BytesIO(b"boom"))

    monkeypatch.setattr(api, "_open", raise_http)
    assert api._fetch("/__mock__/health", 8088) == api.Fetched(500, b"boom")


# MARK: - the URL and the Host header follow the port


def test_a_health_reading_names_the_port_it_is_about(monkeypatch):
    """Both halves come from the port: the control guard answers 421 to any other Host, so a
    request to the journal's port carrying this process's configured header would read as an
    outage rather than the answer it is."""
    seen = {}

    def capture(request, timeout):
        seen["url"] = request.full_url
        seen["host"] = request.get_header("Host")
        return _StubResponse(200, b'{"pid": 1}')

    monkeypatch.setattr(api, "_open", capture)
    api.observe_health(9099)
    assert seen == {"url": "http://127.0.0.1:9099/__mock__/health", "host": "127.0.0.1:9099"}
    assert config.CONTROL_PORT != 9099, "the point of the assertion above is that config was not used"

    api.observe_health()
    assert seen["url"] == f"{config.CONTROL_ORIGIN}/__mock__/health"
    assert seen["host"] == config.CONTROL_HOST_HEADER


# MARK: - the compatibility reader


def test_health_compat_reader_refuses_a_409_mismatch_but_not_a_500(monkeypatch, profile):
    """A 409 mismatch and a 500 carrying the same fingerprint decode to the same `Answering`, so
    the refusal is applied to the status of the fetch — which is why `_health` keeps the `Fetched`
    rather than going through `HealthObservation`."""
    answers(monkeypatch, _StubResponse(409, b'{"error": "profile_mismatch", "running": "0123456789ab"}'))
    with pytest.raises(SystemExit):
        api._health()

    answers(monkeypatch, _StubResponse(500, b'{"error": "profile_mismatch", "running": "0123456789ab"}'))
    assert api._health() is None  # a 500 is an outage to report, not a refusal to exit on

    assert isinstance(api.observe_health().health, own.Answering), "both are listeners to `observe_health`"


def test_health_returns_the_payload_of_a_good_reading(monkeypatch, profile):
    answers(monkeypatch, _StubResponse(200, b'{"pid": 4321, "activeScenario": "default"}'))
    assert api._health() == {"pid": 4321, "activeScenario": "default"}


# MARK: - the opener itself, against a real local server


def test_fetch_does_not_follow_a_redirect_to_an_unreachable_address(real_transport):
    """`urlopen` follows redirects internally, which turns "port N answers 302 to nowhere" into a
    connection error — and the sweep would switch off N's PAC while N answers."""
    server = serving((302, b""))
    try:
        fetched = api._fetch("/__mock__/health", server.server_address[1])
    finally:
        server.shutdown()
    assert isinstance(fetched, api.Fetched) and fetched.status == 302
    assert isinstance(api.decode_health(fetched).health, own.Answering)


def test_fetch_ignores_a_configured_http_proxy(real_transport, monkeypatch):
    """A loopback control request must not be routed through a proxy whose failure would read as
    "the port is silent". The opener is rebuilt here on purpose: one built at import proves nothing
    about a default `ProxyHandler()` that had already cached the environment."""
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{free_port()}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr(api, "_OPENER", api._build_opener())
    server = serving((200, b'{"pid": 4321}'))
    try:
        observation = api.observe_health(server.server_address[1])
    finally:
        server.shutdown()
    assert observation.health == own.Answering(pid=4321, fingerprint=None, journal_error=None)


def test_a_closed_port_is_silent(real_transport):
    assert isinstance(api.observe_health(free_port()).health, own.Silent)
