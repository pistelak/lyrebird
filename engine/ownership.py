"""The pure core of PAC ownership: what the session journal says, what was observed, what to do.

No IO here, and no `os`, `subprocess`, `psutil`, `netproxy` or `config` imports — the `rules.py`
house rule, for the same reason: this is the module that decides whether somebody's proxy settings
are touched, and it has to be provable with the standard library alone. Everything below takes
facts that were already read and returns a value; the decoder takes an already-parsed `object`,
and `session.py` owns `json.loads`.

Three ideas run through it:

* one journal record per user describes the session and the phase it is in;
* an *observation* is a set of facts, each of which may be "not observed" or "could not be read",
  and neither of those is ever the same value as a fact that was read;
* a *decision* is taken here, never in an executor, so "uncertainty acts on nothing" is one thing
  to read rather than a rule spread over four commands.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

SESSION_VERSION = 1

# Bounds the decoder enforces. A journal is written by this process and read by the next one; a
# string without a bound is a `down` that reads a megabyte of somebody's hand-edited file into an
# error message — see test_decode_rejects_an_overlong_string.
MAX_STRING = 4096
MAX_PORT = 65535

# `since` prefixes every archive file name, so a value that is not this shape becomes a path
# component `session.archive` would have to sanitise; refusing it here keeps that one rule in one
# place — see test_decode_rejects_a_since_that_is_not_a_timestamp.
SINCE_PATTERN = re.compile(r"^\d{8}T\d{6}Z$")

# The PAC URL Lyrebird installs, on any port. Anchored and strict: a URL that merely contains ours
# is somebody else's — see test_lyrebird_port_is_a_strict_parse.
_OUR_URL = re.compile(r"^http://127\.0\.0\.1:(\d{1,5})/proxy\.pac$")

Marker = Literal["proxy", "watchdog"]
Reason = Literal["displaced", "service-gone", "unreadable"]
REASONS: tuple[Reason, ...] = ("displaced", "service-gone", "unreadable")


class DecodeError(ValueError):
    """A journal payload this reader will not accept, and why."""


# MARK: - The values a record is made of


@dataclass(frozen=True, slots=True)
class Ref:
    """A process as `psutil` reports it: the pid and the create time, together.

    Never a bare pid: a pid is reused, and signalling on one alone is how `down` killed whatever
    inherited the number — see test_kill_now_never_signals_a_reused_pid.
    """

    pid: int
    create_time: float


@dataclass(frozen=True, slots=True)
class Owner:
    """Who a session belongs to. All three fields, because any one of them alone repeats."""

    control_port: int
    profile_fingerprint: str
    state_root: str


@dataclass(frozen=True, slots=True)
class ServiceRef:
    """A network service by name *and* device. The name is what `networksetup` takes; the device is
    what survives a rename, which is why both are recorded."""

    name: str
    device: str


@dataclass(frozen=True, slots=True)
class Pac:
    """A PAC reading, verbatim: `networksetup` reports the URL and the flag independently, so every
    pair it can print must be representable here. The invariants live on the `baseline` slot of a
    record, not on this type."""

    url: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class Simulator:
    """A device `simctl` commands are addressed to *by UDID* — never by `booted`."""

    udid: str
    name: str

    def __str__(self) -> str:
        return f"{self.name} ({self.udid})"


@dataclass(frozen=True, slots=True)
class Acquiring:
    """The session is taking the PAC: a proxy may have been spawned, nothing is installed yet."""

    proxy: Ref | None


@dataclass(frozen=True, slots=True)
class Active:
    proxy: Ref
    watchdog: Ref


@dataclass(frozen=True, slots=True)
class Restored:
    """The obligation is discharged: the baseline is back, durably, and nothing may touch the PAC
    on this record's behalf again."""

    proxy: Ref | None


Phase = Acquiring | Active | Restored


def _is_lyrebird_baseline(baseline: Pac) -> bool:
    return lyrebird_port(baseline.url) is not None


def _check_baseline(baseline: Pac) -> None:
    """The two shapes that are readings but never baselines.

    An *enabled* Lyrebird URL as a baseline would make `decide_down` answer `Restore` for a target
    `satisfies` can never hold for, so every `down` would preserve forever
    (test_decode_rejects_an_enabled_lyrebird_baseline); `("", True)` would make the satisfied
    reading `("", False)` classify as RESUMABLE forever
    (test_decode_rejects_an_enabled_empty_baseline).
    """
    if baseline.enabled and _is_lyrebird_baseline(baseline):
        raise DecodeError("a baseline may not be an enabled Lyrebird PAC URL")
    if baseline.enabled and baseline.url == "":
        raise DecodeError("a baseline with no URL may not be enabled")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    version: int
    since: str
    simulator: Simulator | None
    owner: Owner
    service: ServiceRef
    baseline: Pac
    phase: Phase

    def __post_init__(self) -> None:
        _check_baseline(self.baseline)


@dataclass(frozen=True, slots=True)
class Known:
    """What an archived session still knows: enough to ask its port and its service about it."""

    owner: Owner
    service: ServiceRef
    proxy: Ref | None


@dataclass(frozen=True, slots=True)
class Unknown:
    pass


Context = Known | Unknown


@dataclass(frozen=True, slots=True)
class Archived:
    version: int
    since: str
    reason: Reason
    path: str
    context: Context

    def __post_init__(self) -> None:
        # Coupled by construction: `Archived("unreadable", Known(...))` would skip the mandatory
        # sweep and `Archived("displaced", Unknown())` would sweep over a baseline somebody knows —
        # see test_decode_rejects_an_archive_whose_context_contradicts_its_reason.
        unreadable = self.reason == "unreadable"
        if unreadable is isinstance(self.context, Known):
            raise DecodeError(f"an archive for {self.reason!r} may not carry {type(self.context).__name__}")


@dataclass(frozen=True, slots=True)
class Absent:
    pass


@dataclass(frozen=True, slots=True)
class Unreadable:
    """The journal is there and could not be read. Not `Absent`: a record that cannot be read may
    still be holding somebody's PAC — see test_read_reports_a_directory_as_unreadable."""

    reason: str


Journal = Absent | Unreadable | SessionRecord | Archived


@dataclass(frozen=True, slots=True)
class PacUnreadable:
    """A PAC read that raised. Distinct from the journal's `Unreadable`, and from any `Pac`."""

    reason: str


Observed = Pac | PacUnreadable


# MARK: - Encode / decode


def _ref(ref: Ref) -> dict[str, object]:
    return {"pid": ref.pid, "createTime": ref.create_time}


def _owner(owner: Owner) -> dict[str, object]:
    return {
        "controlPort": owner.control_port,
        "profileFingerprint": owner.profile_fingerprint,
        "stateRoot": owner.state_root,
    }


def _service(service: ServiceRef) -> dict[str, object]:
    return {"name": service.name, "device": service.device}


def _phase(phase: Phase) -> dict[str, object]:
    match phase:
        case Acquiring(proxy):
            return {"kind": "acquiring", "proxy": _ref(proxy) if proxy else None}
        case Active(proxy, watchdog):
            return {"kind": "active", "proxy": _ref(proxy), "watchdog": _ref(watchdog)}
        case Restored(proxy):
            return {"kind": "restored", "proxy": _ref(proxy) if proxy else None}


def encode(record: SessionRecord | Archived) -> dict[str, object]:
    if isinstance(record, SessionRecord):
        return {
            "version": record.version,
            "kind": "session",
            "since": record.since,
            "owner": _owner(record.owner),
            "service": _service(record.service),
            "baseline": {"url": record.baseline.url, "enabled": record.baseline.enabled},
            "simulator": (
                None if record.simulator is None else {"udid": record.simulator.udid, "name": record.simulator.name}
            ),
            "phase": _phase(record.phase),
        }
    context: dict[str, object]
    if isinstance(record.context, Known):
        context = {
            "kind": "known",
            "owner": _owner(record.context.owner),
            "service": _service(record.context.service),
            "proxy": _ref(record.context.proxy) if record.context.proxy else None,
        }
    else:
        context = {"kind": "unknown"}
    return {
        "version": record.version,
        "kind": "archived",
        "since": record.since,
        "reason": record.reason,
        "path": record.path,
        "context": context,
    }


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DecodeError(f"{where}: expected an object, found {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise DecodeError(f"{where}: a key that is not a string")
    return value


def _exact(data: dict[str, object], keys: tuple[str, ...], where: str) -> None:
    """Exactly these keys. A missing one is a record half this reader's shape; an unknown one is a
    record written by something that knows more than we do, and guessing at either is how a journal
    from a newer engine gets acted on — see test_decode_rejects_an_unknown_key."""
    found = set(data)
    expected = set(keys)
    missing = sorted(expected - found)
    unknown = sorted(found - expected)
    if missing:
        raise DecodeError(f"{where}: missing {', '.join(missing)}")
    if unknown:
        raise DecodeError(f"{where}: unknown {', '.join(unknown)}")


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise DecodeError(f"{where}: expected a string, found {type(value).__name__}")
    if len(value) > MAX_STRING:
        raise DecodeError(f"{where}: longer than {MAX_STRING} characters")
    return value


def _integer(value: object, where: str) -> int:
    # `bool` is an `int` in Python, and `True` read as a pid of 1 would have `down` signalling
    # init — see test_decode_rejects_a_bool_where_an_int_is_expected.
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecodeError(f"{where}: expected an integer, found {type(value).__name__}")
    return value


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise DecodeError(f"{where}: expected a boolean, found {type(value).__name__}")
    return value


def _create_time(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, float):
        raise DecodeError(f"{where}: expected a float, found {type(value).__name__}")
    if not math.isfinite(value):
        raise DecodeError(f"{where}: expected a finite float, found {value!r}")
    return value


def _decode_ref(value: object, where: str) -> Ref:
    data = _object(value, where)
    _exact(data, ("pid", "createTime"), where)
    pid = _integer(data["pid"], f"{where}.pid")
    if pid < 1:
        # `psutil.Process(-1)` raises a plain `ValueError`, which the adapter does not map: a
        # non-positive pid must never reach it — see test_decode_rejects_a_non_positive_pid.
        raise DecodeError(f"{where}.pid: expected a pid of 1 or more, found {pid}")
    return Ref(pid=pid, create_time=_create_time(data["createTime"], f"{where}.createTime"))


def _decode_optional_ref(value: object, where: str) -> Ref | None:
    return None if value is None else _decode_ref(value, where)


def _decode_owner(value: object, where: str) -> Owner:
    data = _object(value, where)
    _exact(data, ("controlPort", "profileFingerprint", "stateRoot"), where)
    port = _integer(data["controlPort"], f"{where}.controlPort")
    if not 1 <= port <= MAX_PORT:
        raise DecodeError(f"{where}.controlPort: {port} is not a port")
    return Owner(
        control_port=port,
        profile_fingerprint=_string(data["profileFingerprint"], f"{where}.profileFingerprint"),
        state_root=_string(data["stateRoot"], f"{where}.stateRoot"),
    )


def _decode_service(value: object, where: str) -> ServiceRef:
    data = _object(value, where)
    _exact(data, ("name", "device"), where)
    return ServiceRef(name=_string(data["name"], f"{where}.name"), device=_string(data["device"], f"{where}.device"))


def _decode_phase(value: object) -> Phase:
    data = _object(value, "phase")
    kind = data.get("kind")
    if kind == "acquiring":
        _exact(data, ("kind", "proxy"), "phase")
        return Acquiring(proxy=_decode_optional_ref(data["proxy"], "phase.proxy"))
    if kind == "active":
        # The payload is exactly what the phase owns: an `active` without a watchdog, or an
        # `acquiring` with one, is a record no writer here produces — see
        # test_decode_rejects_a_phase_payload_that_is_not_its_own.
        _exact(data, ("kind", "proxy", "watchdog"), "phase")
        return Active(
            proxy=_decode_ref(data["proxy"], "phase.proxy"),
            watchdog=_decode_ref(data["watchdog"], "phase.watchdog"),
        )
    if kind == "restored":
        _exact(data, ("kind", "proxy"), "phase")
        return Restored(proxy=_decode_optional_ref(data["proxy"], "phase.proxy"))
    raise DecodeError(f"phase.kind: expected acquiring, active or restored, found {kind!r}")


def _decode_simulator(value: object) -> Simulator | None:
    if value is None:
        return None
    data = _object(value, "simulator")
    _exact(data, ("udid", "name"), "simulator")
    return Simulator(udid=_string(data["udid"], "simulator.udid"), name=_string(data["name"], "simulator.name"))


def _decode_context(value: object) -> Context:
    data = _object(value, "context")
    kind = data.get("kind")
    if kind == "known":
        _exact(data, ("kind", "owner", "service", "proxy"), "context")
        return Known(
            owner=_decode_owner(data["owner"], "context.owner"),
            service=_decode_service(data["service"], "context.service"),
            proxy=_decode_optional_ref(data["proxy"], "context.proxy"),
        )
    if kind == "unknown":
        _exact(data, ("kind",), "context")
        return Unknown()
    raise DecodeError(f"context.kind: expected known or unknown, found {kind!r}")


def _decode_since(value: object) -> str:
    since = _string(value, "since")
    if not SINCE_PATTERN.match(since):
        raise DecodeError(f"since: expected YYYYMMDDTHHMMSSZ, found {since!r}")
    return since


def _decode_version(value: object) -> int:
    version = _integer(value, "version")
    if version != SESSION_VERSION:
        raise DecodeError(f"version: expected {SESSION_VERSION}, found {version}")
    return version


def decode(data: object) -> SessionRecord | Archived:
    """The one reader. Raises `DecodeError`; never returns a partially understood record.

    Strict on purpose: what this accepts, `down` acts on. A record it accepted loosely — an unknown
    key, a pid of `True`, a port of 0 — is a record something signalled or restored from.
    """
    top = _object(data, "record")
    kind = top.get("kind")
    if kind == "session":
        _exact(top, ("version", "kind", "since", "owner", "service", "baseline", "simulator", "phase"), "record")
        baseline_data = _object(top["baseline"], "baseline")
        _exact(baseline_data, ("url", "enabled"), "baseline")
        baseline = Pac(
            url=_string(baseline_data["url"], "baseline.url"),
            enabled=_boolean(baseline_data["enabled"], "baseline.enabled"),
        )
        _check_baseline(baseline)
        return SessionRecord(
            version=_decode_version(top["version"]),
            since=_decode_since(top["since"]),
            simulator=_decode_simulator(top["simulator"]),
            owner=_decode_owner(top["owner"], "owner"),
            service=_decode_service(top["service"], "service"),
            baseline=baseline,
            phase=_decode_phase(top["phase"]),
        )
    if kind == "archived":
        _exact(top, ("version", "kind", "since", "reason", "path", "context"), "record")
        reason = _string(top["reason"], "reason")
        if reason not in REASONS:
            raise DecodeError(f"reason: expected one of {', '.join(REASONS)}, found {reason!r}")
        return Archived(
            version=_decode_version(top["version"]),
            since=_decode_since(top["since"]),
            reason=reason,  # type: ignore[arg-type]  # checked against REASONS just above
            path=_string(top["path"], "path"),
            context=_decode_context(top["context"]),
        )
    raise DecodeError(f"kind: expected session or archived, found {kind!r}")


# MARK: - The classifier


class PacClass(Enum):
    """What an observed PAC is, relative to one port and one baseline. Ordered: `classify` answers
    the first that matches, and `satisfies` is the only predicate for "the obligation is met"."""

    UNREADABLE = "unreadable"
    OURS_ENABLED = "ours_enabled"
    OURS_DISABLED = "ours_disabled"
    RESUMABLE = "resumable"
    AT_TARGET = "at_target"
    FOREIGN = "foreign"


class UnownedClass(Enum):
    """What an observed PAC is when no journal claims it — `up`'s question."""

    UNREADABLE = "unreadable"
    EMPTY = "empty"
    LYREBIRD_ENABLED = "lyrebird_enabled"
    LYREBIRD_DISABLED = "lyrebird_disabled"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Off:
    """Restore to "no PAC in effect": macOS rejects an empty URL, so the only move is the flag."""


@dataclass(frozen=True, slots=True)
class Configured:
    url: str
    enabled: bool


Target = Off | Configured


def our_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/proxy.pac"


def lyrebird_port(url: str) -> int | None:
    """The port of a Lyrebird PAC URL, or None. Strict: the whole URL, the loopback host, our path,
    and a port in range — see test_lyrebird_port_is_a_strict_parse."""
    match = _OUR_URL.match(url)
    if match is None:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= MAX_PORT else None


def restore_target(baseline: Pac) -> Target:
    """What "restored" means for this baseline."""
    if baseline.url == "":
        return Off()
    if lyrebird_port(baseline.url) is not None and not baseline.enabled:
        # Disabled residue of an earlier session: putting the URL back would only re-point the Mac
        # at a port nobody holds. Off is the same end state, one command fewer.
        return Off()
    return Configured(url=baseline.url, enabled=baseline.enabled)


def classify(observed: Observed, port: int, baseline: Pac) -> PacClass:
    """The first matching class, in priority order. One function so the overlaps — a configured
    baseline is both untouched and at target; disabled residue is both ours-disabled and at the Off
    target — are resolved in one place: test_classifier_returns_the_first_matching_class_in_priority.
    """
    if isinstance(observed, PacUnreadable):
        return PacClass.UNREADABLE
    if observed.url == our_url(port):
        return PacClass.OURS_ENABLED if observed.enabled else PacClass.OURS_DISABLED
    if observed.url == baseline.url and observed.enabled != baseline.enabled:
        # plan-v6's definition verbatim, for every URL including the empty one: `("", True)` over a
        # `("", False)` baseline is one `state off` away from the target, not a stranger's PAC —
        # see test_classifier_reads_an_enabled_empty_url_as_resumable.
        return PacClass.RESUMABLE
    target = restore_target(baseline)
    if isinstance(target, Off):
        if not observed.enabled and (observed.url == "" or lyrebird_port(observed.url) is not None):
            return PacClass.AT_TARGET
    elif (observed.url, observed.enabled) == (target.url, target.enabled):
        return PacClass.AT_TARGET
    return PacClass.FOREIGN


def satisfies(cls: PacClass, target: Target) -> bool:
    """The one predicate for "the obligation is met".

    OURS_DISABLED outranks AT_TARGET in `classify`, so a check spelled "must be AT_TARGET" would
    call every successful Off restore a failure — see test_satisfies_accepts_the_residue_a_down_leaves.
    """
    return cls is PacClass.AT_TARGET or (cls is PacClass.OURS_DISABLED and isinstance(target, Off))


def classify_unowned(observed: Observed) -> UnownedClass:
    if isinstance(observed, PacUnreadable):
        return UnownedClass.UNREADABLE
    if observed.url == "":
        return UnownedClass.EMPTY
    if lyrebird_port(observed.url) is not None:
        # Any port: an enabled Lyrebird PAC with no journal is somebody's unowned session, and `up`
        # over it would strand it — see test_classify_unowned_reads_any_port_as_a_lyrebird_pac.
        return UnownedClass.LYREBIRD_ENABLED if observed.enabled else UnownedClass.LYREBIRD_DISABLED
    return UnownedClass.OTHER


# MARK: - Observations


class Liveness(Enum):
    ALIVE = "alive"
    PROVEN_DEAD = "proven_dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Silent:
    """Nothing answered on the port at all. Not "the proxy is dead": only the transport says this."""


@dataclass(frozen=True, slots=True)
class Answering:
    """Something answered on the port — any HTTP status at all.

    `pid` is None when the answer carried no usable one (an older engine, or a listener that is not
    Lyrebird), and None is never "the recorded proxy": every row that compares it treats None as a
    pid that is not it. `journal_error` is the proxy's own `journalError`, the one existing check
    that `up`'s final look already makes.
    """

    pid: int | None
    fingerprint: str | None
    journal_error: str | None


Health = Silent | Answering


@dataclass(frozen=True, slots=True)
class On:
    service: ServiceRef


@dataclass(frozen=True, slots=True)
class NoRoute:
    pass


@dataclass(frozen=True, slots=True)
class RouteAmbiguous:
    pass


@dataclass(frozen=True, slots=True)
class RouteFailed:
    reason: str


Route = On | NoRoute | RouteAmbiguous | RouteFailed


@dataclass(frozen=True, slots=True)
class Present:
    name: str


@dataclass(frozen=True, slots=True)
class Gone:
    """No service carries the recorded device. A proof, not a parse failure."""


@dataclass(frozen=True, slots=True)
class ServiceAmbiguous:
    pass


@dataclass(frozen=True, slots=True)
class ServiceFailed:
    reason: str


Service = Present | Gone | ServiceAmbiguous | ServiceFailed


@dataclass(frozen=True, slots=True)
class NotObserved:
    """A fact this phase does not consult. A value, not None: an executor that forgot to observe
    something the phase *does* consult must not reach a mutating decision."""


@dataclass(frozen=True, slots=True)
class Marked:
    ref: Ref
    kind: Marker
    port: int
    cmdline: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Complete:
    found: tuple[Marked, ...]


@dataclass(frozen=True, slots=True)
class Incomplete:
    """The process table could not be enumerated whole. Not "nothing was found"."""


Scan = Complete | Incomplete


@dataclass(frozen=True, slots=True)
class SweepClean:
    found_any: bool


@dataclass(frozen=True, slots=True)
class SweepLeft:
    ports: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SweepFailed:
    reason: str


SweepResult = SweepClean | SweepLeft | SweepFailed


@dataclass(frozen=True, slots=True)
class UpObs:
    journal: Journal
    requested: Owner
    route: Route | NotObserved
    pac: PacClass | UnownedClass | NotObserved
    health: Health
    proxy: Liveness | NotObserved
    watchdog: Liveness | NotObserved
    scan: Scan


@dataclass(frozen=True, slots=True)
class DownObs:
    journal: Journal
    service: Service | NotObserved
    pac: PacClass | NotObserved
    health: Health | NotObserved
    proxy: Liveness | NotObserved
    watchdog: Liveness | NotObserved
    scan: Scan


@dataclass(frozen=True, slots=True)
class WatchdogObs:
    journal: Journal
    me: Ref
    service: Service | NotObserved
    pac: PacClass | NotObserved
    health: Health
    proxy: Liveness | NotObserved


@dataclass(frozen=True, slots=True)
class ReleaseObs:
    journal: Journal
    proxy: Liveness | NotObserved
    watchdog: Liveness | NotObserved
    health: Health | NotObserved
    sweep: SweepResult | NotObserved
    scan: Scan


# MARK: - Decisions


@dataclass(frozen=True, slots=True)
class Proceed:
    pass


@dataclass(frozen=True, slots=True)
class Idempotent:
    pass


@dataclass(frozen=True, slots=True)
class RepairOwnPac:
    pass


@dataclass(frozen=True, slots=True)
class Restore:
    target: Target


@dataclass(frozen=True, slots=True)
class AlreadyRestored:
    pass


@dataclass(frozen=True, slots=True)
class Archive:
    reason: Reason


@dataclass(frozen=True, slots=True)
class Sweep:
    pass


@dataclass(frozen=True, slots=True)
class Exit:
    pass


@dataclass(frozen=True, slots=True)
class Refuse:
    reason: str


@dataclass(frozen=True, slots=True)
class Preserve:
    reason: str


Decision = Proceed | Idempotent | RepairOwnPac | Restore | AlreadyRestored | Archive | Sweep | Exit | Refuse | Preserve


@dataclass(frozen=True, slots=True)
class ReleaseNow:
    pass


@dataclass(frozen=True, slots=True)
class KeepJournal:
    reason: str


ReleaseDecision = ReleaseNow | KeepJournal


_DOWN = "run `lyrebird down`"


def phase_word(journal: Journal) -> str:
    """The journal's shape as one word, for `/health` and `status`. `Archived` is one word whatever
    its context: what a reader does about it is the same either way."""
    match journal:
        case Absent():
            return "absent"
        case Unreadable():
            return "unreadable"
        case Archived():
            return "archived"
        case SessionRecord():
            return _phase_name(journal.phase)


def _phase_name(phase: Phase) -> str:
    match phase:
        case Acquiring():
            return "acquiring"
        case Active():
            return "active"
        case Restored():
            return "restored"


def _answering_is(health: Health | NotObserved, ref: Ref | None, liveness: Liveness | NotObserved) -> bool:
    """True only when the recorded process is what answered: the pid matches *and* the ref is
    provably alive.

    A pid is not an identity. With the same pid and PROVEN_DEAD liveness the number was reused by
    something else that happens to answer on the port, and `down` would have restored the PAC out
    from under it — see test_decide_down_refuses_a_reused_pid_that_answers.
    """
    return isinstance(health, Answering) and ref is not None and health.pid == ref.pid and liveness is Liveness.ALIVE


def _uncertain_liveness(*values: Liveness | NotObserved) -> bool:
    return any(value is Liveness.UNKNOWN or isinstance(value, NotObserved) for value in values)


def _pac(pac: PacClass | UnownedClass | NotObserved) -> PacClass | None:
    """The observed class if it is the kind this row asks for, else None — an executor that
    classified against the other question has not observed the fact this row consults."""
    return pac if isinstance(pac, PacClass) else None


def _unowned(pac: PacClass | UnownedClass | NotObserved) -> UnownedClass | None:
    return pac if isinstance(pac, UnownedClass) else None


# MARK: - `up`


def _decide_up_absent(obs: UpObs) -> Decision:
    if isinstance(obs.scan, Incomplete):
        return Refuse(f"this machine's process table could not be read whole — {_DOWN}")
    if obs.scan.found:
        ports = ", ".join(str(marked.port) for marked in sorted(obs.scan.found, key=lambda m: m.port))
        return Refuse(f"a Lyrebird process is still running (port {ports}) with no session — {_DOWN}")
    route = obs.route
    if isinstance(route, NotObserved):
        return Preserve("the default route was not observed")
    if isinstance(route, RouteFailed):
        return Preserve(f"the default route could not be read: {route.reason}")
    if isinstance(route, RouteAmbiguous):
        return Preserve("two network services share the device carrying the default route")
    if isinstance(route, NoRoute):
        return Refuse("could not detect the active network service")
    unowned = _unowned(obs.pac)
    if unowned is None:
        return Preserve("the PAC was not observed")
    if unowned is UnownedClass.UNREADABLE:
        return Preserve("the PAC could not be read")
    if unowned is UnownedClass.LYREBIRD_ENABLED:
        return Refuse(f"a Lyrebird PAC is already enabled on this service with no session — {_DOWN}")
    return Proceed()


def _decide_up_active(obs: UpObs, record: SessionRecord, phase: Active) -> Decision:
    health = obs.health
    if isinstance(health, Answering) and health.journal_error is not None:
        # The proxy reads the journal too; it saying the journal is broken means the `down` this
        # `up` would promise cannot restore from it — see
        # test_decide_up_preserves_when_the_proxy_reports_the_journal_broken.
        return Preserve(f"the proxy reports the session journal unreadable ({health.journal_error}) — {_DOWN}")
    if _uncertain_liveness(obs.proxy, obs.watchdog):
        return Preserve("this session's processes could not be checked")
    route = obs.route
    if isinstance(route, NotObserved):
        return Preserve("the default route was not observed")
    if isinstance(route, RouteFailed):
        return Preserve(f"the default route could not be read: {route.reason}")
    if isinstance(route, RouteAmbiguous):
        return Preserve("two network services share the device carrying the default route")
    if isinstance(route, NoRoute):
        return Preserve("this Mac has no default route")
    if route.service.device != record.service.device:
        # Judged before the PAC, and with the PAC unread on either service: `active_service()` names
        # the *new* route's service, and an unreadable PAC there must not turn this refusal into a
        # Preserve — see test_decide_up_refuses_a_moved_route_even_when_the_new_services_pac_is_unreadable.
        return Refuse(
            f"the default route moved to '{route.service.name}' since this session took "
            f"'{record.service.name}' — `lyrebird down && lyrebird up`"
        )
    if not _answering_is(health, phase.proxy, obs.proxy) or obs.watchdog is not Liveness.ALIVE:
        return Refuse(f"this session's proxy is not the one answering on port {record.owner.control_port} — {_DOWN}")
    if isinstance(health, Answering) and health.fingerprint is not None:
        if health.fingerprint != record.owner.profile_fingerprint:
            return Refuse(
                f"a different profile is already running on port {record.owner.control_port}: "
                f"running {health.fingerprint}, requested {record.owner.profile_fingerprint} — "
                f"stop it first ({_DOWN}) or use a different --profile"
            )
    cls = _pac(obs.pac)
    if cls is None:
        return Preserve("the PAC was not observed")
    if cls is PacClass.UNREADABLE:
        return Preserve("the PAC could not be read")
    if cls is PacClass.OURS_ENABLED:
        return Idempotent()
    if cls is PacClass.OURS_DISABLED:
        return RepairOwnPac()
    return Refuse(f"the PAC on '{record.service.name}' is not this session's any more — {_DOWN}")


def decide_up(obs: UpObs) -> Decision:
    journal = obs.journal
    match journal:
        case Unreadable(reason):
            return Refuse(f"the session journal cannot be read ({reason}) — {_DOWN}")
        case Absent():
            return _decide_up_absent(obs)
        case Archived(_, _, reason, path, _):
            return Refuse(f"a session was archived as {reason} and is still recorded ({path}) — {_DOWN}")
        case SessionRecord():
            phase = journal.phase
            if isinstance(phase, Active) and journal.owner == obs.requested:
                return _decide_up_active(obs, journal, phase)
            if isinstance(phase, Active):
                return Refuse(
                    f"a session for profile {journal.owner.profile_fingerprint} on port "
                    f"{journal.owner.control_port} already holds the PAC — {_DOWN}"
                )
            return Refuse(
                f"a session for profile {journal.owner.profile_fingerprint} is {_phase_name(phase)} "
                f"and still recorded — {_DOWN}"
            )


# MARK: - `down`


def _decide_down_holding(obs: DownObs, record: SessionRecord, proxy: Ref | None) -> Decision:
    watchdog = obs.watchdog if isinstance(record.phase, Active) else Liveness.PROVEN_DEAD
    if _uncertain_liveness(obs.proxy, watchdog):
        return Preserve("this session's processes could not be checked")
    service = obs.service
    if isinstance(service, NotObserved):
        return Preserve("the network service was not observed")
    if isinstance(service, ServiceFailed):
        return Preserve(f"the network service could not be resolved: {service.reason}")
    if isinstance(service, ServiceAmbiguous):
        return Preserve(f"two network services carry device '{record.service.device}'")
    if isinstance(service, Gone):
        # Archived, not restored: there is nowhere to put the baseline back, and the record must
        # not keep claiming a PAC — see test_decide_down_is_total_and_safe.
        return Archive("service-gone")
    cls = _pac(obs.pac)
    if cls is None:
        return Preserve("the PAC was not observed")
    if cls is PacClass.UNREADABLE:
        return Preserve("the PAC could not be read")
    if cls is PacClass.FOREIGN:
        # Before the health-identity refusal: a stranger answering on the port must not keep open
        # an obligation this observation has already ended — see
        # test_decide_down_archives_a_foreign_pac_even_when_a_stranger_answers.
        return Archive("displaced")
    health = obs.health
    if isinstance(health, NotObserved):
        return Preserve(f"port {record.owner.control_port} was not asked who holds it")
    if isinstance(health, Answering) and not _answering_is(health, proxy, obs.proxy):
        return Refuse(
            f"a proxy this session does not name answers on port {record.owner.control_port} "
            f"(pid {health.pid}) — stop it, then {_DOWN} again"
        )
    target = restore_target(record.baseline)
    if cls is PacClass.OURS_ENABLED or cls is PacClass.RESUMABLE:
        return Restore(target)
    if cls is PacClass.OURS_DISABLED and isinstance(target, Configured):
        return Restore(target)
    if satisfies(cls, target):
        return AlreadyRestored()
    return Preserve("the PAC is in a state this session cannot account for")


def decide_down(obs: DownObs) -> Decision:
    journal = obs.journal
    match journal:
        case Unreadable():
            # Decided here, not in the executor: the bytes are preserved and the record replaced,
            # whatever else was observed — see test_decide_down_is_total_and_safe.
            return Archive("unreadable")
        case Absent():
            if isinstance(obs.scan, Incomplete):
                return Preserve("this machine's process table could not be read whole")
            return Sweep()
        case Archived(_, _, _, _, context):
            if isinstance(context, Unknown):
                if isinstance(obs.scan, Incomplete):
                    return Preserve("this machine's process table could not be read whole")
                return Sweep()
            return AlreadyRestored()
        case SessionRecord():
            phase = journal.phase
            match phase:
                case Restored():
                    return AlreadyRestored()
                case Acquiring(None):
                    if isinstance(obs.scan, Incomplete):
                        return Preserve("this machine's process table could not be read whole")
                    return Proceed()
                case Acquiring(proxy) if proxy is not None:
                    return _decide_down_holding(obs, journal, proxy)
                case Active(proxy, _):
                    return _decide_down_holding(obs, journal, proxy)
                case _:
                    return Preserve("the recorded phase carries no process this command can act on")


# MARK: - the watchdog


def decide_watchdog(obs: WatchdogObs) -> Decision:
    journal = obs.journal
    if not isinstance(journal, SessionRecord) or not isinstance(journal.phase, Active):
        return Exit()
    phase = journal.phase
    if phase.watchdog != obs.me:
        # Another watchdog's record: retiring is the only safe answer, since restoring would act on
        # an obligation somebody else holds — see test_decide_watchdog_is_total_and_safe.
        return Exit()
    health = obs.health
    if isinstance(health, Answering):
        if health.pid != phase.proxy.pid:
            return Exit()
        if isinstance(obs.proxy, NotObserved) or obs.proxy is Liveness.UNKNOWN:
            return Preserve("the proxy could not be checked")
        if obs.proxy is Liveness.PROVEN_DEAD:
            # The recorded pid answers but the process is gone: something else holds the port.
            return Exit()
        service = obs.service
        if not isinstance(service, Present):
            return Preserve("the network service was not resolved")
        cls = _pac(obs.pac)
        if cls is None or cls is PacClass.UNREADABLE:
            return Preserve("the PAC could not be read")
        if cls is PacClass.OURS_DISABLED:
            return RepairOwnPac()
        return Proceed()
    service = obs.service
    if not isinstance(service, Present):
        # Keep ticking: a service that is gone or unresolved is a temporary condition here, and
        # only `down` archives — see test_decide_watchdog_keeps_ticking_when_the_service_is_gone.
        return Preserve("the network service was not resolved")
    cls = _pac(obs.pac)
    if cls is None or cls is PacClass.UNREADABLE:
        return Preserve("the PAC could not be read")
    if cls is PacClass.FOREIGN:
        return Proceed()
    # Proxy liveness is deliberately not consulted here: whatever it says, the Mac must not stay
    # routed at a port nothing answers on — see test_decide_watchdog_restores_for_every_liveness.
    return Restore(restore_target(journal.baseline))


# MARK: - releasing the journal


def _release_port_row(obs: ReleaseObs, named: tuple[tuple[Ref | None, Liveness | NotObserved], ...]) -> ReleaseDecision:
    for ref, liveness in named:
        if ref is None:
            continue
        if liveness is not Liveness.PROVEN_DEAD:
            # Not "it did not answer": a ref that is alive, or that could not be checked, is a
            # process that may still install a PAC after the journal is gone — see
            # test_decide_release_keeps_the_journal_while_a_ref_is_not_proven_dead.
            return KeepJournal(f"pid {ref.pid} is not proven dead")
    if isinstance(obs.scan, Incomplete):
        return KeepJournal("this machine's process table could not be read whole")
    if obs.scan.found:
        return KeepJournal("a marked Lyrebird process is still running")
    if isinstance(obs.health, NotObserved):
        return KeepJournal("the session's port was not asked who holds it")
    if isinstance(obs.health, Answering):
        return KeepJournal("something still answers on the session's port")
    return ReleaseNow()


def _release_portless_row(obs: ReleaseObs) -> ReleaseDecision:
    if isinstance(obs.scan, Incomplete):
        return KeepJournal("this machine's process table could not be read whole")
    if obs.scan.found:
        return KeepJournal("a marked Lyrebird process is still running")
    sweep = obs.sweep
    if isinstance(sweep, NotObserved):
        return KeepJournal("the sweep was not observed")
    if isinstance(sweep, SweepFailed):
        return KeepJournal(f"the sweep could not finish: {sweep.reason}")
    if isinstance(sweep, SweepLeft):
        ports = ", ".join(str(port) for port in sweep.ports)
        return KeepJournal(f"a Lyrebird PAC is still enabled (port {ports})")
    return ReleaseNow()


def decide_release(obs: ReleaseObs) -> ReleaseDecision:
    """`ReleaseNow` only from a journal after which no PAC obligation is open, with every ref it
    names proven dead and the port fact that row owns holding. Anything else keeps the journal:
    an unlink over an open obligation is the record of what to put back, gone."""
    journal = obs.journal
    match journal:
        case Unreadable(reason):
            return KeepJournal(f"the journal cannot be read ({reason})")
        case Absent():
            return _release_portless_row(obs)
        case Archived(_, _, _, _, context):
            if isinstance(context, Known):
                return _release_port_row(obs, ((context.proxy, obs.proxy),))
            return _release_portless_row(obs)
        case SessionRecord():
            phase = journal.phase
            match phase:
                case Restored(proxy):
                    return _release_port_row(obs, ((proxy, obs.proxy),))
                case Acquiring(None):
                    return _release_port_row(obs, ())
                case Acquiring():
                    return KeepJournal("this session is still acquiring the PAC")
                case Active():
                    return KeepJournal("this session still holds the PAC")
