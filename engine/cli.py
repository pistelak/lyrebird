"""lyrebird — supervisor and macOS integration for the Lyrebird mock proxy.

    lyrebird init [PATH]                  create a profile from the bundled examples
    lyrebird up [--relaunch <bundleid>]   start proxy, trust CA, install host-scoped PAC
    lyrebird down                         stop proxy and restore the previous proxy settings
    lyrebird use <session>                switch active session (reports what it displaced)
    lyrebird recent [--json] [--matched]  what came through, and which overrides answered
    lyrebird override add <json>          add a rule to the active session, no restart
    lyrebird session new <name>           create a scratch session (--clone-from X)
    lyrebird sequence reset [id]          rewind response sequences to their first step
    lyrebird sequence wait <id> --step N  block until a sequence serves a given step
    lyrebird status [--json]              show intercept state (honest about PAC on/off)
    lyrebird wait-ready [--match]         block until traffic arrives, or until a rule matches
    lyrebird trust-ca / untrust-ca        manage the simulator CA
    lyrebird logs                         print the last 60 lines; path on stderr

Routing uses a *host-scoped PAC* so only the hosts in your profile go through the proxy; everything
else stays DIRECT. Whatever PAC you had before is recorded and put back on `down` — and by the
watchdog if the proxy dies, so a crash is unlikely to strand the Mac pointing at a dead port.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import click

import config
import netproxy

MITMDUMP = config.ROOT / ".venv" / "bin" / "mitmdump"
CONTROL = config.CONTROL_ORIGIN
_DOWN_WAIT_SECONDS = 5.0   # how long `down` waits for SIGTERM to take effect

R = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"


# MARK: - Small helpers

def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def _ca_cert() -> Path:
    return config.mitmproxy_confdir() / "mitmproxy-ca-cert.pem"


def _get_json(path: str, timeout: float = 1.5) -> Any:
    request = urllib.request.Request(f"{CONTROL}{path}", headers={"Host": config.CONTROL_HOST_HEADER})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except Exception:  # any failure means 'not reachable', which is the answer
        return None


def _health() -> dict | None:
    return _get_json("/__mock__/health")


def _control(path: str, method: str = "GET", payload: Any = None, timeout: float = 3.0) -> Any:
    """Call the control API, or exit with its error message.

    Exists so nothing outside this function has to remember the loopback Host header or the
    JSON content-type the API requires — both of which otherwise fail as a bare 421 or 415.
    """
    headers = {"Host": config.CONTROL_HOST_HEADER}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["content-type"] = "application/json"
    request = urllib.request.Request(f"{CONTROL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode()).get("error", error.reason)
        except (ValueError, OSError):
            detail = error.reason
        click.echo(f"{RED}✗ {detail}{R}")
        raise SystemExit(1) from None
    except OSError:
        click.echo(f"{RED}✗ proxy not reachable — is it running? (`lyrebird up`){R}")
        raise SystemExit(1) from None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_is_ours(pid: int | None, marker: str) -> bool:
    """PIDs are reused. Never signal one whose command line isn't recognisably ours."""
    if not _pid_alive(pid):
        return False
    result = _run(["ps", "-p", str(pid), "-o", "command="])
    return marker in result.stdout


def _terminate(pid: int | None, marker: str) -> None:
    if pid is None or not _pid_is_ours(pid, marker):
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def _child_env() -> dict:
    return {**os.environ, "LYREBIRD_PROFILE": str(config.PROFILE_DIR)}


def trust_ca_in_sim() -> tuple[bool, str]:
    if not _ca_cert().is_file():
        return False, "CA cert not generated yet (start the proxy first)"
    result = _run(["xcrun", "simctl", "keychain", "booted", "add-root-cert", str(_ca_cert())])
    if result.returncode == 0:
        return True, "trusted in booted simulator"
    return False, (result.stderr or result.stdout or "no booted simulator?").strip()


def _relaunch(bundle_id: str) -> tuple[bool, str]:
    """Terminate is allowed to fail — the app may not be running. Launch is not.

    simctl reports failures as several lines of nested domain/code detail. Only the first line
    carries information a person can act on, and the common failure has a much better answer
    than the text simctl produces.
    """
    _run(["xcrun", "simctl", "terminate", "booted", bundle_id])
    result = _run(["xcrun", "simctl", "launch", "booted", bundle_id])
    if result.returncode == 0:
        return True, bundle_id
    raw = (result.stderr or result.stdout or "launch failed").strip()
    if "failed to launch" in raw or "not find" in raw.lower():
        if bundle_id == "com.example.Store":
            return False, ("com.example.Store is the example placeholder — set simBundleId in "
                           f"{config.PROFILE_FILE} to your app's bundle id")
        return False, f"{bundle_id} is not installed in the booted simulator"
    return False, raw.splitlines()[0]


def _spawn_watchdog(service: str) -> int:
    proc = subprocess.Popen(
        [sys.executable, str(config.ROOT / "cli.py"), "_watchdog", service],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        env=_child_env(),
    )
    return proc.pid


def _require_profile() -> None:
    if not config.PROFILE.exists:
        raise SystemExit(
            f"{RED}no profile at {config.PROFILE_DIR}{R}\n"
            f"  create one:   lyrebird init {config.PROFILE_DIR}\n"
            f"  or point at an existing one:  lyrebird --profile /path/to/profile ...\n"
            f"  (a profile is a directory containing profile.json and sessions/)"
        )
    if not config.INTERCEPT_HOSTS:
        click.echo(f"{YELLOW}⚠ profile lists no hosts — nothing will be intercepted.{R}")


# MARK: - CLI

@click.group()
@click.option("--profile", type=click.Path(), default=None,
              help="Profile directory (overrides $LYREBIRD_PROFILE).")
def cli(profile: str | None) -> None:
    if profile:
        resolved = str(Path(profile).expanduser().resolve())
        os.environ["LYREBIRD_PROFILE"] = resolved
        config.configure(resolved)
        config.reload_profile()


@cli.command()
@click.argument("path", type=click.Path(), required=False)
def init(path: str | None) -> None:
    """Create a profile from the bundled examples."""
    target = Path(path).expanduser().resolve() if path else config.PROFILE_DIR
    if (target / "profile.json").exists():
        raise SystemExit(f"{RED}{target}/profile.json already exists — refusing to overwrite{R}")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(config.EXAMPLES_DIR, target, dirs_exist_ok=True)
    click.echo(f"✓ profile created at {BOLD}{target}{R}")
    click.echo(f"  Edit {target}/profile.json: set `hosts` to the API your app calls, and\n"
               f"  `simBundleId` to your app's bundle identifier. The examples are a schema\n"
               f"  template, not a runnable demo — api.example.com serves none of these paths.")
    click.echo(f"  Then:  lyrebird --profile {target} up")


@cli.command()
@click.option("--relaunch", "bundle_id", default=None,
              help="Terminate + relaunch this app bundle id after up (strongly recommended).")
def up(bundle_id: str | None) -> None:
    """Start the proxy, trust the CA in the simulator, and install the host-scoped PAC."""
    _require_profile()
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)

    with open(config.lock_file(), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(
                f"{RED}another `lyrebird up` is in progress on port {config.CONTROL_PORT}{R}") from None
        _up_locked(bundle_id)


def _up_locked(bundle_id: str | None) -> None:
    runtime = config.read_runtime()
    existing = _health()

    if existing:
        running_profile = existing.get("profileFingerprint")
        if running_profile and running_profile != config.PROFILE_FINGERPRINT:
            raise SystemExit(
                f"{RED}a different profile is already running on port {config.CONTROL_PORT}{R}\n"
                f"  running: {running_profile}   requested: {config.PROFILE_FINGERPRINT}\n"
                f"  stop it first (`lyrebird down`) or use a different --profile / port."
            )
        click.echo(f"{YELLOW}proxy already running{R} (session '{existing['activeSession']}')")
        proxy_pid = existing.get("pid", 0)
    else:
        _start_fresh_log()
        # Popen dups the fd for the child, so closing our copy immediately is correct.
        with open(config.LOG_FILE, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [str(MITMDUMP),
                 "--listen-host", config.PROXY_LISTEN_HOST, "--listen-port", str(config.PROXY_PORT),
                 "--set", f"confdir={config.mitmproxy_confdir()}",
                 "-s", str(config.ROOT / "addon.py")],
                cwd=str(config.ROOT), stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                env=_child_env(),
            )
        deadline = time.time() + 12
        while _health() is None:
            if not _pid_alive(proc.pid):
                click.echo(f"{RED}proxy exited on startup — last log lines:{R}\n{_tail_log(20)}")
                raise SystemExit(1)
            if time.time() >= deadline:
                # Don't leave an orphan that becomes healthy after we have given up on it.
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                click.echo(f"{RED}proxy did not become healthy in time — last log lines:{R}\n"
                           f"{_tail_log(20)}\n   full log: {config.LOG_FILE}")
                raise SystemExit(1)
            time.sleep(0.3)
        proxy_pid = proc.pid

    failures: list[str] = []

    ca_ok, message = trust_ca_in_sim()
    click.echo(f"{'✓' if ca_ok else '✗'} CA: {message}")
    if not ca_ok:
        failures.append(f"CA not trusted in the simulator: {message}")

    service = netproxy.active_service()
    state = {"proxyPid": proxy_pid, "service": service}
    # Record the pid before anything else can fail: if PAC installation raises, `down` must still
    # be able to find and stop the proxy we just started.
    config.write_runtime(state)

    if service:
        # Only carry a recorded previousPac forward while a proxy is actually running. A stale
        # entry left by an earlier crash would otherwise be restored over the user's current
        # settings much later.
        before = (runtime.get("previousPac") if existing else None) or _snapshot_pac(service)
        state["previousPac"] = before
        config.write_runtime(state)
        try:
            netproxy.set_pac(service)
        except netproxy.NetworkSetupError as error:
            click.echo(f"{RED}✗ could not install the PAC on '{service}': {error}{R}\n"
                       f"   the proxy is running — stop it with `lyrebird down`.")
            raise SystemExit(1) from None

        watchdog_pid = runtime.get("watchdogPid")
        if not _pid_is_ours(watchdog_pid, "_watchdog"):
            watchdog_pid = _spawn_watchdog(service)
        state["watchdogPid"] = watchdog_pid
        click.echo(f"✓ PAC installed on '{service}' (configured hosts → proxy, everything else DIRECT)")
    else:
        click.echo(f"{RED}✗ could not detect the active network service — set the PAC manually:{R}\n"
                   f"   {netproxy.pac_url()}")
        failures.append("no active network service: traffic is NOT being intercepted")

    config.write_runtime(state)

    target = bundle_id or config.PROFILE.sim_bundle_id
    if target:
        launched, detail = _relaunch(target)
        click.echo(f"{'✓' if launched else '✗'} relaunch {target}: {detail}")
        if not launched:
            failures.append(f"could not relaunch {target}: {detail}")
    else:
        click.echo(f"{BOLD}{YELLOW}⚠ RELAUNCH THE APP NOW{R} — URLSession caches the proxy config, so an "
                   f"already-running app won't use the PAC until it's relaunched.\n"
                   f"   xcrun simctl terminate booted <bundleid> && xcrun simctl launch booted <bundleid>\n"
                   f"   (or set simBundleId in profile.json)")

    _banner(_health(), service)

    # Exit non-zero unless the whole point of `up` was achieved. Reporting a warning and returning 0
    # meant a script — or an agent — could believe it was mocking when nothing was intercepted.
    if failures:
        # Each failure was already printed inline as it happened, so a single one needs no
        # summary — only collect them when there is more than one to collect.
        if len(failures) > 1:
            click.echo(f"\n{RED}✗ up did not finish cleanly:{R}")
            for failure in failures:
                click.echo(f"   · {failure.splitlines()[0]}")
        else:
            click.echo(f"{RED}✗ up did not finish cleanly.{R}")
        raise SystemExit(1)


def _restore_previous_pac(service: str, runtime: dict) -> dict | None:
    """Put back the PAC recorded at `up`, or return None having touched nothing.

    Returns None when the installed PAC is no longer ours: a PAC the user set by hand while
    Lyrebird was running must survive teardown. Both callers depend on that rule, so it lives
    here rather than being restated in each.
    """
    if not netproxy.pac_status(service).ours:
        return None
    previous = runtime.get("previousPac") or {"url": "", "enabled": False}
    netproxy.restore_pac(service, previous.get("url", ""), previous.get("enabled", False))
    return previous


def _snapshot_pac(service: str) -> dict:
    status = netproxy.pac_status(service)
    if status.ours:
        return {"url": "", "enabled": False}  # never record our own PAC as the thing to restore
    return {"url": status.url, "enabled": status.enabled}


@cli.command()
def down() -> None:
    """Stop the proxy and restore the previous proxy configuration."""
    runtime = config.read_runtime()
    health = _health()

    # The runtime file can be missing or unreadable — deleted by hand, or written by a version
    # that crashed mid-write. Without this, `down` would find nothing to do and cheerfully report
    # "stopped" while the proxy was still running and the PAC still pointing at it. Rediscover
    # what we can: health knows the pid, and the OS knows which service carries the default route.
    if health and not runtime.get("proxyPid"):
        runtime = {**runtime, "proxyPid": health.get("pid")}
    if not runtime.get("service") and (health or netproxy.active_service()):
        runtime = {**runtime, "service": netproxy.active_service()}

    if not health and not runtime:
        click.echo(f"{DIM}nothing to stop — no proxy running and no runtime state{R}")
        return

    # Kill the watchdog FIRST so it can't reinstall the PAC mid-teardown.
    _terminate(runtime.get("watchdogPid"), "_watchdog")

    service = runtime.get("service")
    if service:
        try:
            previous = _restore_previous_pac(service, runtime)
        except netproxy.NetworkSetupError as error:
            click.echo(f"{RED}✗ could not restore proxy settings on '{service}': {error}{R}\n"
                       f"   fix manually: System Settings ▸ Network ▸ {service} ▸ Proxies{R}")
            raise SystemExit(1) from error
        if previous is None:
            click.echo(f"{DIM}PAC on '{service}' is not ours — left untouched{R}")
        elif previous.get("url"):
            click.echo(f"✓ restored the previous PAC on '{service}': {previous['url']}")
        else:
            click.echo(f"✓ PAC removed from '{service}' — direct networking restored")

    _terminate(runtime.get("proxyPid"), "addon.py")
    if config.runtime_file().is_file():
        config.runtime_file().unlink()

    # SIGTERM is a request. Give it a moment and say which actually happened, rather than
    # printing "stopped" over a proxy that is still serving.
    deadline = time.time() + _DOWN_WAIT_SECONDS
    while time.time() < deadline and _health() is not None:
        time.sleep(0.2)
    if _health() is None:
        click.echo(f"{GREEN}stopped{R}")
    else:
        click.echo(f"{YELLOW}⚠ proxy still responding on port {config.CONTROL_PORT} after SIGTERM{R}")
        raise SystemExit(1)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable state on stdout.")
def status(as_json: bool) -> None:
    """Show intercept state (honest about whether the PAC is actually enabled).

    The exit code reports the state, not the formatting: 0 only when the proxy is up *and*
    intercepting, whichever way you asked. `--json` changes what is printed and never what it
    means — a flag that decides how output is rendered must not also decide what success is, or
    `lyrebird status && …` silently proceeds against a proxy that is mocking nothing.
    """
    health = _health()
    service = config.read_runtime().get("service") or netproxy.active_service()

    if as_json:
        pac = netproxy.pac_status(service) if service else None
        click.echo(json.dumps({
            "proxyUp": health is not None,
            "intercepting": netproxy.intercepting(service),
            "activeSession": (health or {}).get("activeSession"),
            "overrideCount": (health or {}).get("overrideCount"),
            "sessions": (health or {}).get("sessions", []),
            # .get with a default: an older engine — or a test double — has no such key, and the
            # exit code of `status` must not depend on this field existing.
            "sequences": (health or {}).get("sequences", []),
            "simBundleId": (health or {}).get("simBundleId"),
            "profile": str(config.PROFILE_DIR),
            "service": service,
            "pac": {"url": pac.url, "enabled": pac.enabled, "ours": pac.ours} if pac else None,
            "dashboard": config.CONTROL_ORIGIN,
        }, indent=2))
    else:
        click.echo(f"{DIM}profile: {config.PROFILE_DIR}{R}")
        _banner(health, service)
        if health:
            click.echo(f"  sessions: {', '.join(health['sessions'])}")
            for state in health.get("sequences", []):
                position = (f"next step {state['nextStep']}/{state['stepCount']}"
                            if state["nextStep"] else f"{RED}exhausted{R}")
                overrun = f" {YELLOW}· overrun{R}" if state["hasOverrun"] else ""
                trigger = "own calls" if state["advanceOn"] == "self" else "advanceOn"
                click.echo(f"  sequence {state['id']}: {position} · {trigger}{overrun}")
            click.echo(f"  dashboard: {CONTROL}/")
        if service:
            pac = netproxy.pac_status(service)
            state = "enabled" if pac.enabled else f"{RED}DISABLED{R}"
            owner = "" if pac.ours or not pac.url else " · not ours"
            click.echo(f"  PAC on '{service}': {pac.url or '(none)'} · {state}{owner}")

    raise SystemExit(0 if health is not None and netproxy.intercepting(service) else 1)


@cli.command()
@click.argument("name")
def use(name: str) -> None:
    """Switch the active session (reports what it displaced)."""
    result = _control("/__mock__/sessions/active", "PUT", {"name": name})
    previous = result.get("previous")
    if previous and previous["name"] != result["active"]:
        click.echo(f"switched: {previous['name']} ({previous['overrideCount']} override(s)) → "
                   f"{BOLD}{result['active']}{R}")
    else:
        click.echo(f"active: {result['active']}")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--matched", "only_matched", is_flag=True, help="Only requests an override answered.")
@click.option("--limit", default=20, help="How many to show.")
def recent(as_json: bool, only_matched: bool, limit: int) -> None:
    """What has come through the proxy, and which overrides answered it."""
    entries = _control("/__mock__/recent") or []
    if only_matched:
        entries = [entry for entry in entries if entry.get("matched")]
    entries = entries[:limit]

    if as_json:
        click.echo(json.dumps(entries, indent=2))
        return
    if not entries:
        click.echo(f"{DIM}(nothing yet){R}")
        return
    for entry in entries:
        mark = f" → {entry['matched']}" if entry.get("matched") else ""
        skipped = f"  {YELLOW}patch skipped: {entry['patchSkipped']}{R}" if entry.get("patchSkipped") else ""
        step = ""
        if entry.get("sequenceId"):
            step = (f"  {DIM}[{entry['sequenceId']} step {entry['selectedStep']}/"
                    f"{entry['stepCount']}]{R}" if entry.get("selectedStep")
                    else f"  {YELLOW}[{entry['sequenceId']} overrun]{R}")
        advanced = f"  {DIM}advanced {', '.join(entry['advanced'])}{R}" if entry.get("advanced") else ""
        click.echo(f"  {entry['method']:6} {entry['status']}  {entry['path']}"
                   f"{mark}{step}{advanced}{skipped}")


@cli.group()
def override() -> None:
    """Add or clear rules in the active session."""


@override.command(name="add")
@click.argument("rule")
def override_add(rule: str) -> None:
    """Add a rule to the active session. RULE is JSON, or - to read stdin.

    Takes effect immediately — no restart, and the session file is updated.

        lyrebird override add '{"match":{"path":"/api/v1/orders/*"},"mode":"replace","status":500}'
    """
    raw = sys.stdin.read() if rule == "-" else rule
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        click.echo(f"{RED}✗ not valid JSON: {error}{R}")
        raise SystemExit(1) from None
    result = _control("/__mock__/overrides", "POST", payload)
    click.echo(f"✓ added {result['id']}")


@override.command(name="clear")
@click.option("--force", is_flag=True, help="Required: this deletes rules and rewrites the file.")
def override_clear(force: bool) -> None:
    """Delete EVERY rule in the active session and rewrite its file. There is no undo."""
    if not force:
        click.echo(f"{RED}✗ refusing without --force{R} — this deletes every override in the "
                   f"active session and rewrites the file on disk.")
        raise SystemExit(1)
    result = _control("/__mock__/overrides", "DELETE")
    click.echo(f"✓ cleared {result['cleared']} override(s) from {result['session']}")


@cli.group()
def session() -> None:
    """Create and remove sessions."""


@session.command(name="new")
@click.argument("name")
@click.option("--clone-from", default=None, help="Start from a copy of this session.")
@click.option("--activate/--no-activate", default=True, help="Switch to it once created.")
def session_new(name: str, clone_from: str | None, activate: bool) -> None:
    """Create a session — use this for scratch work instead of editing a shared one."""
    _control("/__mock__/sessions", "POST", {"name": name, "cloneFrom": clone_from})
    click.echo(f"✓ created {name}" + (f" from {clone_from}" if clone_from else ""))
    if activate:
        _control("/__mock__/sessions/active", "PUT", {"name": name})
        click.echo(f"✓ active: {name}")


@session.command(name="rm")
@click.argument("name")
def session_rm(name: str) -> None:
    """Delete a session and its file."""
    _control(f"/__mock__/sessions/{name}", "DELETE")
    click.echo(f"✓ deleted {name}")


@cli.group()
def sequence() -> None:
    """Inspect and rewind response sequences."""


def _find_sequence(health: dict, override_id: str) -> dict | None:
    for state in health.get("sequences", []):
        if state.get("id") == override_id:
            return state
    return None


@sequence.command(name="reset")
@click.argument("override_id", metavar="[ID]", required=False)
def sequence_reset(override_id: str | None) -> None:
    """Rewind sequences in the active session to their first step.

    With no ID, rewinds every sequence. Cursors are in-memory, so this is the only way to replay a
    scenario without switching sessions.
    """
    result = _control("/__mock__/sequences/reset", "POST", {"id": override_id} if override_id else {})
    reset = result.get("reset") or {}
    if not reset:
        click.echo(f"{DIM}no sequences in '{result.get('session')}' — nothing to reset{R}")
        return
    for name, run_id in reset.items():
        click.echo(f"✓ reset {name} {DIM}(run {run_id}){R}")


@sequence.command(name="wait")
@click.argument("override_id")
@click.option("--step", type=int, required=True, help="Which step to wait for (1-based).")
@click.option("--timeout", default=30, help="Seconds to wait.")
def sequence_wait(override_id: str, step: int, timeout: int) -> None:
    """Block until a sequence serves a given step, in the run current when the wait began.

    Both timeout-shaped mistakes are answered up front instead: a step already served this run
    succeeds immediately (reset → trigger → wait is the documented order, and the action may land
    before the wait starts), and a run already past the step without serving it fails immediately —
    an agent that waits 30 seconds for either learns the wrong thing about why.
    """
    health = _health()
    if health is None:
        # "Could not read it" is a different claim from "there is nothing here".
        click.echo(f"{RED}✗ cannot reach the control API — is the proxy up?{R}")
        raise SystemExit(1)
    state = _find_sequence(health, override_id)
    if state is None:
        click.echo(f"{RED}✗ no sequence '{override_id}' in the active session{R}")
        raise SystemExit(1)
    if not 1 <= step <= state["stepCount"]:
        click.echo(f"{RED}✗ '{override_id}' has {state['stepCount']} step(s); "
                   f"--step {step} is out of range{R}")
        raise SystemExit(1)

    run_id, next_step = state["runId"], state["nextStep"]
    # A serve already recorded for this run IS the postcondition: the counter lives in the runtime
    # entry a reset drops, so everything in it happened after the last reset. The documented
    # workflow is reset → trigger the action → wait, and if the action lands before the wait
    # starts, refusing (or timing out) would report failure on a transition that completed.
    already = (state.get("serves") or {}).get(str(step), 0)
    if already:
        click.echo(f"✓ {override_id} served step {step}/{state['stepCount']} this run "
                   f"({already}×, before the wait began)")
        return

    # Fail now, not at the deadline. This has to come from live state rather than the traffic
    # buffer: /recent holds a bounded window, so the event may have been evicted while the fact
    # that it happened is still true. Ordered after the serve check: a cursor past the step with
    # no serve recorded means the run advanced over the step without ever serving it.
    if next_step is None or next_step > step:
        position = "exhausted" if next_step is None else f"now at step {next_step}"
        click.echo(f"{RED}✗ '{override_id}' is already past step {step} ({position}) and never "
                   f"served it this run.{R}\n"
                   f"   Run `lyrebird sequence reset {override_id}` before triggering the action.")
        raise SystemExit(1)

    # The observation comes from the live serve counter, not /recent: /recent is a bounded window,
    # so under enough traffic the serve could be evicted between polls — the wait would then time
    # out on something that happened. The counter cannot be evicted, and a reset clears it with
    # the runtime entry it lives in.
    deadline = time.time() + timeout
    unreachable = False
    current: dict | None = None
    while True:
        current_health = _health()
        if current_health is None:
            unreachable = True   # transient until the deadline says otherwise; keep polling
        else:
            unreachable = False
            current = _find_sequence(current_health, override_id)
            if current is None or current.get("runId") != run_id:
                what = "was removed" if current is None else "was reset"
                click.echo(f"{RED}✗ '{override_id}' {what} while waiting; "
                           f"this wait's baseline no longer applies.{R}")
                raise SystemExit(1)
            if (current.get("serves") or {}).get(str(step), 0):
                detail = next((e for e in _get_json("/__mock__/recent", timeout=2) or []
                               if e.get("sequenceId") == override_id and e.get("runId") == run_id
                               and e.get("selectedStep") == step), None)
                served = f"✓ {override_id} served step {step}/{current['stepCount']}"
                if detail:
                    click.echo(f"{served} for {detail['method']} {detail['path']} → {detail['status']}")
                else:
                    click.echo(served)   # the serve outlived its /recent entry; the counter is the proof
                return
            now_next = current.get("nextStep")
            if now_next is None or now_next > step:
                # Checked after the serve: a run that served the step and then advanced is a
                # success, but one that advanced over it without serving can never satisfy this
                # wait — burning the rest of the timeout would blame the wrong thing.
                position = "exhausted" if now_next is None else f"now at step {now_next}"
                click.echo(f"{RED}✗ '{override_id}' advanced past step {step} without serving it "
                           f"({position}).{R}\n"
                           f"   Something advanced the sequence that was not the request you were "
                           f"waiting for; check `lyrebird recent`.")
                raise SystemExit(1)
        if time.time() >= deadline:
            break
        time.sleep(1)

    if unreachable:
        click.echo(f"{RED}✗ lost the control API while waiting — the proxy may have stopped; "
                   f"whether step {step} was served is unknown.{R}")
    else:
        click.echo(f"{RED}✗ '{override_id}' did not serve step {step} within {timeout}s "
                   f"(next step: {(current or state).get('nextStep')}).{R}\n"
                   f"   Check `lyrebird recent` for what did arrive.")
    raise SystemExit(1)


@cli.command(name="wait-ready")
@click.option("--timeout", default=30, help="Seconds to wait.")
@click.option("--match", "want_match", is_flag=True,
              help="Wait for a request an override actually matched, not just any traffic.")
def wait_ready(timeout: int, want_match: bool) -> None:
    """Block until the app's traffic reaches the proxy (avoids cold-launch flakiness).

    With --match, wait until an override actually fires. Traffic arriving proves the PAC works;
    it does not prove your rule matched, which is usually the thing you are waiting to confirm.
    """
    def newest(entries: list) -> str:
        return entries[0].get("time", "") if entries else ""

    # Only traffic that arrives from now on counts. Without this the retained buffer could
    # satisfy the wait instantly with a request made before the session was even switched.
    baseline = newest(_get_json("/__mock__/recent", timeout=2) or [])
    deadline = time.time() + timeout
    while time.time() < deadline:
        recent = [e for e in (_get_json("/__mock__/recent", timeout=2) or [])
                  if e.get("time", "") > baseline]
        matched = [entry for entry in recent if entry.get("matched")]
        if matched if want_match else recent:
            if want_match:
                hit = matched[0]
                click.echo(f"✓ override {hit['matched']} matched {hit['method']} {hit['path']} "
                           f"→ {hit['status']}")
            else:
                click.echo(f"✓ app is live ({len(recent)} proxied request(s) seen)")
            return
        time.sleep(1)
    if want_match:
        seen = len([e for e in (_get_json("/__mock__/recent", timeout=2) or [])
                    if e.get("time", "") > baseline])
        click.echo(f"{RED}✗ no override matched within {timeout}s ({seen} request(s) reached the "
                   f"proxy). Check the path in your rule against `lyrebird logs`.{R}")
    else:
        click.echo(f"{RED}✗ no proxied requests within {timeout}s — is the app relaunched, and is it "
                   f"calling a host listed in your profile?{R}")
    raise SystemExit(1)


@cli.command(name="trust-ca")
def trust_ca_cmd() -> None:
    """(Re)trust the Lyrebird CA in the booted simulator."""
    ok, message = trust_ca_in_sim()
    click.echo(f"{'✓' if ok else '✗'} {message}")
    if not ok:
        raise SystemExit(1)


@cli.command(name="untrust-ca")
def untrust_ca_cmd() -> None:
    """Explain how to remove the Lyrebird CA from the simulator."""
    click.echo("simctl exposes no remove-root-cert; to drop trust use either:\n"
               "  xcrun simctl keychain booted reset        # clears added certs on the booted sim\n"
               "  Device ▸ Erase All Content and Settings   # full reset\n"
               f"\nLyrebird's CA lives in {config.mitmproxy_confdir()} — delete that directory to\n"
               "rotate it; a new one is generated on the next `up`.")


@cli.command()
def logs() -> None:
    """Print the last 60 lines of the proxy log (not a follow — use `tail -f` on the path shown)."""
    click.echo(_tail_log(60))
    click.echo(f"{DIM}{config.LOG_FILE}{R}", err=True)


@cli.command(name="_watchdog", hidden=True)
@click.argument("service")
def watchdog(service: str) -> None:
    while True:
        if _health() is None:
            # Proxy gone → put back whatever the user had, rather than merely switching off.
            with contextlib.suppress(netproxy.NetworkSetupError):
                _restore_previous_pac(service, config.read_runtime())
            # Clear the runtime file: the settings it describes have been put back, so a later
            # `up` must snapshot the network afresh rather than trust this record.
            with contextlib.suppress(OSError):
                config.runtime_file().unlink()
            return
        pac = netproxy.pac_status(service)
        if not (pac.enabled and pac.ours) and pac.url in ("", netproxy.pac_url()):
            with contextlib.suppress(netproxy.NetworkSetupError):
                # macOS silently disabled our PAC while the proxy is alive
                netproxy.set_pac(service)
        time.sleep(2)


# MARK: - Presentation

def _start_fresh_log() -> None:
    """Begin each run on a new inode rather than truncating the old one.

    The log names every host and path that came through, so it is 0600 — but a log left by an
    older version may be 0644, and `chmod` cannot revoke a descriptor somebody already holds.
    Truncating in place keeps that inode, so a reader who opened it while it was readable goes on
    seeing new traffic. Renaming a fresh private file over it leaves them holding the old one.
    """
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(config.LOG_FILE, "")


def _tail_log(lines: int) -> str:
    if not config.LOG_FILE.is_file():
        return "(no log)"
    return "\n".join(config.LOG_FILE.read_text(errors="replace").splitlines()[-lines:])


def _banner(health: dict | None, service: str | None) -> None:
    if health is None:
        click.echo(f"{DIM}⚪ proxy not reachable{R}")
        return
    if netproxy.intercepting(service):
        click.echo(f"{BOLD}{RED}🔴 INTERCEPT ACTIVE{R}  "
                   f"session {BOLD}{health['activeSession']}{R} · "
                   f"{health['overrideCount']} override(s) · proxy :{health['proxyPort']} · PAC on {service}")
    else:
        click.echo(f"{BOLD}{YELLOW}🟠 PROXY UP BUT NOT INTERCEPTING{R} — PAC is disabled/not ours. "
                   f"Run {BOLD}lyrebird up{R} to (re)install it.")


if __name__ == "__main__":
    cli()
