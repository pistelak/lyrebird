"""The pure core of PAC ownership: what the session journal says, and what an observed PAC is.

No IO here, and no `os`, `subprocess`, `psutil`, `netproxy` or `config` imports — the `rules.py`
house rule, for the same reason: this is the module that decides whether somebody's proxy settings
are touched, and it has to be provable with the standard library alone. Everything below takes
facts that were already read and returns a value; the decoder takes an already-parsed `object`,
and `session.py` owns `json.loads`.

Two ideas run through it: one journal record per user says who holds the PAC and what was there
before, and one classifier says what an observed PAC is relative to that record — so the overlaps
between "ours", "the baseline" and "already restored" are resolved in one place rather than in
every command that reads a PAC.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum

SESSION_VERSION = 1

MAX_PORT = 65535

# The PAC URL Lyrebird installs, on any port. Anchored and strict: a URL that merely contains ours
# is somebody else's — see test_lyrebird_port_is_a_strict_parse.
_OUR_URL = re.compile(r"^http://127\.0\.0\.1:(\d{1,5})/proxy\.pac$")


class DecodeError(ValueError):
    """A journal payload this reader will not accept, and why."""


# MARK: - The values a record is made of


@dataclass(frozen=True, slots=True)
class Ref:
    """A process as `psutil` reports it: the pid and the create time, together.

    Never a bare pid: a pid is reused, and signalling on one alone is how `down` killed whatever
    inherited the number — see test_terminate_refuses_a_reused_pid.
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


def _check_baseline(baseline: Pac) -> None:
    """The two shapes that are readings but never baselines.

    An *enabled* Lyrebird URL as a baseline would make `restore_target` name a target `satisfies`
    can never hold for, so every `down` would refuse forever
    (test_decode_rejects_an_enabled_lyrebird_baseline); `("", True)` would make the satisfied
    reading `("", False)` classify as RESUMABLE forever
    (test_decode_rejects_an_enabled_empty_baseline).
    """
    if baseline.enabled and lyrebird_port(baseline.url) is not None:
        raise DecodeError("a baseline may not be an enabled Lyrebird PAC URL")
    if baseline.enabled and baseline.url == "":
        raise DecodeError("a baseline with no URL may not be enabled")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """The one record: who owns the session, which service holds its PAC, what was there before,
    the proxy it may signal, and the simulator whose keychain holds the CA."""

    version: int
    owner: Owner
    service: ServiceRef
    baseline: Pac
    proxy: Ref
    simulator: Simulator | None

    def __post_init__(self) -> None:
        _check_baseline(self.baseline)


@dataclass(frozen=True, slots=True)
class Absent:
    pass


@dataclass(frozen=True, slots=True)
class Unreadable:
    """The journal is there and could not be read. Not `Absent`: a record that cannot be read may
    still be holding somebody's PAC — see test_read_reports_a_directory_as_unreadable."""

    reason: str


Journal = Absent | Unreadable | SessionRecord


# MARK: - Encode / decode


def encode(record: SessionRecord) -> dict[str, object]:
    return {
        "version": record.version,
        "owner": {
            "controlPort": record.owner.control_port,
            "profileFingerprint": record.owner.profile_fingerprint,
            "stateRoot": record.owner.state_root,
        },
        "service": {"name": record.service.name, "device": record.service.device},
        "baseline": {"url": record.baseline.url, "enabled": record.baseline.enabled},
        "proxy": {"pid": record.proxy.pid, "createTime": record.proxy.create_time},
        "simulator": (
            None if record.simulator is None else {"udid": record.simulator.udid, "name": record.simulator.name}
        ),
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
    missing = sorted(set(keys) - set(data))
    unknown = sorted(set(data) - set(keys))
    if missing:
        raise DecodeError(f"{where}: missing {', '.join(missing)}")
    if unknown:
        raise DecodeError(f"{where}: unknown {', '.join(unknown)}")


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise DecodeError(f"{where}: expected a string, found {type(value).__name__}")
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


def _decode_owner(value: object) -> Owner:
    data = _object(value, "owner")
    _exact(data, ("controlPort", "profileFingerprint", "stateRoot"), "owner")
    port = _integer(data["controlPort"], "owner.controlPort")
    if not 1 <= port <= MAX_PORT:
        raise DecodeError(f"owner.controlPort: {port} is not a port")
    return Owner(
        control_port=port,
        profile_fingerprint=_string(data["profileFingerprint"], "owner.profileFingerprint"),
        state_root=_string(data["stateRoot"], "owner.stateRoot"),
    )


def _decode_service(value: object) -> ServiceRef:
    data = _object(value, "service")
    _exact(data, ("name", "device"), "service")
    return ServiceRef(name=_string(data["name"], "service.name"), device=_string(data["device"], "service.device"))


def _decode_simulator(value: object) -> Simulator | None:
    if value is None:
        return None
    data = _object(value, "simulator")
    _exact(data, ("udid", "name"), "simulator")
    return Simulator(udid=_string(data["udid"], "simulator.udid"), name=_string(data["name"], "simulator.name"))


def _decode_baseline(value: object) -> Pac:
    data = _object(value, "baseline")
    _exact(data, ("url", "enabled"), "baseline")
    baseline = Pac(url=_string(data["url"], "baseline.url"), enabled=_boolean(data["enabled"], "baseline.enabled"))
    _check_baseline(baseline)
    return baseline


def decode(data: object) -> SessionRecord:
    """The one reader. Raises `DecodeError`; never returns a partially understood record.

    Strict on purpose: what this accepts, `down` acts on. A record it accepted loosely — an unknown
    key, a pid of `True`, a port of 0 — is a record something signalled or restored from.
    """
    top = _object(data, "record")
    _exact(top, ("version", "owner", "service", "baseline", "proxy", "simulator"), "record")
    version = _integer(top["version"], "version")
    if version != SESSION_VERSION:
        raise DecodeError(f"version: expected {SESSION_VERSION}, found {version}")
    return SessionRecord(
        version=version,
        owner=_decode_owner(top["owner"]),
        service=_decode_service(top["service"]),
        baseline=_decode_baseline(top["baseline"]),
        proxy=_decode_ref(top["proxy"], "proxy"),
        simulator=_decode_simulator(top["simulator"]),
    )


# MARK: - The classifier


class PacClass(Enum):
    """What an observed PAC is, relative to one port and one baseline. Ordered: `classify` answers
    the first that matches, and `satisfies` is the only predicate for "the obligation is met"."""

    OURS_ENABLED = "ours_enabled"
    OURS_DISABLED = "ours_disabled"
    RESUMABLE = "resumable"
    AT_TARGET = "at_target"
    FOREIGN = "foreign"


class UnownedClass(Enum):
    """What an observed PAC is when no journal claims it — `up`'s question."""

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


def classify(observed: Pac, port: int, baseline: Pac) -> PacClass:
    """The first matching class, in priority order. One function so the overlaps — a configured
    baseline is both untouched and at target; disabled residue is both ours-disabled and at the Off
    target — are resolved in one place: test_classifier_returns_the_first_matching_class_in_priority.
    """
    if observed.url == our_url(port):
        return PacClass.OURS_ENABLED if observed.enabled else PacClass.OURS_DISABLED
    if observed.url == baseline.url and observed.enabled != baseline.enabled:
        # For every URL including the empty one: `("", True)` over a `("", False)` baseline is one
        # `state off` away from the target, not a stranger's PAC — see
        # test_classifier_reads_an_enabled_empty_url_as_resumable.
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


def classify_unowned(observed: Pac) -> UnownedClass:
    if observed.url == "":
        return UnownedClass.EMPTY
    if lyrebird_port(observed.url) is not None:
        # Any port: an enabled Lyrebird PAC with no journal is somebody's unowned session, and `up`
        # over it would strand it — see test_classify_unowned_reads_any_port_as_a_lyrebird_pac.
        return UnownedClass.LYREBIRD_ENABLED if observed.enabled else UnownedClass.LYREBIRD_DISABLED
    return UnownedClass.OTHER
