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

**The cleanup is not allowed to depend on the thing under test.** `lyrebird down` is what runs
first, because putting the network back is its job and this is where that job is checked. But a
`down` that fails, hangs, or half-finishes is exactly the regression worth catching, and it must
not also be the reason a developer's Mac is left routed at a dead port. So the settings are read
back from `networksetup` directly, and if they are not the ones recorded before anything started,
this kills the proxy this run started, gives the watchdog its chance, and failing that restores the
baseline itself — reporting both the original failure and the fact that it had to.

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
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[3]
LYREBIRD = REPO / "bin" / "lyrebird"
FIXTURE_APP_DIR = REPO / "acceptance" / "FixtureApp"
FIXTURE_SESSIONS = REPO / "acceptance" / "fixture-sessions"
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
    the runtime file names them for the cleanup to find. Killing them here would make every `up`
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive and somebody else's
    return True


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


def _active_service() -> str | None:
    """The network service carrying the default route, resolved the way macOS reports it.

    Deliberately not `lyrebird status --json`, and deliberately not an import of `netproxy`:
    `status` answers with the service the *runtime record* names (engine/cli.py), so once `up` has
    run it echoes back the value this harness recorded and a guard built on it compares a number
    with itself. This is the second opinion — the same two questions the engine asks the OS, asked
    here, so a route that moved is seen whatever Lyrebird believes.
    """
    route = _try_run(["route", "-n", "get", "default"], timeout=30)
    if route is None:
        return None
    interface = re.search(r"interface:\s*(\S+)", route.stdout)
    if not interface:
        return None
    order = _try_run(["networksetup", "-listnetworkserviceorder"], timeout=60)
    if order is None:
        return None
    for name, device in re.findall(r"\(\d+\)\s*(.+?)\n\(Hardware Port:.*?Device:\s*(\w+)\)", order.stdout):
        if device == interface.group(1):
            return name.strip()
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
                    f"this run booted {device['name']} ({udid}) and could not shut it "
                    f"down again: {stopped.stderr if stopped else 'simctl did not run'}"
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
        pytest.skip("xcodegen is not installed (`brew install xcodegen`)")

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
                f"could not uninstall {BUNDLE_ID} from {simulator}: "
                f"{removed.stderr if removed else 'simctl did not run'}"
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
        self.service: str | None = None
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

        Asked of macOS rather than of `status --json`, which reports the service the runtime record
        names and would therefore agree with this run by construction.
        """
        print(f"— phase: {name}", flush=True)
        now = _active_service()
        if now != self.service:
            pytest.fail(
                f"the active network service changed from '{self.service}' to '{now}' "
                f"before the phase '{name}'. Stopping: a PAC installed on '{now}' is not "
                f"one this run recorded a baseline for. '{self.service}' is restored on "
                f"the way out; check '{now}' by hand if an earlier phase reached it."
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

    def runtime(self) -> dict:
        path = self.state / f"runtime-{self.control_port}.json"
        if not path.is_file():
            return {}
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return recorded if isinstance(recorded, dict) else {}

    # MARK: - The network, read from macOS rather than from Lyrebird

    def _read_pac(self) -> Pac | None:
        """None when macOS could not be asked — which is not the same as "there is no PAC", and is
        why the cleanup treats it as a failure to verify rather than as a clean network."""
        if self.service is None:
            return None
        result = _try_run(["networksetup", "-getautoproxyurl", self.service], timeout=60)
        if result is None or result.returncode != 0:
            return None
        url = re.search(r"URL:\s*(\S+)", result.stdout)
        found = url.group(1) if url else ""
        return Pac("" if found.lower() == "(null)" else found, "Enabled: Yes" in result.stdout)

    def pac(self) -> Pac:
        now = self._read_pac()
        if now is None:
            pytest.fail(f"could not read the PAC on '{self.service}'")
        return now

    def pac_is_ours(self) -> bool:
        """Enabled, and pointing at *this* run's control port — not merely "different from before"."""
        now = self._read_pac()
        return now is not None and now.enabled and now.url == self.our_pac_url

    def restored(self, now: Pac | None = None) -> bool:
        """Are the settings back to what this run found?

        Not a string comparison, for two reasons that both come from `netproxy.restore_pac`:

        * it normalises `enabled` to `bool(url) and enabled`, so a baseline that was somehow
          enabled with no URL is correctly restored *disabled* — requiring the recorded flag back
          would reject a correct restore;
        * macOS rejects an empty PAC URL, so with no URL to put back it can only switch the PAC
          off, leaving the URL Lyrebird installed in the field. Disabled-with-our-URL is therefore
          the restored state on a machine that started with none — the same state an ordinary
          `lyrebird down` leaves.

        The exception is that narrow: with an empty baseline the URL left behind must be empty or
        this run's own, and with any other baseline the URL must come back exactly.
        """
        now = now if now is not None else self._read_pac()
        if now is None:
            return False  # unread is not restored
        expected_enabled = self.baseline.enabled and bool(self.baseline.url)
        if now.enabled != expected_enabled:
            return False
        if self.baseline.url:
            return now.url == self.baseline.url
        return now.url in ("", self.our_pac_url)

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
        """`lyrebird down`, then macOS's own account of whether that worked.

        `down` goes first, because restoring is its job and this is where that job is checked. What
        follows does not trust it: the settings are read back from `networksetup`, and if they are
        wrong this puts them back itself rather than leaving the Mac routed at a port whose proxy it
        just stopped. Every step it had to take is returned, so a run whose cleanup was performed
        for it fails loudly instead of looking tidy.
        """
        problems: list[str] = []
        try:
            stopped = self._attempt_down()
            if stopped is None or stopped.returncode != 0:
                # One retry, then report. `down` fails most often because `networksetup` failed for
                # a moment during a network change, and a second call is the recovery available
                # inside the tool this check exists to exercise.
                stopped = self._attempt_down()
            if stopped is None:
                problems.append(f"`lyrebird down` did not complete within {DOWN_TIMEOUT}s or could not be started")
            elif stopped.returncode != 0:
                problems.append(f"`lyrebird down` exited {stopped.returncode}:\n{stopped.stdout}\n{stopped.stderr}")

            # Unconditional, and not driven by the runtime file. `up` starts mitmdump detached and
            # records its pid only once it is healthy and the CA is trusted (engine/cli.py), so an
            # interrupt in that window leaves a proxy that `down` cannot find — no runtime record,
            # and no health yet to rediscover it from — and no PAC to make it obvious either, since
            # a run interrupted there never installed one. Asking the process table instead finds
            # it whatever Lyrebird managed to write down.
            problems.extend(self._stop_our_proxies())

            if not self.restored():
                problems.append(
                    f"the Mac's auto-proxy settings were not restored on "
                    f"'{self.service}' by `lyrebird down`.\n"
                    f"  before: {self.baseline.describe()}\n"
                    f"  after:  {self._describe_now()}"
                )
                problems.extend(self._restore_by_hand())
        except BaseException as unexpected:  # noqa: BLE001 - a finalizer that raises cleans nothing
            # Not the deferred signals — those cannot arrive here any more. Anything else that goes
            # wrong is reported rather than raised, so the caller still hears about the network.
            problems.append(
                f"the cleanup itself failed with {unexpected!r}; the network may not "
                f"be restored — check System Settings ▸ Network ▸ {self.service} ▸ "
                f"Proxies"
            )
        return problems

    def _attempt_down(self) -> subprocess.CompletedProcess | None:
        try:
            return self.attempt("down", timeout=DOWN_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None

    def _describe_now(self) -> str:
        now = self._read_pac()
        return now.describe() if now else "(could not be read)"

    def _restore_by_hand(self) -> list[str]:
        """The last resort: put the recorded settings back, once nothing is left to undo it.

        The proxy is already gone — `_stop_our_proxies` runs first — so this is the watchdog's
        window. Its whole job is to restore the PAC when the proxy dies and it holds the same
        recorded baseline, so it is given its chance before anything here touches `networksetup`;
        waiting for it to exit also waits for any `networksetup` it started, which would otherwise
        land on top of this one.
        """
        notes = [
            "this harness restored them itself, because a cleanup that depends on the command "
            "it is checking cannot be relied on to run"
        ]

        deadline = time.time() + WATCHDOG_GRACE
        while not self.restored() and time.time() < deadline:
            time.sleep(1)
        watchdog_pid = self.runtime().get("watchdogPid")
        if isinstance(watchdog_pid, int) and watchdog_pid > 0:
            self._wait_for_exit(watchdog_pid, KILL_WAIT)
        if self.restored():
            notes.append("the watchdog restored them before this had to")
            return notes

        if self.service:
            if self.baseline.url:
                # Set the URL, and prove it took before switching anything on: `-setautoproxyurl`
                # enables the PAC as a side effect, so enabling after a failed set would route the
                # Mac at whatever URL is in the field — which, right now, is ours.
                _try_run(["networksetup", "-setautoproxyurl", self.service, self.baseline.url], timeout=60)
                now = self._read_pac()
                if now is None or now.url != self.baseline.url:
                    _try_run(["networksetup", "-setautoproxystate", self.service, "off"], timeout=60)
                    notes.append(
                        f"AND FAILED: could not put the previous PAC URL back on "
                        f"'{self.service}' (it now reads {self._describe_now()}), so "
                        f"routing was switched off rather than left pointing at ours. "
                        f"Set it by hand: System Settings ▸ Network ▸ {self.service} ▸ "
                        f"Proxies"
                    )
                    return notes
            # macOS rejects an empty URL, so with nothing recorded the only thing to restore is
            # "off" — which is exactly what `restore_pac` does, and leaves our URL in the field.
            _try_run(
                [
                    "networksetup",
                    "-setautoproxystate",
                    self.service,
                    "on" if (self.baseline.enabled and self.baseline.url) else "off",
                ],
                timeout=60,
            )

        if not self.restored():
            notes.append(
                f"AND FAILED: the settings on '{self.service}' are still "
                f"{self._describe_now()}, wanted {self.baseline.describe()}. "
                f"Fix by hand: System Settings ▸ Network ▸ {self.service} ▸ Proxies"
            )
        return notes

    def _stop_our_proxies(self) -> list[str]:
        """Kill any mitmdump still running for this run, and say so.

        Identity comes from the command line, not from a pid somebody wrote down: `up` starts it
        with `--set confdir=<state>/mitmproxy` (engine/cli.py), and this run's state directory is a
        temporary path nothing else has ever been given. A recorded pid can be stale — the file
        outlives the process, and the OS reuses numbers — and, worse, may never have been recorded
        at all.

        A brief wait first: `down` has asked it to stop and it may still be on its way out. Only
        one that outlives that is a problem, and it is reported as one — a proxy that had to be
        killed here is a proxy `down` did not stop.
        """
        problems = []
        for pid in self._our_proxy_pids():
            if self._wait_for_exit(pid, SHUTDOWN_GRACE):
                continue  # `down` had it in hand after all
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
            gone = self._wait_for_exit(pid, KILL_WAIT)
            problems.append(
                f"a proxy this run started was still running after `down` (pid {pid}); "
                f"it was killed here{'' if gone else f', and did not exit within {KILL_WAIT:g}s'}"
            )
        return problems

    def _our_proxy_pids(self) -> list[int]:
        listing = _try_run(["ps", "-axo", "pid=,command="], timeout=30)
        if listing is None or listing.returncode != 0:
            return []
        pids = []
        for line in listing.stdout.splitlines():
            pid, _, command = line.strip().partition(" ")
            if pid.isdigit() and "mitmdump" in command and str(self.state) in command:
                pids.append(int(pid))
        return pids

    @staticmethod
    def _wait_for_exit(pid: int, timeout: float) -> bool:
        """Has the process gone? Polls cheaply, and only asks `ps` if it is about to say no.

        `kill -0` still succeeds on a process that has exited but whose parent has not reaped it,
        so a signalled child would read as running for as long as nobody waited on it. Nothing is
        executing at that point, and reporting it as a proxy that would not die would send someone
        looking for a process that is not there.
        """
        deadline = time.time() + timeout
        while _pid_alive(pid) and time.time() < deadline:
            time.sleep(0.2)
        if not _pid_alive(pid):
            return True
        state = _try_run(["ps", "-o", "state=", "-p", str(pid)], timeout=15)
        return state is not None and state.stdout.strip().upper().startswith("Z")


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

    for session in sorted(FIXTURE_SESSIONS.glob("*.json")):
        shutil.copy(session, profile / "sessions" / session.name)

    world.service = _active_service()
    if not world.service:
        pytest.fail(
            "no active network service — macOS has nothing to install a PAC on, so nothing here could be intercepted"
        )
    world.baseline = world.pac()
    if world.baseline.enabled and LYREBIRD_PAC.match(world.baseline.url):
        # An *enabled* Lyrebird PAC means another session is live on this machine. Starting now
        # would record its PAC as the thing to restore, hand it back at the end with this run's
        # idea of whose it was, and leave that session pointing at a proxy this run stopped. A
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
