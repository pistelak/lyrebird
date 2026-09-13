"""The pure core: what a PAC reading is, what "restored" means, and what the decoder accepts.

Nothing here touches the filesystem, the network or a process. Everything `up` and `down` decide
about somebody's proxy settings comes through these functions, so they are tested directly rather
than through a command that would also have to be mocked into place.
"""

import math

import pytest

import ownership
from ownership import (
    Configured,
    DecodeError,
    Off,
    Owner,
    Pac,
    PacClass,
    Ref,
    ServiceRef,
    SessionRecord,
    Simulator,
    UnownedClass,
    classify,
    classify_unowned,
    decode,
    encode,
    lyrebird_port,
    our_url,
    restore_target,
    satisfies,
)

PORT = 8088
OURS = our_url(PORT)
OTHER = our_url(9999)
CORPORATE = "http://proxy.example.com/corp.pac"


def _record(**overrides):
    fields = {
        "version": ownership.SESSION_VERSION,
        "owner": Owner(control_port=PORT, profile_fingerprint="abc123", state_root="/tmp/lyrebird-tests/state"),
        "service": ServiceRef(name="Wi-Fi", device="en0"),
        "baseline": Pac("", False),
        "proxy": Ref(pid=4321, create_time=1000.5),
        "simulator": None,
    }
    return SessionRecord(**{**fields, **overrides})


# MARK: - the URL


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://127.0.0.1/proxy.pac",
        "https://127.0.0.1:8088/proxy.pac",
        "http://localhost:8088/proxy.pac",
        "http://127.0.0.1:8088/proxy.pac?x=1",
        " http://127.0.0.1:8088/proxy.pac",
        "http://proxy.example.com/?u=http://127.0.0.1:8088/proxy.pac",
        "http://127.0.0.1:0/proxy.pac",
        "http://127.0.0.1:70000/proxy.pac",
    ],
)
def test_lyrebird_port_is_a_strict_parse(url):
    """A URL that merely resembles or contains ours is somebody else's.

    Read loosely, `up` would install over a corporate PAC whose query string happens to mention a
    loopback address, and `down` would classify a stranger's PAC as its own residue.
    """
    assert lyrebird_port(url) is None


def test_lyrebird_port_reads_our_own_url_on_any_port():
    assert lyrebird_port(OURS) == PORT
    assert lyrebird_port(OTHER) == 9999


# MARK: - the target


@pytest.mark.parametrize(
    ("baseline", "expected"),
    [
        (Pac("", False), Off()),
        (Pac(OTHER, False), Off()),  # disabled residue of an earlier session: Off is the same end state
        (Pac(CORPORATE, True), Configured(CORPORATE, True)),
        (Pac(CORPORATE, False), Configured(CORPORATE, False)),
    ],
)
def test_restore_target_says_what_restored_means(baseline, expected):
    assert restore_target(baseline) == expected


# MARK: - the classifier


@pytest.mark.parametrize(
    ("observed", "baseline", "expected"),
    [
        # Ours outranks everything: a PAC pointing at this port is this session's whatever the
        # baseline was.
        (Pac(OURS, True), Pac("", False), PacClass.OURS_ENABLED),
        (Pac(OURS, False), Pac("", False), PacClass.OURS_DISABLED),
        (Pac(OURS, False), Pac(CORPORATE, True), PacClass.OURS_DISABLED),
        # The baseline's URL with the wrong flag is one command from the target, not a stranger.
        (Pac(CORPORATE, False), Pac(CORPORATE, True), PacClass.RESUMABLE),
        (Pac(CORPORATE, True), Pac(CORPORATE, False), PacClass.RESUMABLE),
        (Pac("", True), Pac("", False), PacClass.RESUMABLE),
        # At target: the Off target accepts an empty URL and any disabled Lyrebird residue.
        (Pac("", False), Pac("", False), PacClass.AT_TARGET),
        (Pac(OTHER, False), Pac("", False), PacClass.AT_TARGET),
        (Pac(CORPORATE, True), Pac(CORPORATE, True), PacClass.AT_TARGET),
        # Anybody else's.
        (Pac(CORPORATE, True), Pac("", False), PacClass.FOREIGN),
        (Pac(OTHER, True), Pac("", False), PacClass.FOREIGN),
        (Pac("", False), Pac(CORPORATE, True), PacClass.FOREIGN),
    ],
)
def test_classifier_returns_the_first_matching_class_in_priority(observed, baseline, expected):
    """The overlaps are resolved in one place. A configured baseline is both untouched and at
    target; disabled residue is both ours-disabled and at the Off target — and every command that
    reads a PAC has to agree on which of those it is."""
    assert classify(observed, PORT, baseline) is expected


def test_classifier_reads_an_enabled_empty_url_as_resumable():
    """`("", True)` over a `("", False)` baseline is one `state off` from the target. Read as
    FOREIGN, `down` would refuse to restore a PAC nobody but macOS had touched."""
    assert classify(Pac("", True), PORT, Pac("", False)) is PacClass.RESUMABLE


def test_satisfies_accepts_the_residue_a_down_leaves():
    """OURS_DISABLED outranks AT_TARGET, so a check spelled "must be AT_TARGET" would call every
    successful Off restore a failure."""
    assert satisfies(PacClass.OURS_DISABLED, Off())
    assert not satisfies(PacClass.OURS_DISABLED, Configured(CORPORATE, True))
    assert satisfies(PacClass.AT_TARGET, Off())
    assert not satisfies(PacClass.OURS_ENABLED, Off())
    assert not satisfies(PacClass.RESUMABLE, Off())
    assert not satisfies(PacClass.FOREIGN, Off())


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        (Pac("", False), UnownedClass.EMPTY),
        (Pac("", True), UnownedClass.EMPTY),
        (Pac(OURS, True), UnownedClass.LYREBIRD_ENABLED),
        (Pac(OTHER, True), UnownedClass.LYREBIRD_ENABLED),
        (Pac(OURS, False), UnownedClass.LYREBIRD_DISABLED),
        (Pac(CORPORATE, True), UnownedClass.OTHER),
        (Pac(CORPORATE, False), UnownedClass.OTHER),
    ],
)
def test_classify_unowned_reads_any_port_as_a_lyrebird_pac(observed, expected):
    """`up`'s question. An enabled Lyrebird PAC on *any* port with no journal is somebody's unowned
    session, and installing over it would strand it."""
    assert classify_unowned(observed) is expected


# MARK: - encode / decode


def test_encode_decode_roundtrips_every_field():
    record = _record(
        baseline=Pac(CORPORATE, True),
        simulator=Simulator(udid="PHONE-1", name="iPhone 17 Pro"),
        service=ServiceRef(name="Wi-Fi", device="en0"),
    )
    assert decode(encode(record)) == record


def test_encode_writes_the_documented_keys():
    payload = encode(_record())
    assert set(payload) == {"version", "owner", "service", "baseline", "proxy", "simulator"}
    assert payload["proxy"] == {"pid": 4321, "createTime": 1000.5}
    assert payload["owner"] == {
        "controlPort": PORT,
        "profileFingerprint": "abc123",
        "stateRoot": "/tmp/lyrebird-tests/state",
    }


def _payload(**overrides):
    data = encode(_record())
    data.update(overrides)
    return data


def _reject(payload):
    with pytest.raises(DecodeError) as raised:
        decode(payload)
    return str(raised.value)


def test_decode_rejects_an_unknown_key():
    """A record written by something that knows more than we do. Guessing at it is how a journal
    from a newer engine gets acted on by an older `down`."""
    assert "unknown" in _reject(_payload(phase={"kind": "active"}))


def test_decode_rejects_a_missing_key():
    payload = _payload()
    del payload["proxy"]
    assert "missing proxy" in _reject(payload)


def test_decode_rejects_a_bool_where_an_int_is_expected():
    """`bool` is an `int` in Python: `True` read as a pid of 1 would have `down` signalling init."""
    assert "expected an integer" in _reject(_payload(proxy={"pid": True, "createTime": 1.0}))


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_decode_rejects_a_port_out_of_range(port):
    owner = {"controlPort": port, "profileFingerprint": "abc", "stateRoot": "/tmp/x"}
    assert "is not a port" in _reject(_payload(owner=owner))


def test_decode_rejects_a_non_positive_pid():
    """`psutil.Process(-1)` raises a plain `ValueError`, which the adapter does not map: a
    non-positive pid must never reach it."""
    assert "pid of 1 or more" in _reject(_payload(proxy={"pid": 0, "createTime": 1.0}))


@pytest.mark.parametrize("create_time", [1, math.inf, math.nan])
def test_decode_rejects_a_create_time_that_is_not_a_finite_float(create_time):
    assert "float" in _reject(_payload(proxy={"pid": 7, "createTime": create_time}))


def test_decode_rejects_an_enabled_lyrebird_baseline():
    """A baseline `restore_target` can never satisfy: every `down` would refuse forever."""
    assert "enabled Lyrebird" in _reject(_payload(baseline={"url": OURS, "enabled": True}))


def test_decode_rejects_an_enabled_empty_baseline():
    """`("", True)` as a baseline makes the satisfied reading `("", False)` classify as RESUMABLE
    forever, so `down` would keep switching a PAC that is already off."""
    assert "no URL may not be enabled" in _reject(_payload(baseline={"url": "", "enabled": True}))


def test_decode_rejects_a_version_it_does_not_know():
    assert "version" in _reject(_payload(version=2))


@pytest.mark.parametrize("payload", [[], "record", 7, None])
def test_decode_rejects_a_payload_that_is_not_an_object(payload):
    assert "expected an object" in _reject(payload)


def test_the_record_type_checks_its_own_baseline():
    """Constructed directly, not only decoded: `up` builds one from a live reading, and the
    invariant has to hold there too."""
    with pytest.raises(DecodeError):
        _record(baseline=Pac(OURS, True))
