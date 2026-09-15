"""Starting and stopping the proxy: the session journal, the PAC, and what `up` must achieve.

The journal is the authority: one record per user, under one lock, saying who holds the PAC and
what was there before. There is one session per user, so a second `up` is refused while a journal
exists, and `down` finds that record from any profile, port or directory — it is the only recovery
command there is. Nothing restores the settings automatically: a proxy that dies leaves the Mac
routed at a dead port until someone runs `lyrebird down`.

`up` exits 0 only when one final observation says so: this proxy answering on the port, this PAC
routing to it, on the service the default route still carries. Anything less unwinds — the network
goes back and the journal goes away — because a half-acquired session nobody was told about is the
failure this file exists to prevent.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import click

import api
import config
import netproxy
import ownership
import procs
import session
import simulator as sim
import ui
from ownership import Absent, Configured, Off, Owner, Pac, PacClass, ServiceRef, SessionRecord, Unreadable

MITMDUMP = config.ROOT / ".venv" / "bin" / "mitmdump"
_STARTUP_DEADLINE_SECONDS = 12.0

_DOWN = "run `lyrebird down`"


# MARK: - the session and the owner


def _session() -> session.Session:
    """Every construction in this module goes through here, so the test suite's one patch of
    `session.default_root` reaches all of them."""
    return session.Session()


def _owner() -> Owner:
    """Who this invocation would be. All three fields: a port, a profile and a state directory each
    repeat on their own."""
    return Owner(
        control_port=config.CONTROL_PORT,
        profile_fingerprint=config.PROFILE_FINGERPRINT,
        state_root=str(config.STATE_ROOT),
    )


def _red(message: str) -> None:
    click.echo(f"{ui.RED}✗ {message}{ui.R}")


def _pac_text(pac: Pac) -> str:
    return f"{pac.url or '(none)'} · {'enabled' if pac.enabled else 'disabled'}"


# MARK: - The two recipes
#
# Each resolves nothing itself: the caller resolves the service once per invocation and passes the
# name. Each reads the PAC before it writes and again afterwards, and raises `NetworkSetupError`
# for anything it cannot account for — `networksetup` exits 0 without applying its change, and a
# recipe that trusted the exit code would report a PAC it never installed.


def _install(name: str, port: int, expected: Pac) -> None:
    """Put our PAC on, over exactly the reading `up` decided on.

    The pre-write read must equal `expected` verbatim — disabled Lyrebird residue included, so
    `up → down → up` installs over what `down` leaves — or nothing is written at all
    (test_up_refuses_to_install_over_a_pac_that_changed_since_it_looked).
    """
    ours = ownership.our_url(port)
    found = netproxy.pac_status(name)
    if found != expected:
        raise netproxy.NetworkSetupError(
            f"the PAC changed while this `up` was deciding: expected {_pac_text(expected)}, found {_pac_text(found)}"
        )
    netproxy.write_pac_url(name, ours)
    back = netproxy.pac_status(name)
    if back == Pac(ours, True):
        return
    if back == Pac(ours, False):
        # `-setautoproxyurl` usually switches the PAC on as a side effect; when it did not, the flag
        # is the one command left.
        netproxy.write_pac_state(name, True)
        back = netproxy.pac_status(name)
        if back == Pac(ours, True):
            return
    raise netproxy.NetworkSetupError(f"the PAC on '{name}' did not become ours (now: {_pac_text(back)})")


def _restore(name: str, port: int, baseline: Pac) -> None:
    """Put the baseline back, in as few `networksetup` commands as the target needs.

    A target that is already satisfied costs zero writes and is still a success: `satisfies` is the
    one predicate for "the obligation is met", and it accepts the disabled residue an Off restore
    leaves (test_restore_issues_exactly_the_commands_the_target_needs).
    """
    target = ownership.restore_target(baseline)
    found = netproxy.pac_status(name)
    cls = ownership.classify(found, port, baseline)
    if ownership.satisfies(cls, target):
        return
    if cls is PacClass.RESUMABLE:
        # The URL is already the baseline's and only the flag is wrong — one command.
        netproxy.write_pac_state(name, isinstance(target, Configured) and target.enabled)
    elif cls is PacClass.OURS_ENABLED or cls is PacClass.OURS_DISABLED:
        if isinstance(target, Off):
            # macOS rejects an empty URL, so the only move for "there was no PAC" is the flag.
            netproxy.write_pac_state(name, False)
        else:
            netproxy.write_pac_url(name, target.url)
            if netproxy.pac_status(name).enabled != target.enabled:
                netproxy.write_pac_state(name, target.enabled)
    else:
        raise netproxy.NetworkSetupError(
            f"the PAC on '{name}' is not one this session may restore over (now: {_pac_text(found)})"
        )
    final = netproxy.pac_status(name)
    if not ownership.satisfies(ownership.classify(final, port, baseline), target):
        # `networksetup` exits 0 without applying its change; read back, that is an open obligation
        # kept, never one abandoned — see test_down_keeps_the_journal_when_a_setter_changes_nothing.
        raise netproxy.NetworkSetupError(f"the PAC on '{name}' did not restore (now: {_pac_text(final)})")


# MARK: - `init`


@click.command()
@click.argument("path", type=click.Path(), required=False)
def init(path: str | None) -> None:
    """Create a profile from the bundled examples."""
    target = Path(path).expanduser().resolve() if path else config.PROFILE_DIR
    if (target / "profile.json").exists():
        raise SystemExit(f"{ui.RED}{target}/profile.json already exists — refusing to overwrite{ui.R}")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(config.EXAMPLES_DIR, target, dirs_exist_ok=True)
    click.echo(f"✓ profile created at {ui.BOLD}{target}{ui.R}")
    click.echo(
        f"  Edit {target}/profile.json: set `hosts` to the API your app calls, and\n"
        f"  `simBundleId` to your app's bundle identifier. The examples are a schema\n"
        f"  template, not a runnable demo — api.example.com serves none of these paths."
    )
    click.echo(f"  Then:  lyrebird --profile {target} up")


# MARK: - `up`


class _Unwind(Exception):
    """A fresh acquisition met a failure: everything it did comes back off under the same lock."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _require_profile() -> None:
    """Read the profile, for the one command that needs its contents.

    `up` is where a malformed profile aborts. Every other command — `down` above all — works from
    the session journal and the live API, so a broken profile.json cannot stop you restoring the
    network.
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

    There is one session per user, so this refuses while one exists — even this profile's. On a
    session you started, to put a different scenario in front of the app: `lyrebird use NAME &&
    lyrebird relaunch`; on another agent's, wait for its `down`.
    """
    # Before `_require_profile`, and so before anything is started: "which of these two did you
    # mean" is not a question to ask after the network has been rewired.
    if no_relaunch and bundle_id:
        raise click.UsageError("--relaunch and --no-relaunch contradict each other: pass one.")
    if use_name is not None and not use_name.strip():
        raise click.UsageError("--use needs a scenario name.")

    _require_profile()
    sess = _session()
    sess.ensure_root()
    try:
        with sess.locked():
            _up_locked(sess, bundle_id, no_relaunch, use_name, simulator_selector)
    except session.LockBusy as error:
        raise SystemExit(f"{ui.RED}{error}{ui.R}") from None


def _refuse_over_a_journal(journal: ownership.Journal) -> None:
    """One session per user. A journal that exists — this owner's or anybody's — is refused, and a
    journal that cannot be read is refused hardest: it may still be holding somebody's PAC, and
    `down` is what reads it next (test_up_refuses_over_an_unreadable_journal)."""
    if isinstance(journal, Unreadable):
        raise SystemExit(
            f"{ui.RED}✗ session.json cannot be read ({journal.reason}) — if the session is yours, move it away and "
            f"{_DOWN}; if another agent's, ask them{ui.R}"
        )
    if isinstance(journal, SessionRecord):
        # Addressed to the reader, not decided by the fingerprint: a shared profile is one fingerprint
        # and two agents — see test_up_refuses_over_another_owners_journal.
        raise SystemExit(
            f"{ui.RED}✗ a session is up (port {journal.owner.control_port}, profile "
            f"{journal.owner.profile_fingerprint}). If you started it: `lyrebird down` first, or to switch "
            f"scenario `lyrebird use X && lyrebird relaunch`. If another agent did: do not run `down` — "
            f"wait for its `down`, then `up` again{ui.R}"
        )


def _acquire_baseline() -> tuple[ServiceRef, Pac, Pac]:
    """The service this `up` would take, the PAC it found there, and the baseline it would record.

    An *enabled* Lyrebird PAC with no journal is somebody's unowned session, and installing over it
    would strand it with nothing left describing what to put back — see
    test_up_refuses_an_enabled_unowned_lyrebird_pac.
    """
    try:
        service = netproxy.active_service()
    except netproxy.NetworkSetupError as error:
        raise SystemExit(f"{ui.RED}✗ {error}{ui.R}") from None
    if service is None:
        raise SystemExit(f"{ui.RED}✗ could not detect the active network service{ui.R}")
    try:
        observed = netproxy.pac_status(service.name)
    except netproxy.NetworkSetupError as error:
        raise SystemExit(f"{ui.RED}✗ {error}{ui.R}") from None
    cls = ownership.classify_unowned(observed)
    if cls is ownership.UnownedClass.LYREBIRD_ENABLED:
        raise SystemExit(
            f"{ui.RED}✗ an enabled Lyrebird PAC on '{service.name}' that no session owns — "
            f"`networksetup -setautoproxystate '{service.name}' off`, then `lyrebird up`{ui.R}"
        )
    # EMPTY and disabled Lyrebird residue both mean "there was nothing here": one canonical
    # baseline, so the Off target cannot be read back as RESUMABLE forever.
    empty = cls in (ownership.UnownedClass.EMPTY, ownership.UnownedClass.LYREBIRD_DISABLED)
    return service, observed, Pac("", False) if empty else observed


def _up_locked(
    sess: session.Session,
    bundle_id: str | None,
    no_relaunch: bool,
    use_name: str | None,
    simulator_selector: str | None,
) -> None:
    owner = _owner()
    port = owner.control_port
    # Nothing else is observed first: `sim._run` has no timeout, and an `up` refused on the journal
    # alone must not hold the session lock for the length of a hanging `simctl` — see
    # test_up_refuses_on_the_journal_without_touching_simctl.
    _refuse_over_a_journal(sess.read())
    service, observed, baseline = _acquire_baseline()

    failures: list[str] = []
    try:
        simulator: sim.Simulator | None = sim.resolve_simulator(simulator_selector)
    except sim.SimulatorError as error:
        simulator = None
        _red(f"simulator: {error}")
        click.echo("   the CA was NOT trusted and nothing was relaunched.")
        failures.append(f"no simulator to work on: {str(error).splitlines()[0]}")

    proc, ref = _start_proxy()
    record = SessionRecord(
        version=ownership.SESSION_VERSION,
        owner=owner,
        service=service,
        baseline=baseline,
        proxy=ref,
        simulator=None,
    )
    try:
        sess.write(record)
    except OSError as error:
        # Nothing is installed and the journal is not there to unwind from, so the child this run
        # started is stopped here — see test_up_stops_the_child_when_the_journal_cannot_be_written.
        _report_stop(ref, port)
        raise SystemExit(
            f"{ui.RED}✗ could not write the session journal ({error}); nothing was installed{ui.R}"
        ) from None

    # From here every failure unwinds: the network goes back and the journal goes away.
    try:
        health = _wait_for_startup(proc, ref, port)
        try:
            _install(service.name, port, observed)
        except netproxy.NetworkSetupError as error:
            raise _Unwind(f"could not install the PAC on '{service.name}': {error}") from None
        click.echo(f"✓ PAC installed on '{service.name}' (configured hosts → proxy, everything else DIRECT)")
        record = _trust_ca(sess, record, simulator, failures)
        _relaunch(simulator, bundle_id, no_relaunch, use_name, health, failures)
        _final_look(record, health, failures)
    except _Unwind as unwound:
        _red(unwound.reason)
        _teardown(sess, record)
        raise SystemExit(1) from None
    except BaseException:
        # Any failure unwinds — a Ctrl-C in the startup wait or a transport error nobody named
        # included. Without this the PAC stayed installed at a port whose proxy this run had just
        # abandoned — see test_up_unwinds_when_something_unexpected_raises_after_the_install.
        _red("up did not finish; putting the network back")
        _teardown(sess, record)
        raise

    if failures:
        # Any failure unwinds: an acquisition that could not achieve its postcondition puts the
        # network back rather than leaving a proxy the operator must remember to stop.
        _teardown(sess, record)
        raise SystemExit(1)


def _start_proxy() -> tuple[subprocess.Popen, ownership.Ref]:
    """Start the proxy and mint the `Ref` the journal will name. Nothing is installed yet, so both
    failures here exit outright rather than unwinding."""
    _start_fresh_log()
    try:
        with open(config.LOG_FILE, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                _proxy_argv(),
                cwd=str(config.ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=_child_env(),
            )
    except OSError as error:
        raise SystemExit(f"{ui.RED}✗ the proxy could not be started ({error}); nothing was installed{ui.R}") from None
    try:
        return proc, procs.ref_of(proc.pid)
    except procs.ProcessCheckError as error:
        click.echo(f"{ui.RED}proxy exited on startup — last log lines:{ui.R}\n{ui._tail_log(20)}")
        raise SystemExit(f"{ui.RED}✗ the proxy this run started could not be inspected ({error}){ui.R}") from None


def _wait_for_startup(proc: subprocess.Popen, ref: ownership.Ref, port: int) -> dict:
    """Wait for *this* proxy to answer on the port. A reading from another pid is the startup port
    race, and adopting it would install a PAC for a proxy this run did not start."""
    deadline = time.time() + _STARTUP_DEADLINE_SECONDS
    while True:
        health = api._health(port)
        if health is not None:
            if health.get("pid") == ref.pid:
                return health
            raise _Unwind(
                f"another proxy (pid {health.get('pid')}) answered on port {port} while this one was starting"
            )
        if proc.poll() is not None:
            click.echo(f"{ui.RED}proxy exited on startup — last log lines:{ui.R}\n{ui._tail_log(20)}")
            raise _Unwind("the proxy exited on startup")
        if time.time() >= deadline:
            click.echo(
                f"{ui.RED}proxy did not become healthy in time — last log lines:{ui.R}\n"
                f"{ui._tail_log(20)}\n   full log: {config.LOG_FILE}"
            )
            raise _Unwind("the proxy did not become healthy in time")
        time.sleep(0.3)


def _trust_ca(
    sess: session.Session, record: SessionRecord, simulator: sim.Simulator | None, failures: list[str]
) -> SessionRecord:
    if simulator is None:
        return record
    ca_ok, message = sim.trust_ca_in_sim(simulator)
    click.echo(f"{'✓' if ca_ok else '✗'} CA: {message}")
    if not ca_ok:
        failures.append(f"CA not trusted in the simulator: {message}")
        return record
    # Only now: the journal's `simulator` means "the device holding the CA", and a failed trust must
    # not leave the session bound to a device without one — see
    # test_up_records_the_simulator_only_after_trusting_it.
    trusted = dataclasses.replace(record, simulator=ownership.Simulator(simulator.udid, simulator.name))
    try:
        sess.write(trusted)
    except OSError as error:
        # `relaunch` and `status` read the device from here, so a write that failed leaves them
        # naming the wrong device while `up` reports success.
        raise _Unwind(f"the trusted simulator could not be recorded ({error})") from None
    return trusted


def _relaunch(
    simulator: sim.Simulator | None,
    bundle_id: str | None,
    no_relaunch: bool,
    use_name: str | None,
    health: dict,
    failures: list[str],
) -> None:
    refused = _select_before_relaunch(use_name, health) if use_name else None
    if refused:
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


def _final_look(record: SessionRecord, startup: dict, failures: list[str]) -> None:
    """The postcondition itself — this proxy answering, this PAC routing to it, on the service the
    route still carries — observed once, at the end. A step that succeeded a moment ago is no
    defence if the observation says otherwise now."""
    port = record.owner.control_port
    health = api._health(port)
    if health is None:
        _red(f"the proxy stopped answering on port {port} during startup")
        failures.append("the proxy stopped answering during startup")
    else:
        if health.get("pid") != record.proxy.pid:
            _red(f"the proxy answering on port {port} is now pid {health.get('pid')}, not {record.proxy.pid}")
            failures.append(f"the port changed hands during startup (pid {record.proxy.pid} → {health.get('pid')})")
        if health.get("journalError"):
            # The proxy reads the journal too, and it saying the journal is broken means the `down`
            # this `up` promises cannot restore from it.
            _red(f"the proxy reports the session journal unreadable: {health['journalError']}")
            failures.append("the session journal is unreadable")
        running = health.get("profileFingerprint")
        if running != record.owner.profile_fingerprint:
            _red(
                f"the proxy on port {port} runs profile {running or 'none reported'}, "
                f"not {record.owner.profile_fingerprint}"
            )
            failures.append("another profile answers on the control port")
    try:
        pac: Pac | None = netproxy.pac_status(record.service.name)
    except netproxy.NetworkSetupError as error:
        pac = None
        _red(f"the PAC on '{record.service.name}' could not be read: {error}")
    if pac != Pac(ownership.our_url(port), True):
        _red(f"the PAC on '{record.service.name}' is not routing to this proxy")
        failures.append(f"PAC on '{record.service.name}' is not routing to the proxy")
    try:
        route = netproxy.active_service()
    except netproxy.NetworkSetupError as error:
        route = None
        _red(f"the active network service could not be confirmed: {error}")
    if route is None:
        _red("the active network service could not be confirmed")
        failures.append("the active network service could not be confirmed")
    elif route.device != record.service.device:
        _red(f"the default route moved to '{route.name}' during startup")
        failures.append("the default route moved during startup")
    if not failures:
        ui._banner(startup, record.service.name, True)


# MARK: - `down`


@click.command()
def down() -> None:
    """Stop the proxy and restore the previous proxy settings.

    From anywhere: no profile, no port and no state directory are needed, because the session
    journal is the authority and there is one per user.
    """
    sess = _session()
    sess.ensure_root()
    try:
        with sess.locked():
            journal = sess.read()
            if isinstance(journal, Absent):
                click.echo(f"{ui.DIM}no session: nothing to stop{ui.R}")
                return
            if isinstance(journal, Unreadable):
                raise SystemExit(
                    f"{ui.RED}✗ session.json cannot be read ({journal.reason}) — nothing was changed; move it "
                    f"away, put the PAC back by hand (`networksetup -setautoproxystate <service> off`), then "
                    f"`lyrebird down`{ui.R}"
                )
            code = _teardown(sess, journal)
    except session.LockBusy as error:
        raise SystemExit(f"{ui.RED}{error}{ui.R}") from None
    if code:
        raise SystemExit(code)


def _teardown(sess: session.Session, journal: SessionRecord) -> int:
    """Put the recorded settings back, release the journal, stop the proxy — in that order.

    The order is the point. A journal kept after a successful restore let a later `down` read a
    baseline the user had since enabled by hand as RESUMABLE and disable it again; with the journal
    gone the second `down` says "no session" and touches nothing — see
    test_down_never_touches_the_pac_after_a_restore_whose_stop_failed.
    """
    port = journal.owner.control_port
    baseline = journal.baseline
    try:
        name = netproxy.resolve(journal.service)
    except netproxy.NetworkSetupError as error:
        _red(f"{error} — the settings recorded at `up` were {_pac_text(baseline)}; the journal is kept")
        return 1
    try:
        found = netproxy.pac_status(name)
    except netproxy.NetworkSetupError as error:
        _red(f"the PAC on '{name}' could not be read ({error}) — nothing was changed; the journal is kept")
        return 1
    if ownership.classify(found, port, baseline) is PacClass.FOREIGN:
        # A hand-set PAC is never written over. The proxy still goes, so nothing keeps routing to
        # it, and the journal stays: it is the only durable record of what the operator has to put
        # back, and a `down` after they have is what releases it — see
        # test_down_over_a_foreign_pac_prints_the_baseline_and_keeps_the_journal.
        _report_stop(journal.proxy, port)
        _red(
            f"the PAC on '{name}' is not this session's any more (now: {_pac_text(found)}); the settings "
            f"recorded at `up` were {_pac_text(baseline)} — not restored. Put them back by hand, then "
            f"`lyrebird down` again; or, to keep the PAC you set, delete {sess.journal_path}."
        )
        return 1
    try:
        _restore(name, port, baseline)
    except netproxy.NetworkSetupError as error:
        _red(
            f"could not restore the previous settings on '{name}': {error} — the journal is kept, `lyrebird down` again"
        )
        return 1
    target = ownership.restore_target(baseline)
    if isinstance(target, Off):
        click.echo(f"✓ PAC removed from '{name}' — direct networking restored")
    else:
        click.echo(f"✓ restored the previous PAC on '{name}': {target.url}")
    try:
        sess.unlink()
    except OSError as error:
        _red(
            f"the previous settings are back, but the journal could not be removed ({error}) — delete "
            f"{sess.journal_path} by hand"
        )
        return 1
    failure = _stop_failure(journal.proxy, port)
    if failure is not None:
        _red(f"the previous settings are back; {failure}")
        return 1
    click.echo(f"{ui.GREEN}stopped{ui.R}")
    return 0


def _stop_failure(ref: ownership.Ref, port: int) -> str | None:
    """Stop the recorded proxy, or say why it could not be proven stopped.

    A pid alone is never enough: after a clock step the create time no longer matches and the
    identity is unproven, which is a refusal to signal rather than a licence to — see
    test_down_refuses_to_signal_a_pid_it_cannot_prove_is_this_sessions.
    """
    try:
        outcome = procs.terminate(ref, port)
    except procs.ProcessCheckError as error:
        return (
            f"the proxy (pid {ref.pid}) could not be proved to be this session's ({error}) — stop it by hand "
            f"(`kill -9 {ref.pid}`)"
        )
    if outcome is procs.Termination.STILL_RUNNING:
        return f"the proxy (pid {ref.pid}) is still running — `kill -9 {ref.pid}`"
    return None


def _report_stop(ref: ownership.Ref, port: int) -> None:
    failure = _stop_failure(ref, port)
    if failure is not None:
        _red(failure)


# MARK: - children


def _child_env() -> dict:
    return {**os.environ, "LYREBIRD_PROFILE": str(config.PROFILE_DIR)}


def _proxy_argv() -> list[str]:
    return [
        str(MITMDUMP),
        "--set",
        f"lyrebird_control_port={config.CONTROL_PORT}",  # adjacent tokens: see `procs.is_proxy_on`
        "--listen-host",
        config.PROXY_LISTEN_HOST,
        "--listen-port",
        str(config.PROXY_PORT),
        "--set",
        f"confdir={config.mitmproxy_confdir()}",
        "-s",
        str(config.ROOT / "addon.py"),
    ]


def _start_fresh_log() -> None:
    """Begin each run on a new inode rather than truncating the old one.

    The log names every host and path that came through, so it is 0600 — but a log left by an
    older version may be 0644, and `chmod` cannot revoke a descriptor somebody already holds.
    Truncating in place keeps that inode, so a reader who opened it while it was readable goes on
    seeing new traffic. Renaming a fresh private file over it leaves them holding the old one.
    """
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(config.LOG_FILE, "")


# MARK: - scenario selection, shared by `use` and `up --use`


def _activate_scenario(name: str) -> None:
    """Make `name` the active scenario, and print what is active now.

    Shared by `use` and `up --use` so the two cannot drift into describing the same switch
    differently. Activation is also what rewinds the scenario's sequences, so the scenario starts
    from its first step rather than resuming where the last run left it — which is why `--use` on
    the scenario that is already active is not a no-op.
    """
    result = api._control("/__mock__/scenarios/active", "PUT", {"name": name})
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
            f"   restart the proxy: `lyrebird down && lyrebird up --use {name}`."
        )
        return f"the running proxy did not report whether '{name}' loaded whole"
    if problems:
        detail = "; ".join(problems)
        click.echo(
            f"{ui.RED}✗ scenario '{name}' did not load whole: {detail}{ui.R}\n"
            f"   the app was NOT relaunched — fix the file, then "
            f"`lyrebird down && lyrebird up --use {name}`."
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
        click.echo(f"{ui.RED}   could not select '{name}' — the app was NOT relaunched.{ui.R}{listing}")
        return f"could not select scenario '{name}'"
    return None


# MARK: - `status`


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable state on stdout.")
def status(as_json: bool) -> None:
    """Show intercept state (honest about whether the PAC is actually enabled).

    The exit code reports the state, not the formatting: 0 only when the proxy is up *and*
    intercepting *this* profile, whichever way you asked. `--json` changes what is printed and
    never what it means — a flag that decides how output is rendered must not also decide what
    success is, or `lyrebird status && …` silently proceeds against a proxy that is mocking
    nothing.

    It answers for the *requested* owner — this profile, this port — and reports the journal
    beside it: a closed control port is exit 1 whatever the journal says, which is what the CI
    launcher check and the subprocess test both depend on.
    """
    # One reading. A status taken from one and fields printed from another can disagree about which
    # proxy answered — see test_status_output_and_exit_code_come_from_one_reading.
    raw = api._health(config.CONTROL_PORT)
    running = (raw or {}).get("profileFingerprint")
    # A proxy that reports no fingerprint is not ours either: it predates the guard, and reading it
    # as ours had the app mutate a stranger's profile — see test_status_treats_a_proxy_without_a_fingerprint_as_foreign.
    foreign = raw is not None and running != config.PROFILE_FINGERPRINT

    # Lock-free: `status` writes nothing, and waiting on the lock behind a slow `up` would turn a
    # question into a hang.
    journal = _session().read()
    journal_error: str | None = journal.reason if isinstance(journal, Unreadable) else None
    if journal_error is None and not foreign and raw and isinstance(raw.get("journalError"), str):
        journal_error = raw["journalError"]

    route, route_error = _active_service()
    service, pac_error = _status_service(journal, route, route_error)
    pac: Pac | None = None
    if service is not None and pac_error is None:
        try:
            pac = netproxy.pac_status(service)
        except netproxy.NetworkSetupError as error:
            # Unproven, which is different from seen to be off — the exit code is the same, the
            # explanation is not, and a person reading "DISABLED" would go and switch it on.
            pac_error = str(error)
    ours = pac is not None and pac.url == ownership.our_url(config.CONTROL_PORT)
    # The route is part of "intercepting": our PAC on the journalled service routes nothing once the
    # default route has moved to another one, and a banner saying ACTIVE beside exit 1 sent people
    # the wrong way — see test_status_exits_1_when_the_route_moved_off_the_journalled_device.
    on_route = route is not None and (not isinstance(journal, SessionRecord) or route.device == journal.service.device)
    # And the proxy answering must be the one the journal names: a stranger on the port behind our
    # PAC printed INTERCEPT ACTIVE beside exit 1 — see test_status_exits_1_when_the_recorded_proxy_is_not_what_answers.
    recorded = not isinstance(journal, SessionRecord) or (raw or {}).get("pid") == journal.proxy.pid
    intercepting = pac is not None and pac.enabled and ours and not foreign and on_route and recorded
    reasons = _status_reasons(journal, raw, route, route_error, pac, ours, foreign, journal_error, pac_error)
    simulator = (
        {"udid": journal.simulator.udid, "name": journal.simulator.name}
        if isinstance(journal, SessionRecord) and journal.simulator is not None
        else None
    )

    if as_json:
        # Every field that describes a profile's state is whatever the proxy that answered said;
        # when that proxy is running another profile, none of it is this profile's, so it is
        # reported as unknown rather than handed over under this profile's name.
        mine = {} if foreign else (raw or {})
        click.echo(
            json.dumps(
                {
                    "proxyUp": raw is not None,
                    "intercepting": intercepting,
                    "profileMismatch": foreign,
                    "profileFingerprint": config.PROFILE_FINGERPRINT,
                    "runningProfileFingerprint": running,
                    "pacError": pac_error,
                    # Renamed from `runtimeError`: the per-port record it named is gone, and this is
                    # the session journal — reported from the local read *and* from the proxy's own.
                    "journalError": journal_error,
                    "activeScenario": mine.get("activeScenario"),
                    "overrideCount": mine.get("overrideCount"),
                    "scenarios": None if foreign else (raw or {}).get("scenarios", []),
                    # `null` when the running engine did not supply the field, never `[]`. A current
                    # engine always sends both, with one entry per rule — so `[]` is a real state ("no
                    # rules here") and a missing key is a capability signal ("this proxy cannot tell
                    # you"). Defaulting to `[]` collapsed those into the claim that nothing has answered.
                    "sequences": mine.get("sequences"),
                    "answers": mine.get("answers"),
                    "simBundleId": mine.get("simBundleId"),
                    "profile": str(config.PROFILE_DIR),
                    "service": service,
                    # `{"udid": …, "name": …}`, or null when no `up` has recorded one — which is a real
                    # state ("nothing here trusted a CA") and not the same as "the default device".
                    "simulator": simulator,
                    "pac": {"url": pac.url, "enabled": pac.enabled, "ours": ours} if pac else None,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"{ui.DIM}profile: {config.PROFILE_DIR}{ui.R}")
        if foreign:
            # Not the banner: "PAC is disabled/not ours" would send the operator to `lyrebird up`,
            # which refuses this exact situation.
            click.echo(
                f"{ui.BOLD}{ui.YELLOW}🟠 PROXY UP, ANOTHER PROFILE{ui.R} — nothing is intercepting for this profile."
            )
            click.echo(api._profile_mismatch(str(running)))
        elif pac_error and raw is not None:
            where = f" on '{service}'" if service else ""
            click.echo(
                f"{ui.BOLD}{ui.YELLOW}🟠 PROXY UP, PAC UNREADABLE{ui.R} — could not read the PAC{where}: {pac_error}"
            )
        else:
            ui._banner(raw, service, intercepting)
        for reason in reasons:
            click.echo(f"  {ui.RED}✗ {reason}{ui.R}")

    raise SystemExit(1 if reasons else 0)


def _active_service() -> tuple[ServiceRef | None, str | None]:
    try:
        return netproxy.active_service(), None
    except netproxy.NetworkSetupError as error:
        return None, str(error)


def _status_service(
    journal: ownership.Journal, route: ServiceRef | None, route_error: str | None
) -> tuple[str | None, str | None]:
    """Which service `status` reports on: the journal's, resolved by device, or — with no session —
    whichever carries the default route."""
    if isinstance(journal, SessionRecord):
        try:
            return netproxy.resolve(journal.service), None
        except netproxy.NetworkSetupError as error:
            return journal.service.name, str(error)
    if route is not None:
        return route.name, None
    return None, route_error


def _status_reasons(
    journal: ownership.Journal,
    raw: dict | None,
    route: ServiceRef | None,
    route_error: str | None,
    pac: Pac | None,
    ours: bool,
    foreign: bool,
    journal_error: str | None,
    pac_error: str | None,
) -> list[str]:
    """Every reason this `status` is not exit 0, in the order a person would act on them."""
    reasons: list[str] = []
    if foreign:
        reasons.append(f"another profile answers on port {config.CONTROL_PORT}")
    if journal_error:
        reasons.append(f"session journal: {journal_error} — `down` cannot restore from it")
    if not isinstance(journal, SessionRecord):
        reasons.append("no session holds the PAC — `lyrebird up`")
        return reasons
    if journal.owner != _owner():
        reasons.append(
            f"another session owns the PAC: port {journal.owner.control_port}, "
            f"profile {journal.owner.profile_fingerprint}"
        )
        return reasons
    port = journal.owner.control_port
    if raw is None or raw.get("pid") != journal.proxy.pid:
        reasons.append(f"the recorded proxy (pid {journal.proxy.pid}) is not what answers on port {port}")
    if pac_error is not None:
        reasons.append(f"the PAC on '{journal.service.name}' could not be read: {pac_error}")
    elif pac is None or not ours or not pac.enabled:
        reasons.append(f"the PAC on '{journal.service.name}' is not routing to this session")
    if route is None:
        reasons.append(f"the active network service could not be confirmed{f': {route_error}' if route_error else ''}")
    elif route.device != journal.service.device:
        reasons.append(f"route moved to {route.device} — the session's owner: `lyrebird down && lyrebird up`")
    return reasons
