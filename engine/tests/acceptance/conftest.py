"""The world the simulator acceptance check runs in: prerequisites, a throwaway profile, and the
cleanup that must put the Mac's network back however the run ended.

Nothing here is monkeypatched. This drives the installed layout — `bin/lyrebird`, its venv, a real
mitmdump, a real PAC on a real network service, a real app in a real simulator — because the
failures the issue behind it describes (CA trust, relaunch ordering, routing, cleanup) live exactly
in the parts a unit test replaces with a double. Where Lyrebird ships a command for something, that
command is what runs: `up --simulator`, `lyrebird relaunch`, `assert-answered --run`. `simctl` is
called directly only for what Lyrebird has no command for — install, uninstall, boot, shutdown, and
reading the app's container.

Prerequisites are settled before anything is started, and they split two ways on purpose:

* **Skip** when the machine simply cannot run this — no full Xcode, no xcodegen, no booted
  simulator. Those are environment facts, and reporting them as failures would make a laptop
  without a simulator look like a regression.
* **Fail** when the machine *could* run it but the answer would not mean anything — two booted
  simulators, a simulator named in the environment that does not exist, another Lyrebird already
  holding the network service's PAC.

Once the prerequisites pass, nothing skips.

**The cleanup is not allowed to depend on the thing under test.** The `down` *phase* goes
through the shipped command, because putting the network back is its job and this is where that job
is checked. The *finalizer* does not: an ordinary `down` finds the session from any directory,
profile or port, so once this run's own session has been released, a contributor's session started
in the meantime is what that `down` would tear down. It calls the same executor in-process —
`supervisor.down_session(session, only_owner=…)` — with an ownership precondition evaluated inside
its lock. The settings are then read back from `networksetup` directly, and if they are not the
ones recorded before anything started, this stops the processes this run's journal names and falls
back to `_restore_by_hand` — which imports nothing from `supervisor`, so a defect in that
executor's wiring cannot defeat both the teardown and the cleanup, and decides with the same pure
function and writes with the same primitives, so it cannot drift from the product's rules either.
It writes only over a journal carrying this run's own owner, and reports both the original failure
and the fact that it had to act.

**There is one interruption path.** SIGTERM and SIGHUP are turned into `KeyboardInterrupt`, which
is what a Ctrl-C already raises, so every route out of a run is the route pytest already unwinds:
each finalizer runs, in order, and the report is still printed. The handlers do nothing else — no
cleanup of their own to get half-done, no diagnostics to be thrown away. Every CLI call runs in its
own process group and kills that group before it propagates a timeout or an interrupt, so an
interrupted run never leaves a `lyrebird` — or the `networksetup` it had started — behind to
undo the restore that follows.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest
import simstate

import api
import netproxy
import ownership
import procs
import session
import supervisor

REPO = Path(__file__).resolve().parents[3]
LYREBIRD = REPO / "bin" / "lyrebird"
FIXTURE_APP_DIR = REPO / "acceptance" / "FixtureApp"
FIXTURE_SCENARIOS = REPO / "acceptance" / "fixture-scenarios"
BUNDLE_ID = "com.example.lyrebird-fixture"
# What the fixture app calls, and what `lyrebird init`'s profile must already intercept.
FIXTURE_HOST = "api.example.com"

# The three files the app reports through. Named here because the container is checked for their
# absence before the first launch: a stale one would let an earlier run's evidence stand in for
# this one's.
EVIDENCE_FILES = ("launches.json", "results.json", "displayed.json")

# Any Lyrebird PAC, whatever port it was started on. Matching `/proxy.pac` alone would refuse to
# run on a machine behind an ordinary corporate PAC, which is precisely a case worth checking the
# restore against.
LYREBIRD_PAC = re.compile(r"^http://127\.0\.0\.1:\d+/proxy\.pac$")

# How long a launch has to route its request through the proxy and write the result. The app itself
# retries a transport error twice a second apart, so this is a launch plus those retries plus the
# simulator being slow the first time an app is started on it.
RESULT_TIMEOUT = 90.0
BOOT_TIMEOUT = 240
# `down` stops a proxy and talks to `networksetup`; generous, but bounded, because a cleanup that
# blocks forever is a cleanup that never happens.
DOWN_TIMEOUT = 120
# How long a signalled process gets to disappear, and how long the watchdog gets to do the
# restoring before the cleanup stops waiting and does it itself.
KILL_WAIT = 15.0
# How long a proxy `down` has already asked to stop may take to go before it counts as a stray.
SHUTDOWN_GRACE = 5.0
WATCHDOG_GRACE = 30.0
# How long the finalizer waits for the session lock. Longer than a watchdog tick and shorter than
# a run: a lock it cannot take is a thing to report, never a thing to wait out forever.
LOCK_WAIT = 60.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything collected from this directory `acceptance`, whatever the module remembered.

    `addopts` deselects that marker, so this is what keeps a check that rewires the network out of
    `pytest tests/ -q`. Left to a `pytestmark` line, a new module that omitted it would join the
    fast suite silently — and the first anyone would know is a developer's PAC pointing at a proxy
    their test run had already stopped.
    """
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.acceptance)


def _run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _try_run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess | None:
    """`_run` for the cleanup path: a command that could not even be started, or that hung, is a
    thing to report — never an exception thrown out of a finalizer that still has work to do."""
    try:
        return _run(args, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _run_grouped(args: list[str], env: dict, timeout: float) -> subprocess.CompletedProcess:
    """Run a `lyrebird` command in its own process group, leaving nothing behind either way.

    `subprocess.run` kills only the process it started. The CLI shells out to `networksetup`, so a
    timeout — or an interrupt — that killed the CLI alone could leave a `networksetup` still
    running, which would then set the PAC *after* the cleanup had read it back and called it
    restored. A new session makes the CLI and everything it spawns one group, and the group is what
    is signalled.

    Not the proxy and the watchdog: `up` detaches those into sessions of their own on purpose, and
    the session journal names them for the cleanup to find. Killing them here would make every `up`
    its own teardown.

    Because every failure path kills and reaps before it propagates, there is never an in-flight
    CLI child by the time a finalizer runs — which is why nothing has to track one.
    """
    with subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                process.communicate(timeout=30)  # reap, and drain the pipes we are closing
            raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


class _Interrupts:
    """The only interruption mechanism in this harness.

    Three rules, and they exist because a run that is interrupted has more to put back than a run
    that finishes:

    * a terminating signal becomes `KeyboardInterrupt`, which is what Ctrl-C already raises — so
      there is one route out of a run, the one pytest unwinds properly, and every finalizer runs;
    * **only the first one does.** Everything after the first signal is teardown: restoring the
      network, uninstalling the app, shutting the simulator down. A second signal landing in the
      middle of that leaves the machine worse than the first one found it, so it is recorded and
      dropped. This was not theoretical — three SIGTERMs in quick succession left the simulator
      booted and the app installed, with the finalizers dying in interpreter shutdown;
    * and `deferred()` extends the same protection to the cleanup of a run nothing signalled, where
      the *first* signal would otherwise arrive with `down` having already stopped the watchdog —
      abandoning the restore between two `networksetup` calls, with the Mac routed at a port whose
      proxy is gone and nothing left running that would notice.

    Absorbing a signal is not something to do lightly, and it is bounded: what remains is a restore
    and two `simctl` calls, each with its own timeout, and `kill -9` is still `kill -9`.
    """

    def __init__(self) -> None:
        self.quiet = False  # has the run started unwinding?
        self.raised = False  # did a signal already become a KeyboardInterrupt?
        self.deferred: list[int] = []

    def handle(self, signum: int, frame: object) -> None:
        if self.quiet:
            self.deferred.append(signum)
            return
        self.quiet = True
        self.raised = True
        raise KeyboardInterrupt(f"terminated by signal {signum}")

    @contextlib.contextmanager
    def deferred_signals(self) -> Iterator[None]:
        """No signal may stop what happens inside this block."""
        self.quiet = True
        yield
        # Deliberately not restored: whatever runs after this block is the rest of the teardown.

    def pending(self) -> int | None:
        """A signal that arrived while nothing was allowed to act on it, and that nothing has
        turned into a `KeyboardInterrupt` yet."""
        if self.raised or not self.deferred:
            return None
        return self.deferred[0]


INTERRUPTS = _Interrupts()


@contextlib.contextmanager
def _uninterrupted() -> Iterator[None]:
    """Let one teardown step finish, then honour a signal that arrived while it ran.

    Wrapped round the finalizers that put the machine back, not only the network one: a first
    signal during `simctl uninstall` or `simctl shutdown` would otherwise abandon it, and setup can
    fail before the harness — and therefore before `clean_up`'s own deferral — exists at all.
    """
    with INTERRUPTS.deferred_signals():
        yield
    signum = INTERRUPTS.pending()
    if signum is not None:
        raise KeyboardInterrupt(
            f"terminated by signal {signum}; the teardown step in progress was allowed to finish first"
        )


@pytest.fixture(scope="session", autouse=True)
def _interruptible() -> Iterator[None]:
    """Route every terminating signal through `INTERRUPTS`.

    A `kill` on the pytest process is not exotic — a CI step timing out, a terminal window closed —
    and without this it runs no finalizer at all: the app stays installed, the simulator stays
    booted, and the detached proxy keeps the PAC pointing at itself.

    Autouse and dependency-free, so it is set up before the first fixture that changes anything and
    torn down after the last one has put it back.
    """
    previous = {
        signum: signal.signal(signum, INTERRUPTS.handle) for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _fail(what: str, result: subprocess.CompletedProcess) -> None:
    pytest.fail(
        f"{what}\n  command: {' '.join(result.args)}\n"
        f"  exit: {result.returncode}\n  stdout:\n{result.stdout}\n  stderr:\n{result.stderr}"
    )


def _free_ports(count: int) -> list[int]:
    """`count` distinct ports nothing holds right now.

    Every socket is held open until the last one is bound, then all are released together. Binding
    and closing one at a time can hand back the same ephemeral port twice, and the proxy and the
    control server sharing a port fails as "the proxy did not become healthy", which names neither.
    """
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    assert len(set(ports)) == count, ports
    return ports


def _service_table() -> list[tuple[str, str]]:
    """Every (name, device), read from macOS by this harness rather than through `netproxy`.

    A `(*)` entry is a disabled service, which still holds its PAC — so the marker is part of the
    pattern rather than a reason to drop the line.
    """
    order = _try_run(["networksetup", "-listnetworkserviceorder"], timeout=60)
    if order is None or order.returncode != 0:
        return []
    found = re.findall(r"\((?:\d+|\*)\)\s*(.+?)\n\(Hardware Port:.*?Device:\s*(\w+)\)", order.stdout)
    return [(name.strip(), device) for name, device in found]


def _active_service() -> ownership.ServiceRef | None:
    """The service carrying the default route — name *and* device — resolved the way macOS
    reports it.

    Deliberately not `lyrebird status --json`, and deliberately not an import of `netproxy`:
    `status` answers with the service the *journal* names, so once `up` has run it echoes back the
    value this harness recorded and a guard built on it compares a number with itself. This is the
    second opinion — the same two questions the engine asks the OS, asked here, so a route that
    moved is seen whatever Lyrebird believes.
    """
    route = _try_run(["route", "-n", "get", "default"], timeout=30)
    if route is None:
        return None
    interface = re.search(r"interface:\s*(\S+)", route.stdout)
    if not interface:
        return None
    for name, device in _service_table():
        if device == interface.group(1):
            return ownership.ServiceRef(name=name, device=device)
    return None


def _devices() -> list[dict]:
    result = _run(["xcrun", "simctl", "list", "devices", "-j"], timeout=120)
    if result.returncode != 0:
        _fail("`xcrun simctl list devices` failed", result)
    listing = json.loads(result.stdout)
    return [device for runtime in listing["devices"].values() for device in runtime]


def _require_xcode() -> None:
    """Skip unless a full Xcode with a simulator platform is selected.

    `xcrun` alone proves nothing: the Command Line Tools ship the shims without the simulator
    platform behind them, and `simctl` then fails in a way that reads as a broken check rather
    than as a machine that cannot run one.
    """
    if shutil.which("xcrun") is None or shutil.which("xcodebuild") is None:
        pytest.skip("no `xcrun`/`xcodebuild` on PATH — these checks need Xcode")
    selected = _run(["xcode-select", "-p"], timeout=60)
    developer = Path(selected.stdout.strip()) if selected.returncode == 0 else None
    if developer is None or not (developer / "Platforms/iPhoneSimulator.platform").is_dir():
        pytest.skip(
            "no full Xcode selected (`xcode-select -p` does not point at a developer "
            "directory with an iPhoneSimulator platform) — these checks need one"
        )


@pytest.fixture(scope="session")
def simulator() -> Iterator[str]:
    """The udid this run means, everywhere.

    It is passed to `up --simulator`, so the CA and the relaunch are bound to it rather than to
    whatever `booted` would resolve to. This still refuses to run with two simulators booted: the
    harness installs the app and reads its container itself, and "which one" is then a question
    nothing here can answer.
    """
    _require_xcode()

    wanted = os.environ.get("LYREBIRD_ACCEPTANCE_SIMULATOR")
    devices = _devices()
    booted = [device for device in devices if device.get("state") == "Booted"]

    if wanted:
        named = [device for device in devices if device["udid"] == wanted or device.get("name") == wanted]
        if not named:
            # A name that names nothing is a typo, not a machine without a simulator: say so
            # rather than skipping and letting the run look like it checked something.
            pytest.fail(
                f"LYREBIRD_ACCEPTANCE_SIMULATOR={wanted!r} matches no simulator "
                f"(`xcrun simctl list devices` lists {len(devices)})"
            )
        if len(named) > 1:
            pytest.fail(f"LYREBIRD_ACCEPTANCE_SIMULATOR={wanted!r} matches {len(named)} simulators — give a udid")
        device = named[0]
        strangers = [other for other in booted if other["udid"] != device["udid"]]
        if strangers:
            pytest.fail(
                "more than one simulator would be booted, and this run installs the app "
                "into one of them. Shut these down first: "
                + ", ".join(f"{other['name']} ({other['udid']})" for other in strangers)
            )
    else:
        if not booted:
            pytest.skip(
                "no booted simulator. Boot one (`xcrun simctl boot <udid>`) or set "
                "LYREBIRD_ACCEPTANCE_SIMULATOR=<udid-or-name> to have this boot it"
            )
        if len(booted) > 1:
            pytest.fail(
                "more than one simulator is booted, and this run installs the app into "
                "one of them. Shut all but one down: "
                + ", ".join(f"{other['name']} ({other['udid']})" for other in booted)
            )
        device = booted[0]

    udid = device["udid"]
    # Set before the call that starts it, not after: `simctl boot` can time out having already
    # booted the device, and a shutdown owed only on the success path is a simulator left running.
    ours = device.get("state") != "Booted"
    try:
        if ours:
            result = _run(["xcrun", "simctl", "boot", udid], timeout=BOOT_TIMEOUT)
            if result.returncode != 0:
                _fail(f"could not boot {device['name']} ({udid})", result)
        status = _run(["xcrun", "simctl", "bootstatus", udid], timeout=BOOT_TIMEOUT)
        if status.returncode != 0:
            _fail(f"{device['name']} ({udid}) did not finish booting", status)
        yield udid
    finally:
        if ours:
            # Only what this run started. A simulator the developer had open stays open. Checked:
            # "shut it down again" is a claim the docs make on this run's behalf.
            with _uninterrupted():
                stopped = _try_run(["xcrun", "simctl", "shutdown", udid], timeout=BOOT_TIMEOUT)
            if stopped is None or stopped.returncode != 0:
                pytest.fail(
                    simstate.cleanup_failure(
                        f"this run booted {device['name']} ({udid}) and could not shut it down again",
                        stopped,
                        udid,
                    )
                )


def _documents(udid: str) -> Path:
    result = _run(["xcrun", "simctl", "get_app_container", udid, BUNDLE_ID, "data"], timeout=120)
    if result.returncode != 0:
        _fail(f"could not find {BUNDLE_ID}'s data container", result)
    return Path(result.stdout.strip()) / "Documents"


@pytest.fixture(scope="session")
def fixture_app(simulator: str) -> Iterator[Path]:
    """Build `acceptance/FixtureApp`, install it into a container with nothing in it, and hand back
    the Documents directory its evidence is read from.

    The container is checked rather than assumed. `simctl uninstall` is allowed to fail — the app
    may not be installed — and an uninstall that failed for any *other* reason would leave an
    earlier run's records in place, where the first check would read them as this run's.
    """
    if shutil.which("xcodegen") is None:
        pytest.skip("xcodegen is not installed (run `make setup-app`, then `make acceptance`)")

    generated = _run(
        ["xcodegen", "generate", "--project", str(FIXTURE_APP_DIR), "--spec", str(FIXTURE_APP_DIR / "project.yml")]
    )
    if generated.returncode != 0:
        _fail("xcodegen could not generate the fixture app project", generated)

    built = _run(
        [
            "xcodebuild",
            "-project",
            str(FIXTURE_APP_DIR / "FixtureApp.xcodeproj"),
            "-scheme",
            "FixtureApp",
            "-configuration",
            "Debug",
            "-destination",
            "generic/platform=iOS Simulator",
            "-derivedDataPath",
            str(FIXTURE_APP_DIR / ".build"),
            "build",
        ]
    )
    if built.returncode != 0:
        _fail("the fixture app did not build", built)

    app = FIXTURE_APP_DIR / ".build/Build/Products/Debug-iphonesimulator/FixtureApp.app"
    if not app.is_dir():
        pytest.fail(f"xcodebuild reported success but there is no app at {app}")

    _try_run(["xcrun", "simctl", "uninstall", simulator, BUNDLE_ID])  # may not be installed; fine
    # Owed from before the install is attempted: an install that timed out may have finished, and
    # everything after it — the container lookup included — can fail with the app on the device.
    try:
        result = _run(["xcrun", "simctl", "install", simulator, str(app)])
        if result.returncode != 0:
            _fail(f"could not install {app.name} into {simulator}", result)

        # Resolved once. It does not move for the life of the install, and asking simctl on every
        # poll turned a half-second wait into a subprocess per half-second.
        documents = _documents(simulator)
        stale = [name for name in EVIDENCE_FILES if (documents / name).is_file()]
        if stale:
            pytest.fail(
                f"{BUNDLE_ID} was reinstalled and its container still holds "
                f"{', '.join(stale)} from an earlier run — every check below would read "
                f"those as this run's.\n  container: {documents}\n"
                f"  fix: `xcrun simctl uninstall {simulator} {BUNDLE_ID}`"
            )
        yield documents
    finally:
        # The CA stays trusted in this simulator — simctl offers no way to remove one root cert —
        # so removing the app is as clean as teardown gets. See acceptance/README.md.
        with _uninterrupted():
            removed = _try_run(["xcrun", "simctl", "uninstall", simulator, BUNDLE_ID])
        if removed is None or removed.returncode != 0:
            pytest.fail(
                simstate.cleanup_failure(f"could not uninstall {BUNDLE_ID} from {simulator}", removed, simulator)
            )


class Pac(NamedTuple):
    """A network service's auto-proxy settings, as macOS reports them."""

    url: str
    enabled: bool

    def describe(self) -> str:
        return f"URL: {self.url or '(none)'} · Enabled: {'Yes' if self.enabled else 'No'}"


class Harness:
    """One object the check drives the whole world through: the real CLI, the real simulator, and
    the app's own record of what it launched, received and displayed."""

    def __init__(self, udid: str, documents: Path, profile: Path, state: Path, env: dict) -> None:
        self.udid = udid
        self.documents = documents
        self.profile = profile
        self.state = state
        self.env = env
        # By device as well as by name: a rename of this run's service, with the old name handed to
        # another device, would otherwise have the harness read the other one and report a failed
        # restore after production correctly restored this one.
        self.service: ownership.ServiceRef | None = None
        self.baseline = Pac("", False)
        self.control_port = env["LYREBIRD_CONTROL_PORT"]
        self.our_pac_url = f"http://127.0.0.1:{self.control_port}/proxy.pac"
        self.cleaned = False

    # MARK: - The CLI

    def run(self, *args: str, expect: int = 0, timeout: float = 180) -> subprocess.CompletedProcess:
        """Run the installed launcher and hold it to an exit code.

        Every call asserts. `up` exiting 0 is the postcondition "the proxy is running and the PAC
        routes to it", and a check that ignored it would go on to blame the app for a network that
        was never rewired.
        """
        result = self.attempt(*args, timeout=timeout)
        if result.returncode != expect:
            _fail(f"`lyrebird {' '.join(args)}` exited {result.returncode}, expected {expect}", result)
        return result

    def attempt(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
        """The same call without the assertion, for the cleanup — which has to go on to check the
        network whatever the command did."""
        return _run_grouped([str(LYREBIRD), "--profile", str(self.profile), *args], self.env, timeout)

    def up(self, *args: str, expect: int = 0) -> subprocess.CompletedProcess:
        """`up`, always naming the device this run means.

        Never the default "whatever is booted": the CA is trusted in, and the app relaunched on,
        the device named here, and the whole point of this check is that the app it reads is the
        app that got that CA.
        """
        return self.run("up", "--simulator", self.udid, *args, expect=expect)

    def relaunch(self) -> None:
        """`lyrebird relaunch`, not `simctl` — the shipped path, and the one that relaunches on the
        device `up` recorded rather than on whatever simctl would call `booted`."""
        self.run("relaunch")

    def phase(self, name: str) -> None:
        """Announce a phase, and stop the run if the network moved under it.

        `up` resolves the active network service every time it runs, so a route that changed
        mid-run — Wi-Fi to Ethernet — would have the next phase install a PAC on a service this run
        never recorded and cannot promise to restore. Checked here rather than compensated for
        later: the honest answer is to stop mutating and say so, with the recorded service still
        restorable because nothing else was touched.

        Asked of macOS rather than of `status --json`, which reports the service the journal names
        and would therefore agree with this run by construction. A moved route is a refusal in the
        engine too — `up` says `lyrebird down && lyrebird up` — so this stops rather than following
        it, and compares *devices*: a rename is not a move.
        """
        print(f"— phase: {name}", flush=True)
        now = _active_service()
        here = self.service.device if self.service else None
        if now is None or now.device != here:
            pytest.fail(
                f"the default route moved from device '{here}' to '{now.device if now else None}' "
                f"before the phase '{name}'. Stopping: a PAC installed on the new service is not "
                f"one this run recorded a baseline for. The recorded service is restored on the way "
                f"out; check the new one by hand if an earlier phase reached it."
            )

    def status(self) -> dict:
        """`status --json` whatever it exits — the exit code is the intercept state, and parts of
        this check expect it to be 1."""
        result = self.attempt("status", "--json", timeout=60)
        try:
            state: dict = json.loads(result.stdout)
        except json.JSONDecodeError:
            _fail("`lyrebird status --json` did not print JSON", result)
        return state

    def journal(self) -> ownership.Journal:
        """What the session journal says, read from the *real* per-user root.

        There is one per user and no environment variable moves it, which is why the prerequisites
        refuse to start over somebody else's — and why the acceptance conftest overrides the fast
        suite's temporary root: the `up` that wrote this ran in another process.
        """
        return session.Session().read()

    def owner(self) -> ownership.Owner:
        """This run's complete `Owner` — all three fields, exactly as `config` computes them."""
        return ownership.Owner(
            control_port=int(self.control_port),
            profile_fingerprint=hashlib.sha256(str(self.profile.resolve()).encode("utf-8")).hexdigest()[:12],
            state_root=str(Path(self.state).resolve()),
        )

    # MARK: - The network, read from macOS rather than from Lyrebird

    def service_name(self) -> str | None:
        """The name that carries this run's recorded *device* now — `netproxy.resolve`'s rule,
        written here so a rename cannot make the harness read another service and report a failed
        restore after production correctly restored this one."""
        if self.service is None:
            return None
        on_device = [name for name, device in _service_table() if device == self.service.device]
        if self.service.name in on_device:
            return self.service.name
        return on_device[0] if len(on_device) == 1 else None

    def _read_pac(self) -> Pac | None:
        """None when macOS could not be asked, or when the recorded device carries no one service —
        which is not the same as "there is no PAC", and is why the cleanup treats it as a failure to
        verify rather than as a clean network."""
        name = self.service_name()
        if name is None:
            return None
        result = _try_run(["networksetup", "-getautoproxyurl", name], timeout=60)
        if result is None or result.returncode != 0:
            return None
        url = re.search(r"URL:\s*(\S+)", result.stdout)
        found = url.group(1) if url else ""
        return Pac("" if found.lower() == "(null)" else found, "Enabled: Yes" in result.stdout)

    def pac(self) -> Pac:
        now = self._read_pac()
        if now is None:
            pytest.fail(f"could not read the PAC on device '{self.service.device if self.service else None}'")
        return now

    def pac_is_ours(self) -> bool:
        """Enabled, and pointing at *this* run's control port — not merely "different from before"."""
        now = self._read_pac()
        return now is not None and now.enabled and now.url == self.our_pac_url

    def restored(self, now: Pac | None = None) -> bool:
        """Are the settings back to what this run found?

        A *verification* predicate, and only that. The snapshot it compares against is what the run
        is checked and reported with; the journal's `baseline` — observed under the lock at
        acquisition — is the only target anything ever writes, because a user who reconfigures the
        PAC between this snapshot and `up` has changed what "restored" means and only the journal
        knows it.

        Not a string comparison, because the target is plan-v6's:

        * an empty baseline has the target `Off`, and macOS rejects an empty PAC URL — so all that
          can be restored is the flag, leaving whatever URL is in the field. Disabled-with-a-
          Lyrebird-URL is the restored state on a machine that started with none;
        * a *disabled Lyrebird* baseline — residue from an earlier run, possibly on another port —
          has the same `Off` target, so demanding that exact old URL back would fail a correct
          restore;
        * every other baseline comes back exactly, flag and all.
        """
        now = now if now is not None else self._read_pac()
        if now is None:
            return False  # unread is not restored
        off_target = not self.baseline.url or (LYREBIRD_PAC.match(self.baseline.url) and not self.baseline.enabled)
        if off_target:
            return not now.enabled and (now.url == "" or bool(LYREBIRD_PAC.match(now.url)))
        return (now.url, now.enabled) == (self.baseline.url, self.baseline.enabled)

    # MARK: - The app

    def _records(self, name: str) -> list[dict]:
        path = self.documents / name
        if not path.is_file():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []  # caught mid-write; the caller is polling

    def launches(self) -> list[dict]:
        """One record per launch, written before any networking. This is what "the app was not
        launched" is asserted against: a result only appears once a request has *finished*, so an
        app that was launched and then hung would look identical to one that was never started."""
        return self._records("launches.json")

    def results(self) -> list[dict]:
        """One record per launch: what the app got back."""
        return self._records("results.json")

    def displayed(self) -> list[dict]:
        """One record per rendered label, written from the value the label is bound to."""
        return self._records("displayed.json")

    def wait_for_results(self, count: int, timeout: float = RESULT_TIMEOUT) -> list[dict]:
        """Block until the app has recorded `count` launches, and say what the proxy saw if it
        never does — "nothing arrived" and "it arrived somewhere else" need different fixes."""
        records = self._wait(self.results, lambda seen: len(seen) >= count, timeout)
        if len(records) < count:
            recent = self.attempt("recent", "--json", timeout=60)
            pytest.fail(
                f"the fixture app recorded {len(records)} result(s) in {timeout:g}s, expected "
                f"{count}.\n  app records: {json.dumps(records, indent=2)}\n"
                f"  proxy saw: {recent.stdout or recent.stderr}"
            )
        return records

    def wait_for_displayed(self, marker: str, timeout: float = RESULT_TIMEOUT) -> str:
        """Block until the label has rendered a string carrying `marker`.

        Polled rather than read once: the app records the result before it publishes the new
        summary, so the label's record lands a moment after the one the caller just read.
        """

        def shown(records: list[dict]) -> bool:
            return any(marker in str(record.get("text", "")) for record in records)

        records = self._wait(self.displayed, shown, timeout)
        if not shown(records):
            pytest.fail(
                f"the response arrived but no label carrying {marker!r} was rendered "
                f"within {timeout:g}s.\n  labels: {json.dumps(records, indent=2)}"
            )
        return next(str(record["text"]) for record in records if marker in str(record.get("text")))

    @staticmethod
    def _wait(read, done, timeout: float) -> list[dict]:
        deadline = time.time() + timeout
        records = read()
        while not done(records) and time.time() < deadline:
            time.sleep(0.5)
            records = read()
        return records

    # MARK: - Putting the machine back

    def clean_up(self) -> list[str]:
        """Stop the proxy and restore the network, without letting a signal stop it half-way.

        Safe to call twice. Raises nothing except the interrupt it deferred: a terminating signal
        arriving *during* the restore is recorded and re-raised once the network is back, so the
        run still ends and the rest of the unwinding still happens. `_Interrupts` says why that is
        worth absorbing a signal for.
        """
        if self.cleaned:
            return []
        self.cleaned = True

        with INTERRUPTS.deferred_signals():
            problems = self._restore_the_network()

        signum = INTERRUPTS.pending()
        if signum is not None:
            detail = f"; the cleanup reported: {' | '.join(problems)}" if problems else ""
            raise KeyboardInterrupt(
                f"terminated by signal {signum} during the cleanup, which was allowed to finish first{detail}"
            )
        return problems

    def _restore_the_network(self) -> list[str]:
        """The owner-checked teardown, then macOS's own account of whether it worked.

        Every attempt goes through `supervisor.down_session(only_owner=…)` — the same executor the
        shipped `down` runs, with an ownership precondition evaluated inside its lock — so a
        successor session appearing before any of them is refused rather than torn down. Nothing
        here trusts the result: the settings are read back from `networksetup`, and if they are
        wrong the independent fallback below acts instead of leaving the Mac routed at a port whose
        proxy this run just stopped. Every step it had to take is returned, so a run whose cleanup
        was performed for it fails loudly instead of looking tidy.
        """
        problems: list[str] = []
        disposition, archive = "none", None
        released = refused = False
        try:
            for attempt in range(2):
                outcome, failure = self._attempt_down()
                if outcome is None:
                    # A wiring error in the executor must not skip the rest of the cleanup and
                    # leave an owned `Active` routed at a dead proxy.
                    problems.append(failure or "the in-process teardown failed for an unknown reason")
                    break
                if disposition == "none" and outcome.disposition != "none":
                    # The first non-`none` disposition is what this run did; a later `Absent`
                    # observation must never overwrite an earlier `archived`.
                    disposition, archive = outcome.disposition, outcome.archive
                if outcome.refused is not None:
                    problems.append(f"the teardown was refused, and wrote nothing: {outcome.refused}")
                    refused = True
                    break
                if outcome.released:
                    released = True
                    break
                if attempt:
                    # Retried while an owner-matching journal remains — a `Restored(ref)` kept
                    # because the proxy would not die still needs accounting and unlinking.
                    problems.append("a session record of this run remains after two teardown attempts")
            if disposition == "archived":
                problems.append(
                    f"this run's session was archived ({archive}); its previous settings were never put back "
                    f"by Lyrebird — see that file"
                )

            problems.extend(self._stop_our_proxies())

            settled = self.restored()
            if not settled:
                problems.append(
                    f"the Mac's auto-proxy settings were not restored on "
                    f"'{self.service_name()}' by the teardown.\n"
                    f"  before: {self.baseline.describe()}\n"
                    f"  after:  {self._describe_now()}"
                )
            # The fallback runs for an unrestored PAC *and* for a record that is still there: a
            # terminal record whose refs were never accounted for is a live recorded proxy the
            # independent scan excludes by design, and nothing else would ever stop it. It never
            # runs after a refusal — that journal is provably somebody else's.
            if not refused and (not settled or not released):
                problems.extend(self._restore_by_hand())
        except BaseException as unexpected:  # noqa: BLE001 - a finalizer that raises cleans nothing
            # Not the deferred signals — those cannot arrive here any more. Anything else that goes
            # wrong is reported rather than raised, so the caller still hears about the network.
            problems.append(
                f"the cleanup itself failed with {unexpected!r}; the network may not "
                f"be restored — check System Settings ▸ Network ▸ {self.service_name()} ▸ "
                f"Proxies"
            )
        return problems

    def _attempt_down(self) -> tuple[object | None, str | None]:
        """One in-process teardown, wrapped whole.

        `Exception`, not `OSError`/`SubprocessError`: this is a function call now, and a traceback
        out of it used to become an exit status nobody read. The traceback goes in the report and
        the finalizer continues to the independent fallback.
        """
        try:
            return supervisor.down_session(session.Session(), only_owner=self.owner()), None
        except Exception as error:  # noqa: BLE001 - reported, never raised out of a finalizer
            return None, f"the in-process `down` raised {error!r}\n{traceback.format_exc()}"

    def _describe_now(self) -> str:
        now = self._read_pac()
        return now.describe() if now else "(could not be read)"

    # MARK: - The independent fallback
    #
    # plan-v6 asks for this to exist *and* to be independent: it imports nothing from
    # `supervisor`, so a defect in that executor's wiring cannot defeat both the teardown and the
    # cleanup. It cannot drift from the product's rules either, because it decides with the same
    # pure function (`ownership.decide_down`) and writes with the same primitives
    # (`netproxy.write_pac_*` under the session lock). It is the executor's row, written a second
    # time — the one place that duplication is worth having.

    def _restore_by_hand(self) -> list[str]:
        """Put this run's own journal right, under one uninterrupted lock, or say what remains.

        It writes **only** over a journal carrying this run's complete `Owner`. Over anything else
        — no journal, another owner's, one that cannot be read, one that names nobody, a terminal
        checkpoint — it writes nothing and prints what remains and `lyrebird down` for a person to
        run knowingly.
        """
        notes = [
            "this harness acted itself, because a cleanup that depends on the command "
            "it is checking cannot be relied on to run"
        ]
        store = session.Session()
        try:
            store.ensure_root()
            with store.locked(timeout=LOCK_WAIT) as lock_fd:
                notes.extend(self._under_the_lock(store, lock_fd))
        except (OSError, session.LockBusy, netproxy.NetworkSetupError, procs.ProcessCheckError) as error:
            notes.append(f"AND FAILED: {error!r}; run `lyrebird down` and check the proxy settings by hand")
        return notes

    def _owner_gate(self, journal: ownership.Journal) -> str | None:
        """Proceed only over a journal that provably carries this run's owner.

        `Unreadable` and `Archived(Unknown)` name nobody — the second by definition — so whose the
        PAC is cannot be proven, and acting would risk a successor whose record merely failed to
        decode. `Absent` is refused too: after a release through `Archived` the baseline was given
        up, and no journal is no authority to put a preflight snapshot back.
        """
        mine = self.owner()
        if isinstance(journal, ownership.Absent):
            return "no session journal remains: nothing here is this run's to put back"
        if isinstance(journal, ownership.Unreadable):
            return (
                f"the session journal cannot be read ({journal.reason}); whose it is cannot be proven, "
                f"so nothing was written — run `lyrebird down`"
            )
        if isinstance(journal, ownership.Archived):
            if isinstance(journal.context, ownership.Unknown):
                return "the archived session names no owner; nothing was written — run `lyrebird down`"
            if journal.context.owner != mine:
                return _not_ours(journal.context.owner)
            return None
        if journal.owner != mine:
            return _not_ours(journal.owner)
        return None

    def _under_the_lock(self, store: session.Session, lock_fd: int) -> list[str]:
        journal = store.read()
        gate = self._owner_gate(journal)
        if gate is not None:
            return [gate]
        if isinstance(journal, ownership.Archived) or isinstance(journal.phase, ownership.Restored):
            return self._terminal(store, journal)
        if isinstance(journal.phase, ownership.Acquiring) and journal.phase.proxy is None:
            # Nothing was installed, so there is no PAC to put back — only the scan's extras.
            return [
                "this run's `up` recorded no proxy; nothing was installed",
                *self._release(store, journal),
            ]
        return self._holding(store, lock_fd, journal)

    def _terminal(self, store: session.Session, journal) -> list[str]:
        """A `Restored` or `Archived(Known)` this run left: the PAC is never touched again, but the
        refs the record names are still accounted for and the record still has to go.

        The barrier comes first: `write` may have replaced the journal and then failed its
        directory sync, and an unlink over a visible-but-undurable checkpoint could let a crash
        resurrect the earlier `Active`.
        """
        try:
            store.barrier()
        except OSError as error:
            return [f"AND FAILED: the checkpoint could not be made durable ({error!r}); the journal is kept"]
        notes = ["the session was already checkpointed; the PAC was not touched"]
        if not self.restored():
            notes.append(f"the settings still read {self._describe_now()}, wanted {self.baseline.describe()}")
        # A terminal record still names refs, and nothing else will ever stop them: both guarded
        # teardown attempts raising before their accounting would otherwise leave a live recorded
        # proxy that the independent scan excludes by design.
        return notes + self._account(journal, _journal_port(journal)) + self._release(store, journal)

    def _holding(self, store: session.Session, lock_fd: int, journal) -> list[str]:
        """`Active` / `Acquiring(ref)`: fence, then observe and decide *again*, and act on the
        second decision only — exactly `down`'s row."""
        port = journal.owner.control_port
        first = ownership.decide_down(self._observe(journal))
        if isinstance(first, (ownership.Preserve, ownership.Refuse)):
            return [f"nothing was written: {first.reason}"]

        watchdog = journal.phase.watchdog if isinstance(journal.phase, ownership.Active) else None
        if watchdog is not None:
            # Not proven gone is not done: the watchdog would repair its own residue, and both
            # `down_session` attempts preserved on exactly this.
            stopped = procs.terminate(watchdog, "watchdog", port)
            if stopped is procs.Termination.STILL_RUNNING:
                return [f"AND FAILED: the watchdog (pid {watchdog.pid}) would not stop; nothing was written"]

        second = ownership.decide_down(self._observe(journal))
        if isinstance(second, (ownership.Preserve, ownership.Refuse)):
            # Nothing signalled either: a proxy stopped over an unreadable second read would leave
            # an `Active` journal, a PAC possibly enabled at a dead port, and no watchdog.
            return [f"the watchdog was stopped; nothing else was written or signalled: {second.reason}"]

        notes: list[str] = []
        if isinstance(second, ownership.Archive):
            # A foreign PAC or a gone service is never overwritten. The recorded processes are
            # still stopped — that evidence is what makes stopping them safe.
            path = store.archive_record(_now(), second.reason, journal)
            notes.append(f"the PAC was {second.reason}; this run's baseline is at {path}")
            record = ownership.Archived(
                version=ownership.SESSION_VERSION,
                since=_now(),
                reason=second.reason,
                path=str(path),
                context=ownership.Known(journal.owner, journal.service, journal.phase.proxy),
            )
            try:
                store.write(record)
            except (OSError, session.Unrepresentable) as error:
                notes.append(f"AND FAILED: the archive record could not be written ({error!r}); the journal is kept")
                return notes + self._account(journal, port)
            return notes + self._account(journal, port) + self._release(store, record)

        if isinstance(second, ownership.Restore):
            self._write_the_baseline(journal, second.target, lock_fd)
            notes.append("the previous settings were put back")
        else:
            notes.append("the previous settings were already in place")
        checkpoint = ownership.SessionRecord(
            version=journal.version,
            since=journal.since,
            simulator=journal.simulator,
            owner=journal.owner,
            service=journal.service,
            baseline=journal.baseline,
            phase=ownership.Restored(journal.phase.proxy),
        )
        try:
            store.write(checkpoint)
        except (OSError, session.Unrepresentable) as error:
            return notes + [f"AND FAILED: the checkpoint could not be written ({error!r}); the journal is kept"]
        return notes + self._account(journal, port) + self._release(store, checkpoint)

    def _write_the_baseline(self, journal, target, lock_fd: int) -> None:
        """§4's recipe by class and target, against the *journal's* baseline — never the preflight
        snapshot — with a read-back that must satisfy the target."""
        name = netproxy.resolved_name(journal.service)
        found = netproxy.pac_status(name)
        cls = ownership.classify(found, journal.owner.control_port, journal.baseline)
        if ownership.satisfies(cls, target):
            return
        if isinstance(target, ownership.Off):
            netproxy.write_pac_state(netproxy.resolved_name(journal.service), False, lock_fd=lock_fd)
        elif cls is ownership.PacClass.RESUMABLE:
            netproxy.write_pac_state(netproxy.resolved_name(journal.service), target.enabled, lock_fd=lock_fd)
        else:
            # `-setautoproxyurl` switches the PAC on as a side effect, so the flag follows the URL.
            netproxy.write_pac_url(netproxy.resolved_name(journal.service), target.url, lock_fd=lock_fd)
            if not target.enabled:
                netproxy.write_pac_state(netproxy.resolved_name(journal.service), False, lock_fd=lock_fd)
            elif not netproxy.pac_status(netproxy.resolved_name(journal.service)).enabled:
                netproxy.write_pac_state(netproxy.resolved_name(journal.service), True, lock_fd=lock_fd)
        back = netproxy.pac_status(netproxy.resolved_name(journal.service))
        if not ownership.satisfies(ownership.classify(back, journal.owner.control_port, journal.baseline), target):
            raise netproxy.UnexpectedPac(f"the PAC did not restore (wanted {target}, now: {back})")

    def _observe(self, journal) -> ownership.DownObs:
        """The same facts the executor gathers, through the same modules."""
        port = journal.owner.control_port
        try:
            service: ownership.Service = netproxy.resolve(journal.service)
        except netproxy.NetworkSetupError as error:
            service = ownership.ServiceFailed(str(error))
        pac: ownership.PacClass | ownership.NotObserved = ownership.NotObserved()
        if isinstance(service, ownership.Present):
            try:
                observed: ownership.Observed = netproxy.pac_status(service.name)
            except netproxy.NetworkSetupError as error:
                observed = ownership.PacUnreadable(str(error))
            pac = ownership.classify(observed, port, journal.baseline)
        proxy = journal.phase.proxy
        watchdog = journal.phase.watchdog if isinstance(journal.phase, ownership.Active) else None
        return ownership.DownObs(
            journal=journal,
            service=service,
            pac=pac,
            health=api_observe(port),
            proxy=procs.liveness(proxy, "proxy", port) if proxy else ownership.NotObserved(),
            watchdog=procs.liveness(watchdog, "watchdog", port) if watchdog else ownership.NotObserved(),
            scan=procs.scan_marked(),
        )

    def _account(self, journal, port: int | None) -> list[str]:
        """Stop the refs the record names, through their *persisted* identity. UNKNOWN is preserved
        and reported: a fresh scan ref never re-authorises signalling a pid nothing proved."""
        notes: list[str] = []
        proxy = _recorded_proxy(journal)
        if proxy is None or port is None:
            return notes
        for ref, marker in ((proxy, "proxy"),):
            state = procs.liveness(ref, marker, port)
            if state is ownership.Liveness.UNKNOWN:
                notes.append(f"the recorded {marker} (pid {ref.pid}) could not be identified; it was left running")
                continue
            if state is ownership.Liveness.PROVEN_DEAD:
                continue
            try:
                if procs.terminate(ref, marker, port) is procs.Termination.STILL_RUNNING:
                    notes.append(f"the recorded {marker} (pid {ref.pid}) is still running")
            except procs.ProcessCheckError as error:
                notes.append(f"the recorded {marker} (pid {ref.pid}) could not be stopped: {error}")
        return notes

    def _release(self, store: session.Session, journal) -> list[str]:
        """Unlink only when `decide_release` says every obligation is closed."""
        port = _journal_port(journal)
        proxy = _recorded_proxy(journal)
        decision = ownership.decide_release(
            ownership.ReleaseObs(
                journal=journal,
                proxy=procs.liveness(proxy, "proxy", port) if proxy and port else ownership.NotObserved(),
                watchdog=ownership.NotObserved(),
                health=api_observe(port) if port else ownership.NotObserved(),
                sweep=ownership.NotObserved(),
                scan=procs.scan_marked(),
            )
        )
        if isinstance(decision, ownership.ReleaseNow):
            store.unlink()
            return []
        return [f"the session record is kept for `lyrebird down`: {decision.reason}"]

    def _stop_our_proxies(self) -> list[str]:
        """Stop any proxy still running *for this run*, behind the same owner gate and under the
        same lock.

        Identity is the marked argv, not a pid somebody wrote down: this run's control port is a
        free port unique to it, and `--set confdir=<state>/mitmproxy` names a temporary directory
        nothing else has ever been given. §5's exclusion applies first — a pid the journal names is
        left to the teardown while its persisted identity is alive or unknown, because a fresh scan
        ref must never re-authorise signalling it — and everything else is signalled only through
        `procs.terminate` with the `Ref` captured at discovery, so a proxy that exits during the
        grace wait and whose pid is reused receives nothing.

        An incomplete scan, an unidentifiable process or one that would not stop is a *reported
        cleanup failure*, never "nothing to stop".
        """
        problems: list[str] = []
        store = session.Session()
        try:
            store.ensure_root()
            with store.locked(timeout=LOCK_WAIT):
                journal = store.read()
                gate = self._owner_gate(journal)
                if gate is not None and not isinstance(journal, ownership.Absent):
                    return [f"the process cleanup was skipped: {gate}"]
                excluded = _excluded(journal)
                scan = procs.scan_marked()
                if isinstance(scan, ownership.Incomplete):
                    return ["the process table could not be read whole, so nothing here can say what is left"]
                for marked in scan.found:
                    if marked.ref.pid in excluded or not self._is_ours(marked):
                        continue
                    problems.append(self._stop(marked))
        except (OSError, session.LockBusy) as error:
            return [f"the process cleanup could not take the session lock ({error!r})"]
        return [problem for problem in problems if problem]

    def _is_ours(self, marked: ownership.Marked) -> bool:
        confdir = f"confdir={Path(self.state).resolve() / 'mitmproxy'}"
        return marked.port == int(self.control_port) and confdir in marked.cmdline

    def _stop(self, marked: ownership.Marked) -> str:
        try:
            outcome = procs.terminate(marked.ref, marked.kind, marked.port)
        except procs.ProcessCheckError as error:
            return f"a {marked.kind} of this run (pid {marked.ref.pid}) could not be identified or stopped: {error}"
        if outcome is procs.Termination.NOT_RUNNING:
            return ""
        if outcome is procs.Termination.STILL_RUNNING:
            return f"a {marked.kind} of this run (pid {marked.ref.pid}) would not stop"
        return f"a {marked.kind} this run started was still running after the teardown (pid {marked.ref.pid})"


def _journal_port(journal) -> int | None:
    if isinstance(journal, ownership.SessionRecord):
        return journal.owner.control_port
    if isinstance(journal, ownership.Archived) and isinstance(journal.context, ownership.Known):
        return journal.context.owner.control_port
    return None


def _recorded_proxy(journal):
    if isinstance(journal, ownership.SessionRecord):
        return getattr(journal.phase, "proxy", None)
    if isinstance(journal, ownership.Archived) and isinstance(journal.context, ownership.Known):
        return journal.context.proxy
    return None


def _excluded(journal) -> set[int]:
    """Pids the journal names whose persisted identity is alive or unknown: those belong to the
    teardown, and a fresh scan ref must not re-authorise signalling them."""
    if isinstance(journal, ownership.SessionRecord):
        port = journal.owner.control_port
        refs = [(getattr(journal.phase, "proxy", None), "proxy")]
        if isinstance(journal.phase, ownership.Active):
            refs.append((journal.phase.watchdog, "watchdog"))
    elif isinstance(journal, ownership.Archived) and isinstance(journal.context, ownership.Known):
        port, refs = journal.context.owner.control_port, [(journal.context.proxy, "proxy")]
    else:
        return set()
    return {
        ref.pid
        for ref, marker in refs
        if ref is not None and procs.liveness(ref, marker, port) is not ownership.Liveness.PROVEN_DEAD
    }


def _not_ours(owner: ownership.Owner) -> str:
    return (
        f"a Lyrebird session on port {owner.control_port} (profile {owner.profile_fingerprint}) owns the PAC "
        f"and it is not this run's: nothing was written — run `lyrebird down` if it is yours"
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def api_observe(port: int) -> ownership.Health:
    """The health reading, through the engine's own transport — the acceptance conftest lifts the
    fast suite's guard on it, because these reads go to the proxy this run started."""
    return api.observe_health(port).health


@pytest.fixture(scope="session")
def harness(tmp_path_factory: pytest.TempPathFactory, simulator: str, fixture_app: Path) -> Iterator[Harness]:
    """A temporary profile, a temporary state directory, free ports — and the promise that the
    Mac's proxy settings are what they were when this returns.

    The cleanup runs on the way out of this fixture, which is every way out of a run: `_interruptible`
    turns a terminating signal into the `KeyboardInterrupt` pytest already unwinds. It cannot
    survive `kill -9` (nothing can) — CONTRIBUTING.md says what that leaves and how to undo it.
    """
    root = tmp_path_factory.mktemp("lyrebird-acceptance")
    profile, state = root / "profile", root / "state"
    control_port, proxy_port = _free_ports(2)

    env = {
        **os.environ,
        "LYREBIRD_STATE_DIR": str(state),
        "LYREBIRD_CONTROL_PORT": str(control_port),
        "LYREBIRD_PROXY_PORT": str(proxy_port),
        # Explicit, not inherited: a developer who set these to a LAN address for their own use
        # would otherwise have this run publish a proxy — and a PAC advertising it — off the
        # loopback the whole design assumes.
        "LYREBIRD_PROXY_LISTEN_HOST": "127.0.0.1",
        "LYREBIRD_PROXY_ADVERTISED_HOST": "127.0.0.1",
    }
    # Explicit everywhere, so an inherited LYREBIRD_PROFILE cannot decide which profile is meant.
    env.pop("LYREBIRD_PROFILE", None)

    world = Harness(simulator, fixture_app, profile, state, env)

    created = _run_grouped([str(LYREBIRD), "init", str(profile)], env, timeout=120)
    if created.returncode != 0:
        _fail("`lyrebird init` could not create the acceptance profile", created)

    # The profile is the one `init` writes, edited the way its own output tells a new user to edit
    # it — not a committed copy. Nothing profile-shaped lives in this repository outside
    # `engine/examples`, and the host is asserted rather than assumed: the fixture app's URL is
    # compiled in, so a bundled profile that stopped intercepting it would make every check below
    # fail as "the app never reached the proxy".
    profile_file = profile / "profile.json"
    document = json.loads(profile_file.read_text(encoding="utf-8"))
    if FIXTURE_HOST not in document.get("hosts", []):
        pytest.fail(
            f"`lyrebird init` wrote a profile that does not intercept {FIXTURE_HOST}, "
            f"which is the host the fixture app calls: {document.get('hosts')}\n"
            f"  fix acceptance/FixtureApp/FixtureApp/FixtureApp.swift and this constant "
            f"together, or restore the host in engine/examples/profile.json"
        )
    document["simBundleId"] = BUNDLE_ID
    profile_file.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    for scenario in sorted(FIXTURE_SCENARIOS.glob("*.json")):
        shutil.copy(scenario, profile / "scenarios" / scenario.name)

    # The session journal lives at one fixed per-user path that no environment variable moves,
    # so a session already there belongs to somebody: this run would acquire over it, record its
    # PAC as the thing to restore, and release the record it was holding.
    existing = session.Session().read()
    if not isinstance(existing, ownership.Absent):
        pytest.fail(
            f"a Lyrebird session exists for this user ({session.Session().journal_path}): "
            f"{ownership.phase_word(existing)}\n  run `lyrebird down` first"
        )

    world.service = _active_service()
    if not world.service:
        pytest.fail(
            "no active network service — macOS has nothing to install a PAC on, so nothing here could be intercepted"
        )
    world.baseline = world.pac()
    if world.baseline.enabled and LYREBIRD_PAC.match(world.baseline.url):
        # An *enabled* Lyrebird PAC means another run is live on this machine. Starting now
        # would record its PAC as the thing to restore, hand it back at the end with this run's
        # idea of whose it was, and leave that run pointing at a proxy this run stopped. A
        # disabled one is only the URL a previous `down` left behind, and is restored verbatim.
        pytest.fail(
            f"a Lyrebird PAC is already enabled on '{world.service}': "
            f"{world.baseline.describe()}\n  run `lyrebird down` for that profile first"
        )

    try:
        yield world
    finally:
        # `clean_up` raises only the interrupt it deferred until the network was back, and that is
        # allowed through: pytest unwinds the remaining finalizers on it, which is how the app is
        # uninstalled and the simulator shut down.
        problems = world.clean_up()
        if problems:
            pytest.fail("\n\n".join(problems))


# MARK: - The isolation the fast suite needs, and this one must not have
#
# The root `tests/conftest.py` collects these checks too, so its autouse fixtures apply here unless
# a nested conftest overrides them by name. Each of the three below is exactly that: an acceptance
# run *is* the real per-user session root, the real process table and the real control port — its
# subprocess `up` writes the journal this run then reads back, and a doubled psutil or a refused
# transport would make every assertion about a real machine meaningless.


@pytest.fixture(autouse=True)
def _no_real_session_root():
    """Overrides the fast suite's temporary root: an acceptance `up` runs in another process, which
    inherits no monkeypatch, and the harness has to read the file that process wrote."""
    return None


@pytest.fixture(autouse=True)
def _no_real_psutil():
    """The processes here are real, and so is the table they are found in."""
    return None


@pytest.fixture(autouse=True)
def _no_real_control_transport():
    """The harness's own in-process health reads go to the proxy this run started."""
    return None


@pytest.fixture(autouse=True)
def _no_real_watchdog():
    """The watchdog here is the shipped one, started by the `up` this check runs — and it is the
    thing the watchdog phase exists to exercise."""
    return None
