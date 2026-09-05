"""lyrebird — supervisor and macOS integration for the Lyrebird mock proxy.

    lyrebird init [PATH]                  create a profile from the bundled examples
    lyrebird up [--relaunch <bundleid>]   start proxy, trust CA, install host-scoped PAC
    lyrebird down                         stop proxy and restore the previous proxy settings
    lyrebird use <session>                switch active session (reports what it displaced)
    lyrebird recent [--json] [--matched]  what came through, and which overrides answered
    lyrebird override add <json>          add a rule to the active session, no restart
    lyrebird explain-match <method> <path>  which rule would be selected, and why the rest were not
    lyrebird assert-answered <id>         exit non-zero unless that rule answered this run
    lyrebird session new <name>           create a scratch session (--clone-from X)
    lyrebird reset [id]                   start a fresh run: rewind sequences, clear answer counts
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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import click

import config
import netproxy
import rules

MITMDUMP = config.ROOT / ".venv" / "bin" / "mitmdump"
CONTROL = config.CONTROL_ORIGIN
_DOWN_WAIT_SECONDS = 5.0   # how long `down` waits for SIGTERM to take effect
_WATCHDOG_RESTORE_ATTEMPTS = 5   # `networksetup` fails transiently; one try is not a restore
_LOCK_WAIT_SECONDS = 60.0   # how long `up` waits for another `up`, or a watchdog restore, to finish

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
            # `detail` first: the API sends the sentence that names the problem ("match: unknown
            # field 'kind' — a matcher may only carry method, path, query, bodyContains") and
            # `error` only a slug for it. Printing the slug throws away the half that tells you
            # what to do, which is how a supported matcher field ends up looking unsupported.
            body = json.loads(error.read().decode())
            detail = body.get("detail") or body.get("error") or error.reason
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
    """Read the profile, for the one command that needs its contents.

    `up` is where a malformed profile aborts. Every other command — `down` above all — works from
    runtime state and the live API, so a broken profile.json cannot stop you restoring the network.
    """
    config.reload_profile()
    if not config.PROFILE.exists:
        raise SystemExit(
            f"{RED}no profile at {config.PROFILE_DIR}{R}\n"
            f"  create one:   lyrebird init {config.PROFILE_DIR}\n"
            f"  or point at an existing one:  lyrebird --profile /path/to/profile ...\n"
            f"  (a profile is a directory containing profile.json and sessions/)"
        )
    # Refused, not warned about: with no hosts there is nothing `up` could achieve, and it
    # used to trust the CA, install a DIRECT-only PAC, relaunch the app, print INTERCEPT ACTIVE
    # and exit 0 — every step a success, the postcondition not met. The addon still honours an
    # empty list as "intercept nothing"; this is the command named for intercepting declining
    # to claim it did.
    if not config.INTERCEPT_HOSTS:
        raise SystemExit(
            f"{RED}profile at {config.PROFILE_DIR} lists no hosts — nothing would be intercepted{R}\n"
            f"  add the hostname your app calls to `hosts` in {config.PROFILE_FILE}"
        )


# MARK: - CLI

@click.group()
@click.option("--profile", type=click.Path(), default=None,
              help="Profile directory (overrides $LYREBIRD_PROFILE).")
def cli(profile: str | None) -> None:
    if profile:
        # Paths only; `_require_profile` reads the contents. Not exported into os.environ: the two
        # child processes get it from `_child_env`, and a process-wide side effect from an
        # argument parser is what made an in-process test leak its profile into the next one.
        config.configure(profile)


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
        _acquire_lock(lock)
        _up_locked(bundle_id)


def _acquire_lock(lock: Any, timeout: float = _LOCK_WAIT_SECONDS) -> None:
    """Take the per-port lock, waiting for a holder to finish rather than failing at once.

    The holder is another `up`, or the watchdog putting the network back after a crash — and the
    second is exactly the moment an `up` must not start: an install interleaved with a restore on
    one network service ends with the new proxy's PAC restored over and its runtime file deleted.
    Both holders finish on their own within seconds, so waiting is right; the timeout is for a
    holder that did not.
    """
    deadline = time.time() + timeout
    waiting = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.time() >= deadline:
                raise SystemExit(
                    f"{RED}another `lyrebird up` or a watchdog restore is still in progress on port "
                    f"{config.CONTROL_PORT} after {timeout:g}s{R}") from None
            if not waiting:
                click.echo(f"{DIM}waiting for another `lyrebird up` or a watchdog restore to finish…{R}")
                waiting = True
            time.sleep(0.5)


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

    # The recorded service is a fallback, not merely a default. After a crash and a restore that
    # failed, the record names the service whose PAC is still ours; losing the route meanwhile
    # (Wi-Fi off) must not turn that into "no service", which strands the record with nothing to
    # restore it on and lets `down` delete it.
    service = netproxy.active_service() or runtime.get("service")
    recorded = runtime.get("previousPac")
    state = {"proxyPid": proxy_pid, "service": service}
    if recorded:
        # Kept until a successful read says otherwise. The record is what the watchdog left when
        # it could not restore; a PAC read that fails below must not cost it, or `down` restores
        # "nothing" over the user's PAC once reads work again.
        state["previousPac"] = recorded
    # Record the pid before anything else can fail: if PAC installation raises, `down` must still
    # be able to find and stop the proxy we just started.
    config.write_runtime(state)

    if service:
        # Carry a recorded previousPac forward while it still describes the network: a proxy is
        # running, or the installed PAC is one Lyrebird is answerable for — ours, or the recorded
        # URL that a failed restore handed back with the wrong flag. Both are the watchdog having
        # kept the record because it could not restore; snapshotting now would read "ours, so
        # nothing" or the wrong flag, and the user's PAC would be lost or restored wrong at the
        # next `down`. Once the installed PAC is somebody else's the record is stale (a crash,
        # then settings changed by hand), and the network is snapshotted afresh.
        try:
            installed = netproxy.pac_status(service)
            keep = recorded is not None and (existing is not None or _still_restorable(installed, recorded))
            state["previousPac"] = recorded if keep else _snapshot_pac(installed)
            config.write_runtime(state)
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

    try:
        intercepting = netproxy.intercepting(service)
    except netproxy.NetworkSetupError as error:
        # Said here, in place of the banner: the banner's "PAC is disabled/not ours" would be a
        # diagnosis this command never made, and the summary below only names failures that
        # were printed as they happened.
        click.echo(f"{RED}✗ could not read the PAC on '{service}' after installing it: {error}{R}")
        failures.append(f"could not read the PAC on '{service}': {error}")
    else:
        _banner(_health(), service, intercepting)

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

    Returns None when the installed PAC is neither ours nor the one being restored: a PAC the
    user set by hand while Lyrebird was running must survive teardown. The second clause is what
    makes a retry work — `restore_pac` changes the URL before the enabled state, so an attempt
    that failed between the two has already handed the URL back and only the flag is wrong.
    Reading that as "not ours" left it wrong for good, because the runtime file was then
    discarded. Both callers depend on this rule, so it lives here rather than in each.
    """
    status = netproxy.pac_status(service)
    previous = runtime.get("previousPac") or {"url": "", "enabled": False}
    if not _still_restorable(status, previous):
        return None
    netproxy.restore_pac(service, previous.get("url", ""), previous.get("enabled", False))
    return previous


def _still_restorable(status: netproxy.PacStatus, previous: dict) -> bool:
    """Is the installed PAC one Lyrebird is answerable for? Ours, or the recorded previous URL —
    which a restore that failed between its two `networksetup` calls has already handed back, with
    the wrong enabled flag. Anything else was set by hand and is left alone. One predicate for
    `down`, the watchdog and `up`, so they cannot disagree about whose PAC it is."""
    url = previous.get("url", "")
    return status.ours or bool(url and status.url == url)


def _snapshot_pac(status: netproxy.PacStatus) -> dict:
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
    # One observation of the PAC feeds the output and the exit code alike. Reading it once for
    # the banner, again for the JSON field and a third time for the exit status let a PAC that
    # flips in between produce output that contradicts the exit code — a `status` whose text says
    # one thing and whose `$?` says another is worse than either being wrong.
    pac_error = None
    try:
        pac = netproxy.pac_status(service) if service else None
    except netproxy.NetworkSetupError as error:
        # Unproven, which is different from seen to be off — the exit code is the same, the
        # explanation is not, and a person reading "DISABLED" would go and switch it on.
        pac, pac_error = None, str(error)
    intercepting = pac is not None and pac.enabled and pac.ours

    if as_json:
        click.echo(json.dumps({
            "proxyUp": health is not None,
            "intercepting": intercepting,
            "pacError": pac_error,
            "activeSession": (health or {}).get("activeSession"),
            "overrideCount": (health or {}).get("overrideCount"),
            "sessions": (health or {}).get("sessions", []),
            # `null` when the running engine did not supply the field, never `[]`. A current
            # engine always sends both, with one entry per rule — so `[]` is a real state ("no
            # rules here") and a missing key is a capability signal ("this proxy cannot tell
            # you"). Defaulting to `[]` collapsed those into the claim that nothing has answered,
            # which is the shape this file exists to avoid. The exit code is computed separately
            # and still does not depend on either field existing.
            "sequences": (health or {}).get("sequences"),
            "answers": (health or {}).get("answers"),
            "simBundleId": (health or {}).get("simBundleId"),
            "profile": str(config.PROFILE_DIR),
            "service": service,
            "pac": {"url": pac.url, "enabled": pac.enabled, "ours": pac.ours} if pac else None,
        }, indent=2))
    else:
        click.echo(f"{DIM}profile: {config.PROFILE_DIR}{R}")
        if pac_error and health is not None:
            # Not the banner: its "PAC is disabled/not ours" is a diagnosis this read never made.
            click.echo(f"{BOLD}{YELLOW}🟠 PROXY UP, PAC UNREADABLE{R} — could not read the PAC on "
                       f"'{service}': {pac_error}")
        else:
            _banner(health, service, intercepting)
        if health:
            click.echo(f"  sessions: {', '.join(health['sessions'])}")
            for state in health.get("sequences", []):
                position = (f"next step {state['nextStep']}/{state['stepCount']}"
                            if state["nextStep"] else f"{RED}exhausted{R}")
                overrun = f" {YELLOW}· overrun{R}" if state["hasOverrun"] else ""
                trigger = "own calls" if state["advanceOn"] == "self" else "advanceOn"
                click.echo(f"  sequence {state['id']}: {position} · {trigger}{overrun}")
        if pac is not None:
            state = "enabled" if pac.enabled else f"{RED}DISABLED{R}"
            owner = "" if pac.ours or not pac.url else " · not ours"
            click.echo(f"  PAC on '{service}': {pac.url or '(none)'} · {state}{owner}")

    raise SystemExit(0 if health is not None and intercepting else 1)


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


# Built from the vocabulary itself, so the help cannot claim a different set of fields from the one
# validation accepts. A matcher field nobody can discover is reported as a missing feature — and the
# rule people write instead is a broader one that quietly answers for its neighbours.
_ADD_HELP = """Add a rule to the active session. RULE is JSON, or - to read stdin.

Takes effect immediately — no restart, and the session file is updated.

\b
    lyrebird override add '{"match":{"path":"/api/v1/orders/*"},"mode":"replace","status":500}'

`match` accepts these fields, and only these:

\b
""" + "\n".join(
    f"    {field:<13} {description}" for field, description in rules.MATCHER_FIELD_HELP.items()
) + """

Constrain a rule as tightly as the thing you are testing. Sibling screens served from one path are
told apart by `query`, and a rule that leaves it out answers for all of them:

\b
    lyrebird override add '{"match":{"method":"GET","path":"/api/items","query":{"kind":"alpha"}},
                            "mode":"replace","status":200,"body":{}}'

`lyrebird explain-match GET '/api/items?kind=alpha'` shows which rule a request would select, and
which others it would also have matched.
"""


@override.command(name="add", help=_ADD_HELP)
@click.argument("rule")
def override_add(rule: str) -> None:
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
                   f"   Run `lyrebird reset {override_id}` before triggering the action.")
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


@cli.command()
@click.argument("override_id", metavar="[ID]", required=False)
def reset(override_id: str | None) -> None:
    """Start a fresh run: rewind sequences to step 1 and clear answer counts.

    With no ID, resets every rule in the active session. Run this immediately before the action you
    are about to test, not once at start-up — the app's launch fetches land in between, and evidence
    they leave behind would satisfy an `assert-answered` the test itself never earned.

    Run state is in memory, so this is also the only way to replay a scenario without switching
    sessions.
    """
    result = _control("/__mock__/reset", "POST", {"id": override_id} if override_id else {})
    reset_ids = result.get("reset") or {}
    if not reset_ids:
        click.echo(f"{DIM}no rules in '{result.get('session')}' — nothing to reset{R}")
        return
    for name, run_id in reset_ids.items():
        click.echo(f"✓ reset {name} {DIM}(run {run_id}){R}")


@cli.command(name="assert-answered")
@click.argument("override_id")
@click.option("--timeout", default=0, help="Seconds to wait for the first answer (0 checks now).")
def assert_answered(override_id: str, timeout: int) -> None:
    """Exit non-zero unless that rule has answered a request since the last reset.

    The assertion a test can make about its own setup. A negative UI assertion — "this section is
    not shown" — passes identically whether the mock applied or never matched, because the real
    backend usually produces the same screen. So a green suite can be testing nothing, and stays
    green when a rule quietly stops matching. This is the command that fails instead.

    The count is taken where the answer is produced, so it survives eviction from the traffic list
    and can never be satisfied by a rule that merely matched and lost. `lyrebird reset` draws the
    boundary: reset, trigger the action, assert.
    """
    if timeout < 0:
        click.echo(f"{RED}✗ --timeout must not be negative{R}")
        raise SystemExit(1)

    deadline = time.time() + timeout
    while True:
        health = _health()
        if health is None:
            # Distinct from "it answered nothing": we could not ask. Falling through to the
            # zero-answer message would send someone to debug a rule that may be perfectly fine.
            click.echo(f"{RED}✗ cannot reach the control API — is the proxy up?{R}")
            raise SystemExit(1)
        if "answers" not in health:
            click.echo(f"{RED}✗ this proxy does not report answer counts — restart it "
                       f"(`lyrebird down && lyrebird up`) to pick up the current engine.{R}")
            raise SystemExit(1)

        state = next((s for s in health["answers"] if s.get("id") == override_id), None)
        if state is None:
            # A typo must not read as "it never fired": different bug, different fix.
            active = health.get("activeSession")
            click.echo(f"{RED}✗ no rule '{override_id}' in session '{active}'{R}")
            raise SystemExit(1)
        count = state.get("count", 0)
        if count:
            click.echo(f"✓ {override_id} answered {count} request(s) this run")
            return
        if not state.get("active", True):
            # Waiting cannot help — matching skips a disabled rule entirely.
            click.echo(f"{RED}✗ '{override_id}' is not active, so it can never answer.{R}")
            raise SystemExit(1)
        # A reset landing mid-wait is deliberately not special-cased: a count can only be observed
        # falling if it was seen above zero first, and a count above zero has already returned. The
        # wait simply runs out and says the rule has not answered this run, which is true.
        if time.time() >= deadline:
            break
        time.sleep(1)

    entries = _get_json("/__mock__/recent", timeout=2) or []
    waited = f" within {timeout}s" if timeout else ""
    click.echo(f"{RED}✗ '{override_id}' has not answered any request this run{waited}.{R}")
    if not entries:
        click.echo("   Nothing has reached the proxy at all — relaunch the app, and check the host "
                   "is listed in your profile.")
    else:
        # The distinct paths, not the count: a count cannot tell "the app went somewhere else"
        # from "the path pattern is wrong", and those have completely different fixes.
        seen = list(dict.fromkeys(
            f"{entry.get('method', '?'):6} {entry.get('path', '?')}" for entry in entries))
        click.echo(f"   {len(entries)} request(s) in the recent buffer, on these paths:")
        for line in seen[:5]:
            click.echo(f"     {DIM}{line}{R}")
        if len(seen) > 5:
            click.echo(f"     {DIM}… and {len(seen) - 5} more{R}")
        # The buffer is not scoped to the run, so these may predate the reset. Enough to tell
        # "the app is not reaching us" from "it is, on other paths"; not enough to blame the rule.
        click.echo("   `lyrebird explain-match <method> <path>` says which rule one of those "
                   "would select, and why yours was not it.")
    raise SystemExit(1)


def _section(title: str, rows: list[tuple[str, str]]) -> None:
    """One labelled block of `explain-match` output, or nothing if it has no rows."""
    if not rows:
        return
    click.echo(f"  {title}:")
    for name, note in rows:
        click.echo(f"    {name:<16}{DIM}{note}{R}")


@cli.command(name="explain-match")
@click.argument("method")
@click.argument("path")
@click.option("--body", default="", help="Request body text, for rules using bodyContains.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def explain_match(method: str, path: str, body: str, as_json: bool) -> None:
    """Which rule a request would select, and why each of the others would not.

    Answers at the keyboard what otherwise costs a cold launch and a walk through the app. It also
    separates the two things "no match" collapses together: a rule this says would be selected, that
    then never fires, means the app did not make the request you assumed it did.

    PATH may carry a query string: `lyrebird explain-match GET '/api/items?kind=alpha'`.

    It reports what would be *selected*, never what would be returned. A `patch` answers only if the
    upstream response turns out to be JSON, and which step a sequence would serve depends on run
    state this deliberately neither reads nor touches.
    """
    split = urllib.parse.urlsplit(path)
    # First value wins for a repeated key, as it does on the wire: the addon builds its query from
    # mitmproxy's MultiDict, whose lookup returns the first. `dict(parse_qsl(...))` keeps the last,
    # and for `?kind=alpha&kind=beta` this command then explained a selection the proxy never made.
    query: dict[str, str] = {}
    for key, value in urllib.parse.parse_qsl(split.query, keep_blank_values=True):
        query.setdefault(key, value)
    overrides = _control("/__mock__/overrides") or []
    selected = rules.find_override(overrides, method, split.path, query, body)

    report = []
    for override in overrides:
        reason = rules.explain_matcher(override.get("match") or {}, method, split.path, query, body)
        report.append({
            "id": override.get("id"),
            "active": rules.is_active(override),
            "matched": reason is None,
            "reason": reason,
            "selected": override is selected,
            "match": override.get("match") or {},
            "sequenced": rules.sequence_steps(override) is not None,
        })

    if as_json:
        click.echo(json.dumps({"selected": (selected or {}).get("id"), "candidates": report},
                              indent=2))
        raise SystemExit(0 if selected else 1)

    if selected is None:
        click.echo(f"{RED}✗ no active rule would be selected for {method.upper()} {path}{R}")
    else:
        sequenced = next(c["sequenced"] for c in report if c["selected"])
        click.echo(f"→ {BOLD}{selected['id']}{R} is selected  "
                   f"{DIM}({'sequence' if sequenced else selected.get('mode')}){R}")
        if selected.get("mode") == "patch":
            click.echo(f"  {DIM}a patch answers only if the upstream response is JSON{R}")
        elif sequenced:
            click.echo(f"  {DIM}which step it serves depends on run state, not read here{R}")

    # The over-match, made visible before it silently answers for a screen nobody is testing.
    _section("also matched, ranked lower",
           [(c["id"], json.dumps(c["match"]))
            for c in report if c["matched"] and c["active"] and not c["selected"]])
    _section("did not match",
           [(c["id"], c["reason"]) for c in report if not c["matched"] and c["active"]])
    _section("inactive",
           [(c["id"], "would have matched" if c["matched"] else c["reason"])
            for c in report if not c["active"]])

    raise SystemExit(0 if selected else 1)


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
            if _restore_after_death(service):
                return
            continue   # a replacement proxy is live: go back to watching it
        try:
            pac = netproxy.pac_status(service)
        except netproxy.NetworkSetupError:
            time.sleep(2)   # unknown is not "off": neither reinstall nor give up, just ask again
            continue
        if not (pac.enabled and pac.ours) and pac.url in ("", netproxy.pac_url()):
            with contextlib.suppress(netproxy.NetworkSetupError):
                # macOS silently disabled our PAC while the proxy is alive
                netproxy.set_pac(service)
        time.sleep(2)


def _restore_after_death(service: str) -> bool:
    """The watchdog's job once health is gone: put back whatever the user had, rather than
    merely switching off. True when that job is finished — restored, or given up with the runtime
    file left for `down` — and False when a replacement proxy turns out to be live.

    Under the lock `up` takes while it starts a proxy and installs its PAC, with health checked
    again once it is held. `up` reuses a running watchdog rather than spawning another, so without
    the lock a replacement starting during this restore had its PAC restored over and then its
    runtime file deleted; the health check before the lock was too early to see it.

    Several attempts, not one: a network change in progress is a common reason for a proxy to
    die, and it makes `networksetup` fail for a moment too. Giving up on the first error left
    the Mac routed at a dead port with nobody left to fix it.
    """
    with open(config.lock_file(), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _health() is not None:
            return False
        for _ in range(_WATCHDOG_RESTORE_ATTEMPTS):
            try:
                _restore_previous_pac(service, config.read_runtime())
            except netproxy.NetworkSetupError:
                time.sleep(2)
                continue
            # Clear the runtime file: the settings it describes have been put back, so a later
            # `up` must snapshot the network afresh rather than trust this record.
            with contextlib.suppress(OSError):
                config.runtime_file().unlink()
            return True
        # Could not read the PAC, or could not put it back. The runtime file is the only record
        # of what to restore, so it stays for `down` — or the next `up` — to act on; deleting it
        # here is how a failed restore used to become permanent and invisible.
        return True


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


def _banner(health: dict | None, service: str | None, intercepting: bool) -> None:
    """Takes the PAC verdict rather than reading it, so the caller's one observation is the one
    shown."""
    if health is None:
        click.echo(f"{DIM}⚪ proxy not reachable{R}")
        return
    if intercepting:
        click.echo(f"{BOLD}{RED}🔴 INTERCEPT ACTIVE{R}  "
                   f"session {BOLD}{health['activeSession']}{R} · "
                   f"{health['overrideCount']} override(s) · proxy :{health['proxyPort']} · PAC on {service}")
    else:
        click.echo(f"{BOLD}{YELLOW}🟠 PROXY UP BUT NOT INTERCEPTING{R} — PAC is disabled/not ours. "
                   f"Run {BOLD}lyrebird up{R} to (re)install it.")


if __name__ == "__main__":
    cli()
