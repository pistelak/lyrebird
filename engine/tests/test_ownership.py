"""The pure ownership core: the decoder, the classifier, and the four decision functions.

Nothing here touches a disk, a process or a network — that is the point of the module. The
decision tests are exhaustive products over the observation space rather than a handful of
examples, because the failures this design exists to prevent all have the shape "one cell of the
table acted while something was unknown", and a cell nobody thought to write a test for is exactly
where that survives.
"""

import itertools

import pytest

import ownership as own

SINCE = "20260912T101500Z"
PORT = 8088
OURS = own.our_url(PORT)
FOREIGN_URL = "http://proxy.example.com/corp.pac"

OWNER = own.Owner(control_port=PORT, profile_fingerprint="ab12cd34ef56", state_root="/path/to/state")
OTHER_OWNER = own.Owner(control_port=9099, profile_fingerprint="0123456789ab", state_root="/other/state")
SERVICE = own.ServiceRef(name="Wi-Fi", device="en0")
OTHER_SERVICE = own.ServiceRef(name="Ethernet", device="en5")
BASELINE = own.Pac(url=FOREIGN_URL, enabled=True)
PROXY = own.Ref(pid=101, create_time=1000.5)
WATCHDOG = own.Ref(pid=102, create_time=1001.5)
STRANGER = own.Ref(pid=999, create_time=2000.5)


def record(phase, *, owner=OWNER, baseline=BASELINE, service=SERVICE, simulator=None):
    return own.SessionRecord(
        version=1, since=SINCE, simulator=simulator, owner=owner, service=service, baseline=baseline, phase=phase
    )


ARCHIVED_KNOWN = own.Archived(
    version=1,
    since=SINCE,
    reason="displaced",
    path="/path/to/archive/20260912T101500Z-displaced-ab12cd.json",
    context=own.Known(owner=OWNER, service=SERVICE, proxy=PROXY),
)
ARCHIVED_UNKNOWN = own.Archived(
    version=1,
    since=SINCE,
    reason="unreadable",
    path="/path/to/archive/20260912T101500Z-unreadable-ab12cd.json",
    context=own.Unknown(),
)


# MARK: - the classifier


@pytest.mark.parametrize(
    ("observed", "baseline", "expected"),
    [
        (own.PacUnreadable("networksetup failed"), own.Pac("", False), own.PacClass.UNREADABLE),
        (own.Pac(OURS, True), own.Pac(FOREIGN_URL, True), own.PacClass.OURS_ENABLED),
        (own.Pac(OURS, False), own.Pac(FOREIGN_URL, True), own.PacClass.OURS_DISABLED),
        # The overlap that matters most: our own disabled residue is at the Off target *and*
        # ours-disabled, and `classify` must answer the second so `_repair` can tell them apart.
        (own.Pac(OURS, False), own.Pac("", False), own.PacClass.OURS_DISABLED),
        (own.Pac(FOREIGN_URL, False), own.Pac(FOREIGN_URL, True), own.PacClass.RESUMABLE),
        (own.Pac(FOREIGN_URL, True), own.Pac(FOREIGN_URL, True), own.PacClass.AT_TARGET),
        (own.Pac("", False), own.Pac("", False), own.PacClass.AT_TARGET),
        (own.Pac(own.our_url(9099), False), own.Pac("", False), own.PacClass.AT_TARGET),
        (own.Pac("http://other.example.com/x.pac", True), own.Pac(FOREIGN_URL, True), own.PacClass.FOREIGN),
    ],
    ids=[
        "unreadable",
        "ours enabled",
        "ours disabled",
        "our residue over an empty baseline",
        "the baseline URL with the wrong flag",
        "the baseline itself",
        "no PAC over an empty baseline",
        "another port's disabled residue is off",
        "somebody else's PAC",
    ],
)
def test_classifier_returns_the_first_matching_class_in_priority(observed, baseline, expected):
    assert own.classify(observed, PORT, baseline) is expected


def test_classifier_reads_an_enabled_empty_url_as_resumable():
    """`networksetup` parses the URL and the flag independently, so `("", True)` is a reading. Over
    an empty baseline it is one `state off` from the target — read as FOREIGN, `down` would have
    archived a session whose PAC was its own."""
    assert own.classify(own.Pac("", True), PORT, own.Pac("", False)) is own.PacClass.RESUMABLE


def test_classifier_reads_an_enabled_residue_baseline_as_resumable():
    """A disabled Lyrebird-shaped baseline found enabled: the URL is the baseline's, the flag is
    not, so it is restored to the recorded flag — plan-v6's accepted limit."""
    baseline = own.Pac(own.our_url(9099), False)
    assert own.classify(own.Pac(own.our_url(9099), True), PORT, baseline) is own.PacClass.RESUMABLE


def test_satisfies_accepts_the_residue_a_down_leaves():
    """OURS_DISABLED outranks AT_TARGET, so a check spelled "must be AT_TARGET" would call every
    successful Off restore a failure and `down` would preserve forever."""
    assert own.satisfies(own.PacClass.OURS_DISABLED, own.Off()) is True
    assert own.satisfies(own.PacClass.OURS_DISABLED, own.Configured(FOREIGN_URL, True)) is False
    assert own.satisfies(own.PacClass.AT_TARGET, own.Off()) is True
    assert own.satisfies(own.PacClass.RESUMABLE, own.Off()) is False


@pytest.mark.parametrize(
    ("baseline", "expected"),
    [
        (own.Pac("", False), own.Off()),
        (own.Pac(own.our_url(9099), False), own.Off()),
        (own.Pac(FOREIGN_URL, True), own.Configured(FOREIGN_URL, True)),
        (own.Pac(FOREIGN_URL, False), own.Configured(FOREIGN_URL, False)),
    ],
    ids=["no PAC", "our own disabled residue", "a PAC in use", "a PAC configured but off"],
)
def test_restore_target_says_what_restored_means(baseline, expected):
    assert own.restore_target(baseline) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://127.0.0.1:8088/proxy.pac?x=1",
        "http://127.0.0.1:8088/other.pac",
        "https://127.0.0.1:8088/proxy.pac",
        "http://localhost:8088/proxy.pac",
        "http://127.0.0.1:0/proxy.pac",
        "http://127.0.0.1:99999/proxy.pac",
        "http://example.com/http://127.0.0.1:8088/proxy.pac",
        " http://127.0.0.1:8088/proxy.pac",
    ],
    ids=[
        "empty",
        "a query",
        "another path",
        "https",
        "by name",
        "port zero",
        "port out of range",
        "ours inside somebody else's path",
        "leading space",
    ],
)
def test_lyrebird_port_is_a_strict_parse(url):
    assert own.lyrebird_port(url) is None


def test_lyrebird_port_reads_any_port_of_ours():
    assert own.lyrebird_port(own.our_url(8088)) == 8088
    assert own.lyrebird_port(own.our_url(9099)) == 9099


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        (own.PacUnreadable("x"), own.UnownedClass.UNREADABLE),
        (own.Pac("", False), own.UnownedClass.EMPTY),
        (own.Pac("", True), own.UnownedClass.EMPTY),
        (own.Pac(own.our_url(9099), True), own.UnownedClass.LYREBIRD_ENABLED),
        (own.Pac(own.our_url(9099), False), own.UnownedClass.LYREBIRD_DISABLED),
        (own.Pac(FOREIGN_URL, True), own.UnownedClass.OTHER),
    ],
)
def test_classify_unowned_reads_any_port_as_a_lyrebird_pac(observed, expected):
    assert own.classify_unowned(observed) is expected


# MARK: - encode / decode


def payload(**overrides):
    data = own.encode(record(own.Active(PROXY, WATCHDOG)))
    data.update(overrides)
    return data


def test_encode_decode_roundtrip():
    records = [
        record(own.Acquiring(None)),
        record(own.Acquiring(PROXY)),
        record(own.Active(PROXY, WATCHDOG), simulator=own.Simulator(udid="SIM-1", name="iPhone 17")),
        record(own.Restored(None), baseline=own.Pac("", False)),
        record(own.Restored(PROXY), baseline=own.Pac(own.our_url(9099), False)),
        ARCHIVED_KNOWN,
        ARCHIVED_UNKNOWN,
    ]
    for subject in records:
        assert own.decode(own.encode(subject)) == subject


def test_decode_rejects_an_unknown_key():
    with pytest.raises(own.DecodeError, match="unknown extra"):
        own.decode(payload(extra=1))


def test_decode_rejects_a_missing_key():
    data = payload()
    del data["service"]
    with pytest.raises(own.DecodeError, match="missing service"):
        own.decode(data)


def test_decode_rejects_a_bool_where_an_int_is_expected():
    """`True` is an `int` in Python, and read as a pid of 1 it would have `down` signalling init."""
    with pytest.raises(own.DecodeError, match="phase.proxy.pid"):
        own.decode(
            payload(
                phase={
                    "kind": "active",
                    "proxy": {"pid": True, "createTime": 1.0},
                    "watchdog": {"pid": 2, "createTime": 1.0},
                }
            )
        )


def test_decode_rejects_a_non_positive_pid():
    """`psutil.Process(-1)` raises a plain `ValueError`, which the adapter does not map, so a
    negative pid must never reach it."""
    with pytest.raises(own.DecodeError, match="expected a pid of 1 or more"):
        own.decode(
            payload(
                phase={
                    "kind": "active",
                    "proxy": {"pid": 0, "createTime": 1.0},
                    "watchdog": {"pid": 2, "createTime": 1.0},
                }
            )
        )


def test_decode_rejects_a_create_time_that_is_not_a_finite_float():
    with pytest.raises(own.DecodeError, match="createTime"):
        own.decode(
            payload(
                phase={
                    "kind": "active",
                    "proxy": {"pid": 1, "createTime": "1.0"},
                    "watchdog": {"pid": 2, "createTime": 1.0},
                }
            )
        )
    with pytest.raises(own.DecodeError, match="finite"):
        own.decode(
            payload(
                phase={
                    "kind": "active",
                    "proxy": {"pid": 1, "createTime": float("inf")},
                    "watchdog": {"pid": 2, "createTime": 1.0},
                }
            )
        )


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_decode_rejects_a_control_port_out_of_range(port):
    with pytest.raises(own.DecodeError, match="is not a port"):
        own.decode(payload(owner={"controlPort": port, "profileFingerprint": "a", "stateRoot": "/x"}))


def test_decode_rejects_an_overlong_string():
    with pytest.raises(own.DecodeError, match="longer than"):
        own.decode(payload(owner={"controlPort": PORT, "profileFingerprint": "a" * 4097, "stateRoot": "/x"}))


def test_decode_rejects_a_phase_payload_that_is_not_its_own():
    with pytest.raises(own.DecodeError, match="missing watchdog"):
        own.decode(payload(phase={"kind": "active", "proxy": {"pid": 1, "createTime": 1.0}}))
    with pytest.raises(own.DecodeError, match="unknown watchdog"):
        own.decode(payload(phase={"kind": "acquiring", "proxy": None, "watchdog": {"pid": 2, "createTime": 1.0}}))
    with pytest.raises(own.DecodeError, match="phase.kind"):
        own.decode(payload(phase={"kind": "wandering", "proxy": None}))


def test_decode_rejects_an_enabled_lyrebird_baseline():
    """A record carrying `(our_url, True)` as the baseline would make `decide_down` answer `Restore`
    for a target `satisfies` can never hold for, so every `down` would preserve forever."""
    with pytest.raises(own.DecodeError, match="enabled Lyrebird PAC URL"):
        own.decode(payload(baseline={"url": own.our_url(9099), "enabled": True}))
    with pytest.raises(own.DecodeError, match="enabled Lyrebird PAC URL"):
        record(own.Acquiring(None), baseline=own.Pac(own.our_url(9099), True))


def test_decode_rejects_an_enabled_empty_baseline():
    """`("", True)` as a baseline would make the satisfied reading `("", False)` classify as
    RESUMABLE forever, and `down` would keep switching off a PAC that is already off."""
    with pytest.raises(own.DecodeError, match="may not be enabled"):
        own.decode(payload(baseline={"url": "", "enabled": True}))
    with pytest.raises(own.DecodeError, match="may not be enabled"):
        record(own.Acquiring(None), baseline=own.Pac("", True))


def test_decode_rejects_an_archive_whose_context_contradicts_its_reason():
    """`unreadable` ⇔ `Unknown`, `displaced`/`service-gone` ⇔ `Known`: an `unreadable` archive with
    a context would skip the sweep, and a `displaced` one without would sweep over a baseline
    somebody knows."""
    known = own.encode(ARCHIVED_KNOWN)
    with pytest.raises(own.DecodeError, match="may not carry"):
        own.decode({**known, "reason": "unreadable"})
    unknown = own.encode(ARCHIVED_UNKNOWN)
    with pytest.raises(own.DecodeError, match="may not carry"):
        own.decode({**unknown, "reason": "displaced"})


def test_decode_rejects_an_unknown_reason_or_context():
    with pytest.raises(own.DecodeError, match="reason"):
        own.decode({**own.encode(ARCHIVED_KNOWN), "reason": "misplaced"})
    with pytest.raises(own.DecodeError, match="context.kind"):
        own.decode({**own.encode(ARCHIVED_UNKNOWN), "context": {"kind": "partial"}})


def test_decode_rejects_a_since_that_is_not_a_timestamp():
    """`since` prefixes an archive's file name; a value with a path separator in it would be a
    component `archive()` never meant to accept."""
    with pytest.raises(own.DecodeError, match="YYYYMMDDTHHMMSSZ"):
        own.decode(payload(since="../../etc"))


@pytest.mark.parametrize("data", [None, [], "record", 1])
def test_decode_rejects_a_top_level_that_is_not_an_object(data):
    with pytest.raises(own.DecodeError):
        own.decode(data)


def test_decode_rejects_another_version():
    with pytest.raises(own.DecodeError, match="version"):
        own.decode(payload(version=2))


# MARK: - the observation space the decision tests range over

PACS = [*own.PacClass, *own.UnownedClass, own.NotObserved()]
ROUTES = [
    own.On(SERVICE),
    own.On(OTHER_SERVICE),
    own.NoRoute(),
    own.RouteAmbiguous(),
    own.RouteFailed("route failed"),
    own.NotObserved(),
]
SERVICES = [own.Present("Wi-Fi"), own.Gone(), own.ServiceAmbiguous(), own.ServiceFailed("no answer"), own.NotObserved()]
HEALTHS = [
    own.Silent(),
    own.Answering(pid=PROXY.pid, fingerprint=None, journal_error=None),
    own.Answering(pid=PROXY.pid, fingerprint=OWNER.profile_fingerprint, journal_error=None),
    own.Answering(pid=PROXY.pid, fingerprint=OTHER_OWNER.profile_fingerprint, journal_error=None),
    own.Answering(pid=PROXY.pid, fingerprint=None, journal_error="session.json cannot be read"),
    own.Answering(pid=STRANGER.pid, fingerprint=None, journal_error=None),
    own.Answering(pid=None, fingerprint=None, journal_error=None),
]
LIVENESSES = [*own.Liveness, own.NotObserved()]
SCANS = [
    own.Complete(()),
    own.Complete((own.Marked(ref=STRANGER, kind="proxy", port=9099, cmdline=("mitmdump",)),)),
    own.Incomplete(),
]
JOURNALS = [
    own.Absent(),
    own.Unreadable("not JSON"),
    record(own.Acquiring(None)),
    record(own.Acquiring(PROXY)),
    record(own.Active(PROXY, WATCHDOG)),
    record(own.Restored(PROXY)),
    ARCHIVED_KNOWN,
    ARCHIVED_UNKNOWN,
]

_ACTS_ON_THE_PAC = (own.Proceed, own.Idempotent, own.RepairOwnPac, own.Restore, own.AlreadyRestored)
_DECISIONS = (
    own.Proceed,
    own.Idempotent,
    own.RepairOwnPac,
    own.Restore,
    own.AlreadyRestored,
    own.Archive,
    own.Sweep,
    own.Exit,
    own.Refuse,
    own.Preserve,
)


def holding(journal):
    """The phases that hold an obligation: a proxy is named and the PAC may be ours."""
    if not isinstance(journal, own.SessionRecord):
        return None
    phase = journal.phase
    if isinstance(phase, own.Active):
        return phase.proxy
    if isinstance(phase, own.Acquiring) and phase.proxy is not None:
        return phase.proxy
    return None


def test_decide_up_is_total_and_safe():
    """Every cell answers, and no cell acts on the PAC while something it consults is unknown."""
    cells = 0
    for journal, route, pac, health, proxy, watchdog, scan, requested in itertools.product(
        JOURNALS, ROUTES, PACS, HEALTHS, LIVENESSES, LIVENESSES, SCANS, (OWNER, OTHER_OWNER)
    ):
        obs = own.UpObs(
            journal=journal,
            requested=requested,
            route=route,
            pac=pac,
            health=health,
            proxy=proxy,
            watchdog=watchdog,
            scan=scan,
        )
        decision = own.decide_up(obs)
        cells += 1
        assert isinstance(decision, _DECISIONS)
        assert decision == own.decide_up(obs), "the same observation must always decide the same way"
        if isinstance(decision, (own.Refuse, own.Preserve)):
            assert decision.reason
        if isinstance(journal, own.Unreadable):
            assert isinstance(decision, own.Refuse)
        if not isinstance(decision, _ACTS_ON_THE_PAC):
            continue
        # Whatever the row, acting means the PAC was read and the route is the journalled one.
        assert isinstance(route, own.On)
        assert pac not in (own.PacClass.UNREADABLE, own.UnownedClass.UNREADABLE)
        assert not isinstance(pac, own.NotObserved)
        if isinstance(journal, own.Absent):
            assert isinstance(decision, own.Proceed)
            assert scan == own.Complete(())
            assert isinstance(pac, own.UnownedClass)
        else:
            assert isinstance(journal, own.SessionRecord) and isinstance(journal.phase, own.Active)
            assert journal.owner == requested
            assert route.service.device == journal.service.device
            assert proxy is own.Liveness.ALIVE and watchdog is own.Liveness.ALIVE
            assert isinstance(health, own.Answering)
            assert health.pid == journal.phase.proxy.pid
            assert health.journal_error is None
            assert health.fingerprint in (None, journal.owner.profile_fingerprint)
            assert isinstance(pac, own.PacClass)
    assert cells > 1000


def test_decide_up_preserves_when_the_proxy_reports_the_journal_broken():
    """A repair over a journal the proxy says it cannot read would re-enable a PAC whose baseline
    may be gone — today's fatal case, kept."""
    decision = own.decide_up(
        own.UpObs(
            journal=record(own.Active(PROXY, WATCHDOG)),
            requested=OWNER,
            route=own.On(SERVICE),
            pac=own.PacClass.OURS_ENABLED,
            health=own.Answering(PROXY.pid, OWNER.profile_fingerprint, "session.json cannot be read"),
            proxy=own.Liveness.ALIVE,
            watchdog=own.Liveness.ALIVE,
            scan=own.Complete(()),
        )
    )
    assert isinstance(decision, own.Preserve)


def test_decide_up_refuses_a_moved_route_even_when_the_new_services_pac_is_unreadable():
    """`active_service()` names the *new* route's service; the PAC is read on neither, so an
    unreadable one must not turn the moved-route refusal into a Preserve."""
    decision = own.decide_up(
        own.UpObs(
            journal=record(own.Active(PROXY, WATCHDOG)),
            requested=OWNER,
            route=own.On(OTHER_SERVICE),
            pac=own.NotObserved(),
            health=own.Answering(PROXY.pid, None, None),
            proxy=own.Liveness.ALIVE,
            watchdog=own.Liveness.ALIVE,
            scan=own.Complete(()),
        )
    )
    assert isinstance(decision, own.Refuse)
    assert "moved" in decision.reason


def test_decide_up_refuses_a_marked_process_with_no_session():
    decision = own.decide_up(
        own.UpObs(
            journal=own.Absent(),
            requested=OWNER,
            route=own.On(SERVICE),
            pac=own.UnownedClass.EMPTY,
            health=own.Silent(),
            proxy=own.NotObserved(),
            watchdog=own.NotObserved(),
            scan=own.Complete((own.Marked(STRANGER, "proxy", 9099, ("mitmdump",)),)),
        )
    )
    assert isinstance(decision, own.Refuse)
    assert "9099" in decision.reason


def test_decide_down_is_total_and_safe():
    cells = 0
    for journal, service, pac, health, proxy, watchdog, scan in itertools.product(
        JOURNALS,
        SERVICES,
        [*own.PacClass, own.NotObserved()],
        HEALTHS + [own.NotObserved()],
        LIVENESSES,
        LIVENESSES,
        SCANS,
    ):
        obs = own.DownObs(
            journal=journal, service=service, pac=pac, health=health, proxy=proxy, watchdog=watchdog, scan=scan
        )
        decision = own.decide_down(obs)
        cells += 1
        assert isinstance(decision, _DECISIONS)
        assert decision == own.decide_down(obs)
        if isinstance(journal, own.Unreadable):
            # Whatever else was observed: the bytes are preserved and the record replaced.
            assert decision == own.Archive("unreadable")
            continue
        if isinstance(decision, own.Sweep):
            assert isinstance(journal, own.Absent) or (
                isinstance(journal, own.Archived) and isinstance(journal.context, own.Unknown)
            )
            assert isinstance(scan, own.Complete)
        proxy_ref = holding(journal)
        if proxy_ref is not None:
            if isinstance(decision, own.Archive):
                assert (decision.reason == "service-gone" and isinstance(service, own.Gone)) or (
                    decision.reason == "displaced" and pac is own.PacClass.FOREIGN
                )
            if isinstance(decision, _ACTS_ON_THE_PAC):
                assert isinstance(service, own.Present)
                assert isinstance(pac, own.PacClass) and pac is not own.PacClass.UNREADABLE
                assert proxy is own.Liveness.ALIVE or not isinstance(health, own.Answering)
                # The property: a health pid that is not the *live* recorded ref never restores.
                if isinstance(health, own.Answering):
                    assert health.pid == proxy_ref.pid and proxy is own.Liveness.ALIVE
                if isinstance(journal.phase, own.Active):
                    assert watchdog is not own.Liveness.UNKNOWN
                    assert not isinstance(watchdog, own.NotObserved)
                assert proxy is not own.Liveness.UNKNOWN
    assert cells > 1000


def test_decide_down_archives_a_foreign_pac_even_when_a_stranger_answers():
    """A stranger on the port must not keep open an obligation the observation has already ended:
    a later `down`, after that listener is gone and the PAC has become resumable, would otherwise
    restore what plan-v6 said to archive."""
    decision = own.decide_down(
        own.DownObs(
            journal=record(own.Active(PROXY, WATCHDOG)),
            service=own.Present("Wi-Fi"),
            pac=own.PacClass.FOREIGN,
            health=own.Answering(STRANGER.pid, None, None),
            proxy=own.Liveness.ALIVE,
            watchdog=own.Liveness.ALIVE,
            scan=own.Complete(()),
        )
    )
    assert decision == own.Archive("displaced")


def test_decide_down_refuses_a_reused_pid_that_answers():
    """The recorded pid answers while the ref is proven dead: the number was reused by something
    else that happens to listen on the port, and restoring would pull the PAC out from under it."""
    decision = own.decide_down(
        own.DownObs(
            journal=record(own.Active(PROXY, WATCHDOG)),
            service=own.Present("Wi-Fi"),
            pac=own.PacClass.OURS_ENABLED,
            health=own.Answering(PROXY.pid, None, None),
            proxy=own.Liveness.PROVEN_DEAD,
            watchdog=own.Liveness.PROVEN_DEAD,
            scan=own.Complete(()),
        )
    )
    assert isinstance(decision, own.Refuse)
    assert str(PORT) in decision.reason


def test_decide_down_restores_when_the_recorded_proxy_is_silent_and_dead():
    decision = own.decide_down(
        own.DownObs(
            journal=record(own.Active(PROXY, WATCHDOG)),
            service=own.Present("Wi-Fi"),
            pac=own.PacClass.OURS_ENABLED,
            health=own.Silent(),
            proxy=own.Liveness.PROVEN_DEAD,
            watchdog=own.Liveness.PROVEN_DEAD,
            scan=own.Complete(()),
        )
    )
    assert decision == own.Restore(own.Configured(FOREIGN_URL, True))


def test_decide_down_on_acquiring_none_waits_for_nothing_but_a_whole_scan():
    absent_proxy = record(own.Acquiring(None))
    facts = {
        "service": own.NotObserved(),
        "pac": own.NotObserved(),
        "health": own.NotObserved(),
        "proxy": own.NotObserved(),
        "watchdog": own.NotObserved(),
    }
    assert isinstance(own.decide_down(own.DownObs(journal=absent_proxy, scan=own.Complete(()), **facts)), own.Proceed)
    assert isinstance(own.decide_down(own.DownObs(journal=absent_proxy, scan=own.Incomplete(), **facts)), own.Preserve)


def test_decide_watchdog_is_total_and_safe():
    cells = 0
    for journal, service, pac, health, proxy, me in itertools.product(
        JOURNALS,
        SERVICES,
        [*own.PacClass, own.NotObserved()],
        HEALTHS,
        LIVENESSES,
        (WATCHDOG, STRANGER),
    ):
        obs = own.WatchdogObs(journal=journal, me=me, service=service, pac=pac, health=health, proxy=proxy)
        decision = own.decide_watchdog(obs)
        cells += 1
        assert isinstance(decision, _DECISIONS)
        assert decision == own.decide_watchdog(obs)
        active = isinstance(journal, own.SessionRecord) and isinstance(journal.phase, own.Active)
        if not active or journal.phase.watchdog != me:
            assert isinstance(decision, own.Exit)
            continue
        if isinstance(decision, (own.Restore, own.RepairOwnPac)):
            assert isinstance(service, own.Present)
            assert isinstance(pac, own.PacClass) and pac is not own.PacClass.UNREADABLE
        if isinstance(decision, own.RepairOwnPac):
            # The repair arm is the one that consults liveness: it acts only on a proxy that both
            # answers and is provably alive.
            assert isinstance(health, own.Answering) and health.pid == journal.phase.proxy.pid
            assert proxy is own.Liveness.ALIVE
    assert cells > 1000


@pytest.mark.parametrize("proxy", list(own.Liveness))
@pytest.mark.parametrize(
    "pac", [own.PacClass.OURS_ENABLED, own.PacClass.OURS_DISABLED, own.PacClass.RESUMABLE, own.PacClass.AT_TARGET]
)
def test_decide_watchdog_restores_for_every_liveness(proxy, pac):
    """The silent row does not consult the proxy's liveness: whatever it says, the Mac must not
    stay routed at a port nothing answers on. A satisfied target makes the recipe a read-back with
    zero writes, which is why AT_TARGET is in the list."""
    decision = own.decide_watchdog(
        own.WatchdogObs(
            journal=record(own.Active(PROXY, WATCHDOG)),
            me=WATCHDOG,
            service=own.Present("Wi-Fi"),
            pac=pac,
            health=own.Silent(),
            proxy=proxy,
        )
    )
    assert decision == own.Restore(own.Configured(FOREIGN_URL, True))


def test_decide_watchdog_exits_when_a_reused_pid_answers():
    decision = own.decide_watchdog(
        own.WatchdogObs(
            journal=record(own.Active(PROXY, WATCHDOG)),
            me=WATCHDOG,
            service=own.Present("Wi-Fi"),
            pac=own.PacClass.OURS_ENABLED,
            health=own.Answering(PROXY.pid, None, None),
            proxy=own.Liveness.PROVEN_DEAD,
        )
    )
    assert isinstance(decision, own.Exit)


def test_decide_watchdog_keeps_ticking_when_the_service_is_gone():
    """A service that is gone is a temporary condition to a watchdog; only `down` archives."""
    decision = own.decide_watchdog(
        own.WatchdogObs(
            journal=record(own.Active(PROXY, WATCHDOG)),
            me=WATCHDOG,
            service=own.Gone(),
            pac=own.NotObserved(),
            health=own.Silent(),
            proxy=own.Liveness.UNKNOWN,
        )
    )
    assert isinstance(decision, own.Preserve)


def test_decide_release_is_total_and_only_releases_a_closed_session():
    cells = 0
    sweeps = [
        own.SweepClean(False),
        own.SweepClean(True),
        own.SweepLeft((8088,)),
        own.SweepFailed("x"),
        own.NotObserved(),
    ]
    for journal, proxy, watchdog, health, sweep, scan in itertools.product(
        JOURNALS, LIVENESSES, LIVENESSES, HEALTHS + [own.NotObserved()], sweeps, SCANS
    ):
        obs = own.ReleaseObs(journal=journal, proxy=proxy, watchdog=watchdog, health=health, sweep=sweep, scan=scan)
        decision = own.decide_release(obs)
        cells += 1
        assert isinstance(decision, (own.ReleaseNow, own.KeepJournal))
        assert decision == own.decide_release(obs)
        if isinstance(decision, own.KeepJournal):
            assert decision.reason
            continue
        assert scan == own.Complete(())
        if isinstance(journal, own.SessionRecord):
            phase = journal.phase
            assert isinstance(phase, (own.Restored, own.Acquiring))
            if isinstance(phase, own.Acquiring):
                assert phase.proxy is None
            if getattr(phase, "proxy", None) is not None:
                assert proxy is own.Liveness.PROVEN_DEAD
            assert isinstance(health, own.Silent)
        elif isinstance(journal, own.Archived):
            if isinstance(journal.context, own.Known):
                assert isinstance(health, own.Silent)
                if journal.context.proxy is not None:
                    assert proxy is own.Liveness.PROVEN_DEAD
            else:
                assert sweep in (own.SweepClean(False), own.SweepClean(True))
        else:
            assert isinstance(journal, own.Absent)
            assert sweep in (own.SweepClean(False), own.SweepClean(True))
    assert cells > 1000


def test_decide_release_keeps_the_journal_while_a_ref_is_not_proven_dead():
    for liveness in (own.Liveness.ALIVE, own.Liveness.UNKNOWN, own.NotObserved()):
        decision = own.decide_release(
            own.ReleaseObs(
                journal=record(own.Restored(PROXY)),
                proxy=liveness,
                watchdog=own.NotObserved(),
                health=own.Silent(),
                sweep=own.NotObserved(),
                scan=own.Complete(()),
            )
        )
        assert isinstance(decision, own.KeepJournal)


def test_down_decides_archive_unreadable_in_the_pure_core():
    """A journal that cannot be read decides its own fate here, never in the executor: whatever
    else was observed, the bytes are preserved and the record replaced.

    Left to `down`, "is this the unreadable row?" would be a consequential decision taken outside
    the pure core — and the one row where every other fact is irrelevant is exactly the one an
    executor would be tempted to shortcut.
    """
    for service, pac, health, scan in itertools.product(SERVICES, [*own.PacClass, own.NotObserved()], HEALTHS, SCANS):
        decision = own.decide_down(
            own.DownObs(
                journal=own.Unreadable("expected an object, found list"),
                service=service,
                pac=pac,
                health=health,
                proxy=own.NotObserved(),
                watchdog=own.NotObserved(),
                scan=scan,
            )
        )
        assert decision == own.Archive("unreadable"), (service, pac, health, scan)
