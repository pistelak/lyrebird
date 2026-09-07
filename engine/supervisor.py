"""Starting and stopping the proxy: the lock, the PAC, the watchdog, and what `up` must achieve."""

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
from pathlib import Path
from typing import Any

import click

import api
import config
import netproxy
import simulator as sim
import store
import ui

MITMDUMP = config.ROOT / ".venv" / "bin" / "mitmdump"
_DOWN_WAIT_SECONDS = 5.0  # how long `down` waits for SIGTERM to take effect
_WATCHDOG_RESTORE_ATTEMPTS = 5  # `networksetup` fails transiently; one try is not a restore
# An attempt is four `networksetup`/`route` calls, each bounded by `netproxy._COMMAND_TIMEOUT`,
# so a hung command costs ~20s per attempt rather than the whole restore.
_LOCK_WAIT_SECONDS = 60.0  # how long `up` waits for another `up`, or a watchdog restore, to finish


def _discover_service() -> tuple[str | None, str | None]:
    """The service carrying the default route, or the reason we could not find out.

    `netproxy.active_service` can raise now that its `route`/`networksetup` calls are bounded, and
    the three commands that ask are the three that must not die of it: `status --json` would print
    no JSON, `down` would abort before restoring anything, and `up` would exit with a proxy running
    and no pid recorded. The substitution is made here rather than inside `active_service`, because
    returning None there would claim "no default route" about a command that never answered — and
    each caller below has both a fallback (the service recorded by the last `up`) and somewhere to
    put the reason, which `active_service` has neither of.
    """
    try:
        return netproxy.active_service(), None
    except netproxy.NetworkSetupError as error:
        return None, str(error)


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
    # `sim._run` is the generic capture-output subprocess helper; it lives in `simulator.py`
    # because every simctl call goes through it, and `fake_simctl` in the tests replaces that one.
    result = sim._run(["ps", "-p", str(pid), "-o", "command="])
    return marker in result.stdout


def _terminate(pid: int | None, marker: str) -> None:
    if pid is None or not _pid_is_ours(pid, marker):
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def _child_env() -> dict:
    return {**os.environ, "LYREBIRD_PROFILE": str(config.PROFILE_DIR)}


def _spawn_watchdog(service: str) -> int:
    proc = subprocess.Popen(
        [sys.executable, str(config.ROOT / "cli.py"), "_watchdog", service],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
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
            f"{ui.RED}no profile at {config.PROFILE_DIR}{ui.R}\n"
            f"  create one:   lyrebird init {config.PROFILE_DIR}\n"
            f"  or point at an existing one:  lyrebird --profile /path/to/profile ...\n"
            f"  (a profile is a directory containing profile.json and scenarios/)"
        )
    # Refused, not warned about: with no hosts there is nothing `up` could achieve, and it
    # used to trust the CA, install a DIRECT-only PAC, relaunch the app, print INTERCEPT ACTIVE
    # and exit 0 — every step a success, the postcondition not met. The addon still honours an
    # empty list as "intercept nothing"; this is the command named for intercepting declining
    # to claim it did.
    if not config.INTERCEPT_HOSTS:
        raise SystemExit(
            f"{ui.RED}profile at {config.PROFILE_DIR} lists no hosts — nothing would be intercepted{ui.R}\n"
            f"  add the hostname your app calls to `hosts` in {config.PROFILE_FILE}"
        )


@click.command()
@click.argument("path", type=click.Path(), required=False)
def init(path: str | None) -> None:
    """Create a profile from the bundled examples."""
    target = Path(path).expanduser().resolve() if path else config.PROFILE_DIR
    if (target / "profile.json").exists():
        raise SystemExit(f"{ui.RED}{target}/profile.json already exists — refusing to overwrite{ui.R}")
    # Checked on the target, not on `config.PROFILE_DIR`: `init PATH` writes somewhere else. Copying
    # the examples into a profile that still has `sessions/` would leave two directories of scenarios
    # with only one of them read — see test_init_refuses_a_legacy_sessions_layout.
    try:
        store.refuse_legacy_layout(target)
    except store.LegacyProfileLayout as error:
        raise SystemExit(f"{ui.RED}{error}{ui.R}") from None
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(config.EXAMPLES_DIR, target, dirs_exist_ok=True)
    click.echo(f"✓ profile created at {ui.BOLD}{target}{ui.R}")
    click.echo(
        f"  Edit {target}/profile.json: set `hosts` to the API your app calls, and\n"
        f"  `simBundleId` to your app's bundle identifier. The examples are a schema\n"
        f"  template, not a runnable demo — api.example.com serves none of these paths."
    )
    click.echo(f"  Then:  lyrebird --profile {target} up")


@click.command()
@click.option(
    "--relaunch",
    "bundle_id",
    default=None,
    help="Terminate + relaunch this app bundle id after up (strongly recommended).",
)
@click.option(
    "--no-relaunch",
    is_flag=True,
    default=False,
    help="Launch nothing; the caller starts the app once `up` has exited 0.",
)
@click.option(
    "--use", "use_name", default=None, help="Select this scenario before the app is relaunched, so the launch meets it."
)
@click.option(
    "--simulator",
    "simulator_selector",
    default=None,
    metavar="UDID-OR-NAME",
    help="Which simulator to trust the CA in and relaunch on. Defaults to the booted "
    "one; required when more than one is booted.",
)
def up(bundle_id: str | None, no_relaunch: bool, use_name: str | None, simulator_selector: str | None) -> None:
    """Start the proxy, trust the CA in the simulator, and install the host-scoped PAC.

    With `--use NAME` the scenario is selected — and its sequences rewound — before the app is
    relaunched, so the app's launch requests are answered by that scenario rather than by
    whichever scenario was last active. That is what a `use` afterwards is too late to fix for an
    app that caches its launch response.

    With `--no-relaunch` nothing is launched: pass it when a UI runner owns the app, and start it
    yourself once `up` has exited 0.

    `--simulator` binds the CA and the relaunch to one device, which is what a UI-test runner that
    already picked a simulator needs. It does not confine interception to that device: the PAC is
    installed on a network service and scoped by hostname, so every simulator on this Mac follows
    it for the profile's hosts.
    """
    # Before `_require_profile`, and so before anything is started: "which of these two did you
    # mean" is not a question to ask after the network has been rewired.
    if no_relaunch and bundle_id:
        raise click.UsageError("--relaunch and --no-relaunch contradict each other: pass one.")
    if use_name is not None and not use_name.strip():
        raise click.UsageError("--use needs a scenario name.")

    _require_profile()
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)

    with open(config.lock_file(), "w") as lock:
        _acquire_lock(lock)
        _up_locked(bundle_id, no_relaunch, use_name, simulator_selector)


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
                    f"{ui.RED}another `lyrebird up` or a watchdog restore is still in progress on port "
                    f"{config.CONTROL_PORT} after {timeout:g}s{ui.R}"
                ) from None
            if not waiting:
                click.echo(f"{ui.DIM}waiting for another `lyrebird up` or a watchdog restore to finish…{ui.R}")
                waiting = True
            time.sleep(0.5)


def _activate_scenario(name: str) -> None:
    """Make `name` the active scenario, and report what it displaced.

    Shared by `use` and `up --use` so the two cannot drift into describing the same switch
    differently. Activation is also what rewinds the scenario's sequences, so the scenario starts
    from its first step rather than resuming where the last run left it — which is why `--use` on
    the scenario that is already active is not a no-op.
    """
    result = api._control("/__mock__/scenarios/active", "PUT", {"name": name})
    previous = result.get("previous")
    if previous and previous["name"] != result["active"]:
        click.echo(
            f"switched: {previous['name']} ({previous['overrideCount']} override(s)) → "
            f"{ui.BOLD}{result['active']}{ui.R}"
        )
    else:
        click.echo(f"active: {result['active']}")


def _load_problems_for(health: dict | None, name: str) -> list[str] | None:
    """What stopped scenario `name` loading whole, or None if nobody could say.

    None is not an empty list. A proxy older than this CLI does not report the field at all, and
    reading that silence as "there were none" would let `up --use` relaunch the app against a
    scenario it never checked, while printing what it prints when the scenario is whole.

    The answer comes from `scenariosNotWhole`, which the proxy keys by scenario, rather than from
    the `loadProblems` strings beside it. Those cannot be matched back: a file named
    `orders-outage.json: backup.json` produces `"skipped orders-outage.json: backup.json: …"`,
    which begins exactly like a problem with `orders-outage` — and refusing to launch a scenario
    because a *differently named file* is broken is the same wrong answer in the other direction.
    """
    reported = (health or {}).get("scenariosNotWhole")
    if not isinstance(reported, dict):
        return None
    return [str(problem) for problem in reported.get(name) or []]


def _select_before_relaunch(name: str, health: dict | None) -> str | None:
    """Select `name`, or return the failure that means the app must not be relaunched.

    Three refusals, each of which would otherwise put the app in front of a scenario that is not
    the one the caller named:

    * the running proxy cannot report what loaded — it predates the field. Unchecked is not the
      same as whole, and this is the one refusal a restart fixes outright.
    * the scenario is there but did not load whole — overrides dropped for failing validation, or a
      malformed `default.json` replaced by an empty in-memory `default`. Neither is visible in the
      scenario list and both activate happily, so the PUT is not what can tell you.
    * the PUT was refused — no such scenario, or the proxy stopped answering. `_control` has
      already printed the API's own sentence and turned it into a `SystemExit`.

    Health is the reading `up` already has rather than a fresh one: what a scenario failed to load
    is settled while the store is built and nothing afterwards adds to it, so asking again would
    only widen the window in which the rest of the reading moves under us.
    """
    problems = _load_problems_for(health, name)
    if problems is None:
        click.echo(
            f"{ui.RED}✗ the running proxy is older than this CLI and cannot say whether "
            f"'{name}' loaded whole — the app was NOT relaunched against a scenario "
            f"nothing could check.{ui.R}\n"
            f"   restart the proxy: `lyrebird down && lyrebird up --use {name}`.\n"
            f"   the proxy is running — stop it with `lyrebird down`."
        )
        return f"the running proxy did not report whether '{name}' loaded whole"
    if problems:
        detail = "; ".join(problems)
        click.echo(
            f"{ui.RED}✗ scenario '{name}' did not load whole: {detail}{ui.R}\n"
            f"   the app was NOT relaunched — fix the file, then "
            f"`lyrebird down && lyrebird up --use {name}`.\n"
            f"   the proxy is running — stop it with `lyrebird down`."
        )
        return f"scenario '{name}' did not load whole: {detail}"

    try:
        _activate_scenario(name)
    except SystemExit:
        # `_control` has already printed why on its way out. What it could not know is what the
        # failure costs here: the relaunch is off, because launching now would put the app in
        # front of some scenario other than the one the caller named.
        known = [str(scenario) for scenario in (health or {}).get("scenarios") or []]
        listing = f"\n   scenarios in this profile: {', '.join(known)}" if known else ""
        click.echo(
            f"{ui.RED}   could not select '{name}' — the app was NOT relaunched.{ui.R}{listing}\n"
            f"   the proxy is running — stop it with `lyrebird down`."
        )
        return f"could not select scenario '{name}'"
    return None


def _up_locked(
    bundle_id: str | None, no_relaunch: bool = False, use_name: str | None = None, simulator_selector: str | None = None
) -> None:
    runtime = config.read_runtime()
    existing = api._health()
    # Kept rather than fetched again later: this is the reading `--use` checks its scenario against.
    health = existing

    if existing:
        api._require_same_profile(existing)
        click.echo(f"{ui.YELLOW}proxy already running{ui.R} (scenario '{existing['activeScenario']}')")
        proxy_pid = existing.get("pid", 0)
    else:
        _start_fresh_log()
        # Popen dups the fd for the child, so closing our copy immediately is correct.
        with open(config.LOG_FILE, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [
                    str(MITMDUMP),
                    "--listen-host",
                    config.PROXY_LISTEN_HOST,
                    "--listen-port",
                    str(config.PROXY_PORT),
                    "--set",
                    f"confdir={config.mitmproxy_confdir()}",
                    "-s",
                    str(config.ROOT / "addon.py"),
                ],
                cwd=str(config.ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=_child_env(),
            )
        deadline = time.time() + 12
        while True:
            health = api._health()
            if health is not None:
                break
            if not _pid_alive(proc.pid):
                click.echo(f"{ui.RED}proxy exited on startup — last log lines:{ui.R}\n{ui._tail_log(20)}")
                raise SystemExit(1)
            if time.time() >= deadline:
                # Don't leave an orphan that becomes healthy after we have given up on it.
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                click.echo(
                    f"{ui.RED}proxy did not become healthy in time — last log lines:{ui.R}\n"
                    f"{ui._tail_log(20)}\n   full log: {config.LOG_FILE}"
                )
                raise SystemExit(1)
            time.sleep(0.3)
        proxy_pid = proc.pid

    failures: list[str] = []

    # One resolution for both device operations below, so the CA cannot be trusted on one
    # simulator while the app is relaunched on another — which is what two independent `booted`
    # lookups allow the moment a second device boots between them.
    try:
        simulator: sim.Simulator | None = sim.resolve_simulator(simulator_selector)
    except sim.SimulatorError as error:
        simulator = None
        click.echo(f"{ui.RED}✗ simulator: {error}{ui.R}\n   the CA was NOT trusted and nothing was relaunched.")
        failures.append(f"no simulator to work on: {str(error).splitlines()[0]}")

    if simulator is not None:
        ca_ok, message = sim.trust_ca_in_sim(simulator)
        click.echo(f"{'✓' if ca_ok else '✗'} CA: {message}")
        if not ca_ok:
            failures.append(f"CA not trusted in the simulator: {message}")

    # The recorded service is a fallback, not merely a default. After a crash and a restore that
    # failed, the record names the service whose PAC is still ours; losing the route meanwhile
    # (Wi-Fi off) must not turn that into "no service", which strands the record with nothing to
    # restore it on and lets `down` delete it.
    discovered, discovery_error = _discover_service()
    service = discovered or runtime.get("service")
    recorded = runtime.get("previousPac")
    recorded_service = runtime.get("service")
    if recorded and recorded_service and recorded_service != service:
        # The route moved — Wi-Fi to Ethernet — since the record was written. The record belongs
        # to the old service, and it is about to be replaced by one for the new: put the old
        # service back *first*, or nothing will remember that its PAC still points at us. A
        # record that is not ours any more (settings changed by hand) is simply dropped.
        #
        # The old service's watchdog goes first of all. It is told its service on its command
        # line; left alive it would answer the proxy's next death by reading the new record,
        # finding the old service's PAC "not ours", and deleting it. Its live loop is no safer:
        # it re-enables our PAC wherever it finds it disabled — which is exactly what restoring
        # "no previous PAC" leaves behind. The lock this `up` holds keeps that loop out until the
        # signal lands, and the loop checks whose record it is once it gets in.
        _terminate(runtime.get("watchdogPid"), "_watchdog")
        runtime = {**runtime, "watchdogPid": None}
        try:
            _restore_previous_pac(recorded_service, runtime)
        except netproxy.NetworkSetupError as error:
            click.echo(
                f"{ui.RED}✗ could not restore the previous PAC on '{recorded_service}' before "
                f"switching to '{service}': {error}{ui.R}\n"
                f"   the record is kept; run `lyrebird down` once '{recorded_service}' can "
                f"be reached."
            )
            raise SystemExit(1) from None
        recorded = None
    state: dict = {"proxyPid": proxy_pid, "service": service}
    if simulator is not None:
        # Recorded so `status` can say which device this run trusted and relaunched. Absent when
        # nothing was resolved, rather than a stale entry from an earlier run: the file is
        # rewritten whole on every `up`.
        state["simulator"] = {"udid": simulator.udid, "name": simulator.name}
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
            click.echo(
                f"{ui.RED}✗ could not install the PAC on '{service}': {error}{ui.R}\n"
                f"   the proxy is running — stop it with `lyrebird down`."
            )
            raise SystemExit(1) from None

        # A watchdog watches one service, given on its command line. One left over from a run on
        # another service would restore that service's record and then delete this one's.
        watchdog_pid = runtime.get("watchdogPid")
        if not (_pid_is_ours(watchdog_pid, "_watchdog") and recorded_service == service):
            _terminate(watchdog_pid, "_watchdog")
            watchdog_pid = _spawn_watchdog(service)
        state["watchdogPid"] = watchdog_pid
        click.echo(f"✓ PAC installed on '{service}' (configured hosts → proxy, everything else DIRECT)")
    else:
        # The discovery error is appended rather than replacing the message: "no active network
        # service" is what it means for the run, and the `route`/`networksetup` failure is why —
        # an operator told only the first goes looking at Wi-Fi, and one told only the second
        # does not learn that nothing is being intercepted.
        why = f" ({discovery_error})" if discovery_error else ""
        click.echo(
            f"{ui.RED}✗ could not detect the active network service{why} — set the PAC "
            f"manually:{ui.R}\n   {netproxy.pac_url()}"
        )
        failures.append(f"no active network service{why}: traffic is NOT being intercepted")

    config.write_runtime(state)

    # Before the relaunch, and that ordering is the whole point of `--use`: an app makes its first
    # requests *while it launches*, so a scenario selected afterwards is one the launch never saw —
    # and an app that caches its launch response goes on showing the old scenario however green a
    # later `use` looks. It runs whether or not there is a device to launch on: a caller who
    # starts the app themselves still asked for that scenario.
    refused = _select_before_relaunch(use_name, health) if use_name else None
    if refused:
        # No relaunch, and no banner asking for one by hand either: both would put the app in front
        # of the scenario the caller was trying to replace. The final look below still runs — the
        # proxy is up and the PAC is installed, and an operator not told that walks away believing
        # the network was left alone.
        failures.append(refused)
    elif no_relaunch:
        click.echo("↷ relaunch skipped (--no-relaunch): the caller launches the app")
    elif simulator is not None and (target := (bundle_id or config.PROFILE.sim_bundle_id)):
        launched, detail = sim._relaunch(target, simulator)
        click.echo(f"{'✓' if launched else '✗'} relaunch {target}: {detail}")
        if not launched:
            failures.append(f"could not relaunch {target}: {detail}")
    elif simulator is not None:
        click.echo(
            f"{ui.BOLD}{ui.YELLOW}⚠ RELAUNCH THE APP NOW{ui.R} — URLSession caches the proxy config, so an "
            f"already-running app won't use the PAC until it's relaunched.\n"
            f"   lyrebird relaunch <bundleid>   # on {simulator}, the device this run used\n"
            f"   (or set simBundleId in profile.json and it happens here)"
        )
    # And no `else`: with no simulator resolved there is nothing to launch on, and no device to
    # tell the operator to launch on by hand either. That failure was printed and counted where
    # the device could not be resolved; saying it again here would turn one problem into two.

    # The last look decides. Everything above reported its own step; this is the postcondition
    # itself — proxy answering, PAC routing to it — observed once, at the end, and a step that
    # succeeded a moment ago is no defence if the observation says otherwise now.
    final = api._health()
    if final is None:
        click.echo(f"{ui.RED}✗ the proxy stopped answering on port {config.CONTROL_PORT} during startup{ui.R}")
        failures.append("the proxy stopped answering during startup")
    try:
        intercepting = netproxy.intercepting(service)
    except netproxy.NetworkSetupError as error:
        # Said here, in place of the banner: the banner's "PAC is disabled/not ours" would be a
        # diagnosis this command never made, and the summary below only names failures that
        # were printed as they happened.
        click.echo(f"{ui.RED}✗ could not read the PAC on '{service}' after installing it: {error}{ui.R}")
        failures.append(f"could not read the PAC on '{service}': {error}")
    else:
        ui._banner(final, service, intercepting)
        if service and not intercepting:
            failures.append(f"PAC on '{service}' is not routing to the proxy — disabled, or not ours")

    # Exit non-zero unless the whole point of `up` was achieved. Reporting a warning and returning 0
    # meant a script — or an agent — could believe it was mocking when nothing was intercepted.
    if failures:
        # Each failure was already printed inline as it happened, so a single one needs no
        # summary — only collect them when there is more than one to collect.
        if len(failures) > 1:
            click.echo(f"\n{ui.RED}✗ up did not finish cleanly:{ui.R}")
            for failure in failures:
                click.echo(f"   · {failure.splitlines()[0]}")
        else:
            click.echo(f"{ui.RED}✗ up did not finish cleanly.{ui.R}")
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


@click.command()
def down() -> None:
    """Stop the proxy and restore the previous proxy configuration."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with open(config.lock_file(), "w") as lock:
        # The same lock `up` and the watchdog take, held from before the runtime record is read
        # until the cleanup is done. A watchdog repair in flight finishes before its parent is
        # signalled — SIGTERM reaches the watchdog, not the `networksetup` it already started,
        # which would otherwise switch the PAC back on after the restore had read back clean.
        # And no repair can start afterwards: the record it checks is gone.
        _acquire_lock(lock)
        _down_locked()


def _down_locked() -> None:
    runtime = config.read_runtime()
    health = api._health()

    # The runtime file can be missing or unreadable — deleted by hand, or written by a version
    # that crashed mid-write. Without this, `down` would find nothing to do and cheerfully report
    # "stopped" while the proxy was still running and the PAC still pointing at it. Rediscover
    # what we can: health knows the pid, and the OS knows which service carries the default route.
    if health and not runtime.get("proxyPid"):
        runtime = {**runtime, "proxyPid": health.get("pid")}
    discovery_error = None
    if not runtime.get("service"):
        discovered, discovery_error = _discover_service()
        if health or discovered:
            runtime = {**runtime, "service": discovered}

    if not health and not runtime:
        if discovery_error:
            # No proxy and no record, but the one read that could have found a PAC of ours still
            # installed never answered. "Nothing to stop" is true of the proxy and unproven of the
            # network, and exit 0 would claim both.
            click.echo(
                f"{ui.RED}✗ nothing to stop, but the proxy settings could not be checked: the "
                f"active network service could not be detected ({discovery_error}){ui.R}\n"
                f"   check System Settings ▸ Network ▸ <service> ▸ Proxies by hand."
            )
            raise SystemExit(1)
        click.echo(f"{ui.DIM}nothing to stop — no proxy running and no runtime state{ui.R}")
        return

    # Kill the watchdog FIRST so it can't reinstall the PAC mid-teardown.
    _terminate(runtime.get("watchdogPid"), "_watchdog")

    service = runtime.get("service")
    unrestorable = False
    if not service and discovery_error:
        # No service to act on and no record of one, so there is nothing the PAC could be restored
        # *on*. The proxy and the watchdog are still stopped below — leaving them alive would be a
        # second failure on top of this one — but `down` must not then print "stopped" and exit 0,
        # because the network is exactly as this command found it.
        unrestorable = True
        click.echo(
            f"{ui.RED}✗ could not restore the proxy settings: the active network service "
            f"could not be detected ({discovery_error}) and none is recorded{ui.R}\n"
            f"   the proxy is being stopped anyway; check System Settings ▸ Network ▸ "
            f"<service> ▸ Proxies by hand."
        )
    if service:
        try:
            previous = _restore_previous_pac(service, runtime)
        except netproxy.NetworkSetupError as error:
            click.echo(
                f"{ui.RED}✗ could not restore proxy settings on '{service}': {error}{ui.R}\n"
                f"   fix manually: System Settings ▸ Network ▸ {service} ▸ Proxies{ui.R}"
            )
            raise SystemExit(1) from error
        if previous is None:
            click.echo(f"{ui.DIM}PAC on '{service}' is not ours — left untouched{ui.R}")
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
    while time.time() < deadline and api._health() is not None:
        time.sleep(0.2)
    if api._health() is None:
        click.echo(f"{ui.GREEN}stopped{ui.R}")
    else:
        click.echo(f"{ui.YELLOW}⚠ proxy still responding on port {config.CONTROL_PORT} after SIGTERM{ui.R}")
        raise SystemExit(1)
    if unrestorable:
        raise SystemExit(1)  # the proxy is down; the network was never put back


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable state on stdout.")
def status(as_json: bool) -> None:
    """Show intercept state (honest about whether the PAC is actually enabled).

    The exit code reports the state, not the formatting: 0 only when the proxy is up *and*
    intercepting *this* profile, whichever way you asked. `--json` changes what is printed and
    never what it means — a flag that decides how output is rendered must not also decide what
    success is, or `lyrebird status && …` silently proceeds against a proxy that is mocking
    nothing.
    """
    health = api._health()
    # Compared by hand, not `_require_same_profile` (see it for why): `status` reports the foreign
    # proxy before exiting 1, so `--profile B status && …` cannot proceed against one mocking A.
    running = (health or {}).get("profileFingerprint")
    foreign = bool(running) and running != config.PROFILE_FINGERPRINT
    runtime = config.read_runtime()
    # Discovery can fail the same way the PAC read below can, and it is reported the same way: as
    # `pacError` with `service` null, not as a traceback that leaves `--json` printing nothing.
    service, pac_error = runtime.get("service"), None
    if not service:
        service, pac_error = _discover_service()
    # What the last `up` acted on, not a fresh lookup: the question `status` answers is which
    # device this run trusted and relaunched, and re-resolving would report whatever is booted
    # now — a different device, with the old one's CA, reading as if it were the one in use.
    recorded_simulator = runtime.get("simulator")
    simulator = recorded_simulator if isinstance(recorded_simulator, dict) else None
    # One observation of the PAC feeds the output and the exit code alike. Reading it once for
    # the banner, again for the JSON field and a third time for the exit status let a PAC that
    # flips in between produce output that contradicts the exit code — a `status` whose text says
    # one thing and whose `$?` says another is worse than either being wrong.
    try:
        pac = netproxy.pac_status(service) if service else None
    except netproxy.NetworkSetupError as error:
        # Unproven, which is different from seen to be off — the exit code is the same, the
        # explanation is not, and a person reading "DISABLED" would go and switch it on.
        pac, pac_error = None, str(error)
    # A PAC pointing at this port is "ours" whoever started the proxy behind it, so a foreign
    # profile fails the same way a disabled PAC does: nothing here is intercepting this profile.
    intercepting = pac is not None and pac.enabled and pac.ours and not foreign

    if as_json:
        # Every field below that describes a profile's state is whatever the proxy that answered
        # said; when that proxy is running another profile, none of it is this profile's, so it
        # is reported as unknown rather than handed over under this profile's name. `proxyUp`
        # stays true — the port really is held — and `profileMismatch` says by whom.
        mine = {} if foreign else (health or {})
        click.echo(
            json.dumps(
                {
                    "proxyUp": health is not None,
                    "intercepting": intercepting,
                    "profileMismatch": foreign,
                    "profileFingerprint": config.PROFILE_FINGERPRINT,
                    "runningProfileFingerprint": running,
                    "pacError": pac_error,
                    "activeScenario": mine.get("activeScenario"),
                    "overrideCount": mine.get("overrideCount"),
                    "scenarios": None if foreign else (health or {}).get("scenarios", []),
                    # `null` when the running engine did not supply the field, never `[]`. A current
                    # engine always sends both, with one entry per rule — so `[]` is a real state ("no
                    # rules here") and a missing key is a capability signal ("this proxy cannot tell
                    # you"). Defaulting to `[]` collapsed those into the claim that nothing has answered,
                    # which is the shape this file exists to avoid. The exit code is computed separately
                    # and still does not depend on either field existing.
                    "sequences": mine.get("sequences"),
                    "answers": mine.get("answers"),
                    "simBundleId": mine.get("simBundleId"),
                    "profile": str(config.PROFILE_DIR),
                    "service": service,
                    # `{"udid": …, "name": …}`, or null when no `up` has recorded one — which is a real
                    # state ("nothing here trusted a CA") and not the same as "the default device".
                    "simulator": simulator,
                    "pac": {"url": pac.url, "enabled": pac.enabled, "ours": pac.ours} if pac else None,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"{ui.DIM}profile: {config.PROFILE_DIR}{ui.R}")
        if foreign:
            # Not the banner: "PAC is disabled/not ours" would send the operator to `lyrebird up`,
            # which refuses this exact situation. The remedy is to stop that proxy or aim
            # elsewhere, so say which proxy answered and print both fingerprints to identify it.
            click.echo(
                f"{ui.BOLD}{ui.YELLOW}🟠 PROXY UP, ANOTHER PROFILE{ui.R} — nothing is intercepting for this profile."
            )
            click.echo(api._profile_mismatch(str(running)))
        elif pac_error and health is not None:
            # Not the banner: its "PAC is disabled/not ours" is a diagnosis this read never made.
            # `service` is None when discovery itself is what failed, and "on 'None'" would name a
            # network service that does not exist.
            where = f" on '{service}'" if service else ""
            click.echo(
                f"{ui.BOLD}{ui.YELLOW}🟠 PROXY UP, PAC UNREADABLE{ui.R} — could not read the PAC{where}: {pac_error}"
            )
        else:
            ui._banner(health, service, intercepting)
        if health and not foreign:
            click.echo(f"  scenarios: {', '.join(health['scenarios'])}")
            for state in health.get("sequences", []):
                position = (
                    f"next step {state['nextStep']}/{state['stepCount']}"
                    if state["nextStep"]
                    else f"{ui.RED}exhausted{ui.R}"
                )
                overrun = f" {ui.YELLOW}· overrun{ui.R}" if state["hasOverrun"] else ""
                trigger = "own calls" if state["advanceOn"] == "self" else "advanceOn"
                click.echo(f"  sequence {state['id']}: {position} · {trigger}{overrun}")
        if pac is not None:
            state = "enabled" if pac.enabled else f"{ui.RED}DISABLED{ui.R}"
            owner = "" if pac.ours or not pac.url else " · not ours"
            click.echo(f"  PAC on '{service}': {pac.url or '(none)'} · {state}{owner}")
        if simulator:
            click.echo(
                f"  simulator: {simulator.get('name')} ({simulator.get('udid')}) "
                f"{ui.DIM}· CA + relaunch only; the PAC is not scoped to it{ui.R}"
            )

    raise SystemExit(0 if health is not None and intercepting else 1)


@click.command()
def logs() -> None:
    """Print the last 60 lines of the proxy log (not a follow — use `tail -f` on the path shown)."""
    click.echo(ui._tail_log(60))
    click.echo(f"{ui.DIM}{config.LOG_FILE}{ui.R}", err=True)


@click.command(name="_watchdog", hidden=True)
@click.argument("service")
def watchdog(service: str) -> None:
    while True:
        if api._health() is None:
            if _restore_after_death(service):
                return
            continue  # a replacement proxy is live: go back to watching it
        _repair_pac(service)
        time.sleep(2)


def _repair_pac(service: str) -> None:
    """The watchdog's live loop: macOS silently disables our PAC while the proxy is alive, so
    switch it back on.

    Under the lock `up` holds, and only while the runtime record still names this service. An
    `up` that moves the route to another service restores this one — and restoring "no previous
    PAC" leaves our URL installed, disabled — exactly what this loop exists to undo. Unlocked, it
    undid it in the gap before its own SIGTERM landed, and the next `down` restored the new
    service only.
    """
    with open(config.lock_file(), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if config.read_runtime().get("service") != service:
            return  # migrated away from, or `down` has been: not ours to touch any more
        try:
            pac = netproxy.pac_status(service)
        except netproxy.NetworkSetupError:
            return  # unknown is not "off": neither reinstall nor give up, just ask again
        if not (pac.enabled and pac.ours) and pac.url in ("", netproxy.pac_url()):
            with contextlib.suppress(netproxy.NetworkSetupError):
                netproxy.set_pac(service)


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
        if api._health() is not None:
            return False
        runtime = config.read_runtime()
        if runtime.get("service") not in (None, service):
            # The record is another service's — an `up` moved the route and then failed before
            # it could replace this watchdog. Restoring *our* service from it would read the PAC
            # there as "not ours" and delete a record that still matters. Leave it for `down`.
            return True
        for _ in range(_WATCHDOG_RESTORE_ATTEMPTS):
            try:
                _restore_previous_pac(service, runtime)
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


def _start_fresh_log() -> None:
    """Begin each run on a new inode rather than truncating the old one.

    The log names every host and path that came through, so it is 0600 — but a log left by an
    older version may be 0644, and `chmod` cannot revoke a descriptor somebody already holds.
    Truncating in place keeps that inode, so a reader who opened it while it was readable goes on
    seeing new traffic. Renaming a fresh private file over it leaves them holding the old one.
    """
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(config.LOG_FILE, "")
