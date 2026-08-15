"""Paths, ports, profile resolution and host scoping for Lyrebird.

There are two seams, deliberately kept apart:

* the **profile** (``--profile`` / ``LYREBIRD_PROFILE``) — your data, and safe to keep in version
  control: which hosts to intercept, saved sessions, presets.
* **tool-owned files**, which macOS wants in three different places and which differ in what may
  destroy them: durable state and the CA in ``~/Library/Application Support/Lyrebird``, the
  regenerable catalog in ``~/Library/Caches/com.lyrebird.Lyrebird``, and the proxy log in
  ``~/Library/Logs/Lyrebird``. Setting ``LYREBIRD_STATE_DIR`` collapses all three underneath it,
  which is what the tests and anyone who wants one directory to delete rely on. Never put any of
  them in a repo.

Runtime files are keyed by *control port*, not by profile, so ``lyrebird down`` finds the running
instance no matter which profile — or which directory — it is invoked from.

Host scoping is **exact**: a profile lists complete hostnames and subdomains are not implied.
The three mechanisms that enforce it (``is_intercepted_host`` for the addon, ``allow_hosts_regexes``
for mitmproxy's TLS decryption, and ``pac_contents`` for macOS routing) are all generated from the
same validated list, and all three match case-insensitively, so a host cannot be accepted by one
and refused by another.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# MARK: - Code location (never written to at runtime)

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
EXAMPLES_DIR = ROOT / "examples"


# MARK: - Ports and listeners

PROXY_PORT = int(os.environ.get("LYREBIRD_PROXY_PORT", "8080"))
CONTROL_PORT = int(os.environ.get("LYREBIRD_CONTROL_PORT", "8088"))


# The admin API is unauthenticated, so it binds to loopback and is not configurable.
CONTROL_HOST = "127.0.0.1"
# Where mitmproxy binds. Separate from the control host so widening one never widens the other.
PROXY_LISTEN_HOST = os.environ.get("LYREBIRD_PROXY_LISTEN_HOST", "127.0.0.1")
# The proxy address the PAC advertises to clients — not necessarily the bind address.
PROXY_ADVERTISED_HOST = os.environ.get("LYREBIRD_PROXY_ADVERTISED_HOST", "127.0.0.1")

CONTROL_ORIGIN = f"http://{CONTROL_HOST}:{CONTROL_PORT}"
CONTROL_HOST_HEADER = f"{CONTROL_HOST}:{CONTROL_PORT}"

RECENT_CAP = 200
MAX_DELAY_MS = 60000  # ceiling for a per-override delayMs so a typo can't wedge a flow indefinitely



# MARK: - Profile and state locations

def _default_profile() -> Path:
    """`~/.config/lyrebird`, honouring XDG_CONFIG_HOME.

    A profile is configuration: hand-edited, worth version-controlling, and the only part of
    Lyrebird a user is expected to open in an editor. `~/.config` is where CLI tools put that on
    macOS as well as Linux, so it belongs there rather than in a bespoke dotdir.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "lyrebird"


def _default_state_root() -> Path:
    """What must survive: `~/Library/Application Support/Lyrebird`.

    Deliberately NOT `~/.config`: none of this is configuration. It is the active-session pointer,
    the per-port recovery files, and the CA private key — which is better somewhere Finder hides,
    and all of which it would be wrong to lose. Time Machine includes this directory, which is the
    reason the other two exist: `tmutil isexcluded` reports Caches and Logs as excluded, so a cache
    kept here would be backed up forever for no benefit.
    """
    return Path.home() / "Library" / "Application Support" / "Lyrebird"


def _default_cache_root() -> Path:
    """What may be thrown away: `~/Library/Caches/com.lyrebird.Lyrebird`.

    Apple's directory for discardable files, and excluded from Time Machine. The catalog is derived
    from a spec or from observed traffic, so losing it costs one regeneration.
    """
    return Path.home() / "Library" / "Caches" / "com.lyrebird.Lyrebird"


def _default_log_root() -> Path:
    """What a person reads: `~/Library/Logs/Lyrebird`.

    Apple's directory for user-visible logs, which is where Console.app looks — so putting the
    proxy log here means it can be read and searched without `lyrebird logs` at all.
    """
    return Path.home() / "Library" / "Logs" / "Lyrebird"


PROFILE_DIR: Path
PROFILE_FILE: Path
SESSIONS_DIR: Path
PRESETS_DIR: Path
STATE_ROOT: Path
CACHE_ROOT: Path
LOG_ROOT: Path
STATE_FILE: Path
CATALOG_FILE: Path
LOG_FILE: Path
PROFILE_FINGERPRINT: str


def configure(profile: str | None = None) -> None:
    """(Re)resolve every path. Called at import from the environment, and again by the CLI when
    ``--profile`` is given. The CLI exports the resolved path into the mitmdump child's
    environment, so the addon and the supervisor always agree on which profile is live."""
    # Module-level rebinding is the point: every module reads these as `config.X`, and the CLI
    # re-resolves them once at startup before anything else imports them. Threading a settings
    # object through the addon, store, control server and CLI would buy nothing here.
    global PROFILE_DIR, PROFILE_FILE, SESSIONS_DIR, PRESETS_DIR
    global STATE_ROOT, CACHE_ROOT, LOG_ROOT, STATE_FILE, CATALOG_FILE, LOG_FILE, PROFILE_FINGERPRINT

    raw = profile or os.environ.get("LYREBIRD_PROFILE")
    # Resolved either way. Only the explicit path used to be, so if `~/.config` is a symlink — or
    # XDG_CONFIG_HOME is — the same physical profile got two fingerprints depending on whether you
    # named it or let it default, and therefore two active-session pointers and two logs.
    PROFILE_DIR = (Path(raw).expanduser() if raw else _default_profile()).resolve()
    PROFILE_FILE = PROFILE_DIR / "profile.json"
    SESSIONS_DIR = PROFILE_DIR / "sessions"
    PRESETS_DIR = PROFILE_DIR / "presets"

    state_env = os.environ.get("LYREBIRD_STATE_DIR")
    if state_env:
        # An explicit override means "put everything here", not "here plus two Library directories":
        # the tests point it at a temp tree and expect nothing to escape, and someone who sets it by
        # hand wants one directory to delete. The platform split is a property of the defaults only.
        STATE_ROOT = Path(state_env).expanduser().resolve()
        CACHE_ROOT = STATE_ROOT / "cache"
        LOG_ROOT = STATE_ROOT / "logs"
    else:
        STATE_ROOT = _default_state_root()
        CACHE_ROOT = _default_cache_root()
        LOG_ROOT = _default_log_root()

    PROFILE_FINGERPRINT = hashlib.sha256(str(PROFILE_DIR).encode("utf-8")).hexdigest()[:12]
    STATE_FILE = STATE_ROOT / "profiles" / PROFILE_FINGERPRINT / "state.json"
    CATALOG_FILE = CACHE_ROOT / PROFILE_FINGERPRINT / "catalog.json"
    LOG_FILE = LOG_ROOT / f"{PROFILE_FINGERPRINT}.log"


def runtime_file() -> Path:
    """Keyed by control port, not profile — `down` must find the live instance from anywhere."""
    return STATE_ROOT / f"runtime-{CONTROL_PORT}.json"


def lock_file() -> Path:
    return STATE_ROOT / f"lock-{CONTROL_PORT}"


def mitmproxy_confdir() -> Path:
    """A CA of our own, so trusting Lyrebird never widens trust for other mitmproxy tooling."""
    return STATE_ROOT / "mitmproxy"


def atomic_write(path: Path, text: str) -> None:
    """Write privately and indivisibly: these files hold response payloads and pids, so they must
    not be world-readable and must never be observed half-written.

    The mode is set when the file is created, not after it is filled. Writing first and calling
    `chmod` second leaves the payload world-readable for the width of that gap under the usual 022
    umask — which is the opposite of what the sentence above promises.

    `O_EXCL | O_NOFOLLOW` because the temporary name is derived from the pid and therefore
    predictable: after a crash, or with a pid reused, something may already be sitting there. The
    unlink first keeps the old overwrite behaviour; the flags make sure that between the unlink and
    the open we cannot be talked into writing through a symlink someone else planted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.unlink(missing_ok=True)
    descriptor = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)


def read_runtime() -> dict:
    """Never raises. `lyrebird down` reads this before doing anything else, so an unreadable file
    must not stop the PAC being restored."""
    path = runtime_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_runtime(data: dict) -> None:
    atomic_write(runtime_file(), json.dumps(data, indent=2))


configure()


# MARK: - Profile loading and host validation

# A conservative DNS hostname. Rejects schemes, ports, paths, wildcards, whitespace and control
# characters by construction — all of which would otherwise reach a regex or the PAC JavaScript.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


def validate_host(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"invalid host {raw!r} — must be a string")
    host = raw.strip().lower()
    if host.replace(".", "").isdigit():
        raise ValueError(f"invalid host {raw!r} — give a hostname, not an address")
    if not _HOSTNAME_RE.match(host):
        raise ValueError(
            f"invalid host {raw!r} — give a bare hostname such as 'api.example.com' "
            f"(no scheme, port, path or wildcard)"
        )
    return host


@dataclass(frozen=True, slots=True)
class Profile:
    """The result of reading profile.json.

    A dataclass rather than a TypedDict because this never round-trips through JSON — it is a
    load result, not a document — so nothing is lost by giving it a closed shape.
    """

    hosts: list[str]
    sim_bundle_id: str | None
    exists: bool


def load_profile() -> Profile:
    """Read profile.json. Fails closed and loudly: a malformed profile must never silently fall
    back to intercepting something the user did not ask for."""
    if not PROFILE_FILE.is_file():
        return Profile(hosts=[], sim_bundle_id=None, exists=False)

    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"could not read {PROFILE_FILE}: {error}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"{PROFILE_FILE}: expected a JSON object")

    hosts = data.get("hosts", [])
    if not isinstance(hosts, list):
        raise SystemExit(f"{PROFILE_FILE}: 'hosts' must be a list")
    try:
        validated = [validate_host(host) for host in hosts]
    except ValueError as error:
        raise SystemExit(f"{PROFILE_FILE}: {error}") from None

    bundle_id = data.get("simBundleId")
    # An empty host list means "intercept nothing", and that is honoured.
    return Profile(hosts=validated, sim_bundle_id=str(bundle_id) if bundle_id else None, exists=True)


PROFILE = load_profile()
INTERCEPT_HOSTS: list[str] = PROFILE.hosts


def reload_profile() -> None:
    """Re-read profile.json after `configure()` has moved the paths."""
    global PROFILE, INTERCEPT_HOSTS
    PROFILE = load_profile()
    INTERCEPT_HOSTS = PROFILE.hosts


# MARK: - The three host-scoping mechanisms (all generated from INTERCEPT_HOSTS)

def is_intercepted_host(host: str) -> bool:
    """Exact hostname match. Subdomains are not implied — list them explicitly if you need them."""
    return (host or "").lower().split(":")[0] in INTERCEPT_HOSTS


def allow_hosts_regexes() -> list[str]:
    """Regexes for mitmproxy's ``allow_hosts``, which uses ``re.search`` against ``host:port``.

    Anchored at both ends so a lookalike such as ``api.example.com.attacker.test`` cannot match.
    An empty profile yields a never-matching pattern, because an empty ``allow_hosts`` means
    "no restriction" to mitmproxy — the opposite of what an empty profile asks for.
    """
    if not INTERCEPT_HOSTS:
        return [r"(?!)"]
    # (?i): DNS is case-insensitive and a client may send any case. Without this the addon
    # check (which lowercases) accepted a host that mitmproxy then refused to decrypt, so the
    # request reached the proxy and was silently tunnelled instead of intercepted.
    return [rf"(?i)^{re.escape(host)}(:\d+)?$" for host in INTERCEPT_HOSTS]


def pac_contents() -> str:
    """A PAC that routes exactly the configured hosts to us and everything else DIRECT.

    Hosts are emitted with ``json.dumps`` so they are always well-formed JS string literals, and
    compared with ``===`` rather than ``shExpMatch`` to keep PAC matching identical to
    ``is_intercepted_host``.
    """
    if not INTERCEPT_HOSTS:
        return 'function FindProxyForURL(url, host) {\n  return "DIRECT";\n}\n'

    clauses = " ||\n      ".join(f"lower === {json.dumps(host)}" for host in INTERCEPT_HOSTS)
    proxy = json.dumps(f"PROXY {PROXY_ADVERTISED_HOST}:{PROXY_PORT}")
    return (
        "function FindProxyForURL(url, host) {\n"
        "  var lower = host.toLowerCase();\n"
        f"  if ({clauses}) {{\n"
        f"    return {proxy};\n"
        "  }\n"
        '  return "DIRECT";\n'
        "}\n"
    )
