"""Doubles more than one CLI test module leans on.

Three of them carry the whole of the switch's world, and each can inject a failure or a death at
the nth call — the shape a named example test needs to pin one window:

* `FakeNetwork` replaces `netproxy._run` wholesale, so every *parser* under test is the real one
  and only `networksetup` itself is invented;
* `FakePsutil` replaces the module attribute `procs.psutil`, so the adapter's own error
  normalisation is what runs;
* `FakeHealth` replaces `api._open` — the one call every control request goes through — so a test
  injects a transport answer and the real reader decodes it.

Not named `test_*`, so pytest does not collect it. A double only one module uses lives in that
module.
"""

import json
import subprocess
import urllib.error
import urllib.parse

import click
import psutil

import api
import config
import netproxy
import ownership
import procs
import session
import simulator as sim
import supervisor
from ownership import Pac, ServiceRef

# MARK: - the process table


class FakeProc:
    """One process as the fake table knows it.

    `errors` raises on a named call (`status`, `create_time`, `cmdline`, `terminate`,
    `kill`, `wait`), `waits` is the sequence `wait()` answers with, and `on_terminate`/`on_kill`
    let a test say what the signal did — the real psutil exception classes throughout, because
    `procs` catches those and nothing else.
    """

    def __init__(
        self,
        *,
        create_time=1000.5,
        cmdline=(),
        status=psutil.STATUS_RUNNING,
        errors=None,
        waits=(),
        on_terminate=None,
        on_kill=None,
        gone=False,
        dies=False,
    ):
        self.create_time = create_time
        self.cmdline = list(cmdline)
        self.status = status
        self.errors = dict(errors or {})
        self.waits = list(waits)
        self.on_terminate = on_terminate
        self.on_kill = on_kill
        self.gone = gone
        # A process that dies when it is asked to, which is what an ordinary child does. Off by
        # default so an adapter test can say "SIGTERM was ignored" by saying nothing.
        self.dies = dies


class _FakeProcess:
    def __init__(self, table, pid, spec):
        self.table = table
        self.pid = pid
        self.spec = spec

    def _check(self, call):
        error = self.spec.errors.get(call)
        if error is not None:
            raise error
        if self.spec.gone:
            raise psutil.NoSuchProcess(self.pid)

    def status(self):
        self._check("status")
        return self.spec.status

    def create_time(self):
        self._check("create_time")
        return self.spec.create_time

    def cmdline(self):
        self._check("cmdline")
        if self.spec.status == psutil.STATUS_ZOMBIE:
            raise psutil.ZombieProcess(self.pid)  # as macOS answers for a zombie
        return list(self.spec.cmdline)

    def terminate(self):
        self._check("terminate")
        self.table.signalled.append((self.pid, "terminate"))
        if self.spec.on_terminate is not None:
            self.spec.on_terminate(self.spec)
        elif self.spec.dies:
            self.spec.gone = True

    def kill(self):
        self._check("kill")
        self.table.signalled.append((self.pid, "kill"))
        if self.spec.on_kill is not None:
            self.spec.on_kill(self.spec)
        elif self.spec.dies:
            self.spec.gone = True

    def wait(self, timeout=None):
        self._check("wait")
        outcome = self.spec.waits.pop(0) if self.spec.waits else 0
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def proxy_argv(port, *, confdir="/tmp/lyrebird-tests/mitmproxy"):
    """A proxy's argv as `up` builds it — exactly the tokens `procs.is_proxy_on` reads."""
    return [
        "/opt/lyrebird/.venv/bin/mitmdump",
        "--set",
        f"lyrebird_control_port={port}",
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        "8080",
        "--set",
        f"confdir={confdir}",
        "-s",
        "/opt/lyrebird/engine/addon.py",
    ]


class FakePsutil:
    """The process table `procs` sees. One `monkeypatch.setattr(procs, "psutil", fake)` is the
    whole seam, so the adapter's own error normalisation is what is under test rather than a
    double's idea of it. Counts constructions, so a test can prove inspection and signal went
    through one instance."""

    Error = psutil.Error
    NoSuchProcess = psutil.NoSuchProcess
    ZombieProcess = psutil.ZombieProcess
    AccessDenied = psutil.AccessDenied
    TimeoutExpired = psutil.TimeoutExpired
    STATUS_ZOMBIE = psutil.STATUS_ZOMBIE
    STATUS_RUNNING = psutil.STATUS_RUNNING

    def __init__(self, processes=None):
        self.processes = dict(processes or {})
        self.constructed = []
        self.signalled = []
        self._next_pid = 5000

    def Process(self, pid):  # noqa: N802 — the name psutil uses, which `procs` calls
        self.constructed.append(pid)
        spec = self.processes.get(pid)
        if spec is None:
            raise psutil.NoSuchProcess(pid)
        construct = spec.errors.get("construct")
        if construct is not None:
            raise construct
        return _FakeProcess(self, pid, spec)

    # MARK: - what the CLI tests put in it

    def spawn(self, port, *, create_time=None, pid=None, confdir=None):
        """Register a marked Lyrebird proxy, and return its pid."""
        if pid is None:
            pid = self._next_pid
            self._next_pid += 1
        self.processes[pid] = FakeProc(
            cmdline=proxy_argv(port, **({"confdir": confdir} if confdir else {})),
            create_time=1000.0 + pid if create_time is None else create_time,
            dies=True,
        )
        return pid

    def ref(self, pid):
        return ownership.Ref(pid=pid, create_time=self.processes[pid].create_time)

    def spawn_ref(self, port, **kwargs):
        return self.ref(self.spawn(port, **kwargs))

    def marked_pid(self, port):
        """The live pid of the marked proxy on this port, or None."""
        for pid, spec in self.processes.items():
            if spec.gone:
                continue
            if procs.is_proxy_on(list(spec.cmdline), port):
                return pid
        return None

    def alive(self, pid):
        spec = self.processes.get(pid)
        return spec is not None and not spec.gone


class FakePopen:
    """What `subprocess.Popen` hands back: a pid, and the exit check `up` polls."""

    def __init__(self, pid, table=None):
        self.pid = pid
        self._table = table

    def poll(self):
        spec = None if self._table is None else self._table.processes.get(self.pid)
        return None if spec is None or not spec.gone else 1


def spawning_proxy(monkeypatch, table, *, port=None, raises=None, pid=None, dead=False):
    """`subprocess.Popen` for the proxy `up` starts: registers a marked pid in `table`.

    `dead=True` is the child that exits the instant it is started — a malformed profile, a port
    already bound — which is the case `up` has to report with the log's last lines.
    """
    started = []

    def popen(argv, **kwargs):
        if raises is not None:
            raise raises
        child = table.spawn(config.CONTROL_PORT if port is None else port, pid=pid)
        if dead:
            table.processes[child].gone = True
        started.append(child)
        return FakePopen(child, table)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return started


# MARK: - the network


_LEGEND = "An asterisk (*) denotes that a network service is disabled."


class FakeNetwork:
    """Every `networksetup`/`route` command, rendered as the real ones answer.

    It replaces `netproxy._run`, so the parsers, the recipes and the read-backs under test are the
    production ones. Failure is injected by *call number*, which is what lets a test land a foreign
    write inside a window that has no other name.
    """

    def __init__(self, services=None, route="en0"):
        given = services or {"Wi-Fi": ("en0", Pac("", False))}
        self.services = {name: [device, pac] for name, (device, pac) in given.items()}
        self.route = route
        self.calls = []
        self.disabled = set()
        self._fail_at = {}
        self._die_at = {}
        self._write_at = {}
        self._inert_at = set()
        self._inert_all = False

    # MARK: - setup

    def install(self, monkeypatch):
        monkeypatch.setattr(netproxy, "_run", self._run)
        return self

    def pac(self, name):
        return self.services[name][1]

    def set_pac(self, name, pac):
        self.services[name][1] = pac

    def ref(self, name):
        return ServiceRef(name=name, device=self.services[name][0])

    def rename(self, name, to):
        self.services[to] = self.services.pop(name)

    def fail_at(self, call, detail="networksetup: command failed"):
        """The nth command exits non-zero, which `check=True` turns into a NetworkSetupError."""
        self._fail_at[call] = detail
        return self

    def die_at(self, call, detail=None):
        """The nth command does not answer at all."""
        self._die_at[call] = detail or "did not finish within 5s"
        return self

    def write_at(self, call, name, pac):
        """Somebody else's PAC edit lands immediately *before* the nth command runs."""
        self._write_at.setdefault(call, []).append((name, pac))
        return self

    def inert_at(self, call):
        """The nth setter exits 0 and changes nothing — `networksetup` really does this."""
        self._inert_at.add(call)
        return self

    def inert_setters(self):
        """*Every* setter exits 0 and changes nothing, for the tests about what a read-back is for
        rather than about which command number it lands on."""
        self._inert_all = True
        return self

    # MARK: - the commands

    def _run(self, args, check=False):
        args = list(args)
        self.calls.append(args)
        call = len(self.calls)
        for name, pac in self._write_at.get(call, ()):
            self.services[name][1] = pac
        if call in self._die_at:
            raise netproxy.NetworkSetupError(f"`{' '.join(args)}` {self._die_at[call]}")
        if call in self._fail_at:
            result = subprocess.CompletedProcess(args, 1, "", self._fail_at[call])
            if check:
                raise netproxy.NetworkSetupError(f"`{' '.join(args)}` failed: {self._fail_at[call]}")
            return result
        out = self._render(args, inert=self._inert_all or call in self._inert_at)
        if isinstance(out, subprocess.CompletedProcess):
            return out
        if check and out is None:
            raise netproxy.NetworkSetupError(f"`{' '.join(args)}` failed: no such service")
        return subprocess.CompletedProcess(args, 0 if out is not None else 1, out or "", "")

    def _render(self, args, *, inert):
        if args[:2] == ["route", "-n"]:
            return self._route(args)
        option = args[1] if len(args) > 1 else ""
        if option == "-listnetworkserviceorder":
            return self._order()
        if option == "-getautoproxyurl":
            return self._get(args[2])
        if option == "-setautoproxyurl":
            return self._set_url(args[2], args[3], inert=inert)
        if option == "-setautoproxystate":
            return self._set_state(args[2], args[3] == "on", inert=inert)
        raise AssertionError(f"a test reached an unexpected network command: {args}")

    def _route(self, args):
        if self.route is None:
            # `route` exits 0 and says so on stderr: the one output that means "no default route".
            return subprocess.CompletedProcess(args, 0, "", "   route: writing to routing socket: not in table\n")
        return f"   route to: default\n   interface: {self.route}\n   flags: <UP,GATEWAY,DONE>\n"

    def _order(self):
        lines = [_LEGEND, ""]
        for index, (name, (device, _)) in enumerate(self.services.items(), start=1):
            marker = "*" if name in self.disabled else str(index)
            lines.append(f"({marker}) {name}")
            lines.append(f"(Hardware Port: {name}, Device: {device})")
            lines.append("")
        return "\n".join(lines)

    def _get(self, name):
        if name not in self.services:
            return None
        pac = self.services[name][1]
        return f"URL: {pac.url or '(null)'}\nEnabled: {'Yes' if pac.enabled else 'No'}\n"

    def _set_url(self, name, url, *, inert):
        if name not in self.services:
            return None
        if not inert:
            # As macOS does: setting the URL switches the PAC on as a side effect.
            self.services[name][1] = Pac(url=url, enabled=True)
        return ""

    def _set_state(self, name, on, *, inert):
        if name not in self.services:
            return None
        if not inert:
            self.services[name][1] = Pac(url=self.services[name][1].url, enabled=on)
        return ""

    # MARK: - what a test asks it afterwards

    def commands(self, *options):
        """Every command whose option is one of these, in order."""
        return [call for call in self.calls if len(call) > 1 and call[1] in options]

    def setters(self):
        return self.commands("-setautoproxyurl", "-setautoproxystate")


# MARK: - health


def _health_payload(**extra):
    """The envelope every health response carries, so a double cannot pin a shape the API never
    sends — and so a command that starts reading another field fails here rather than passing
    against a payload that omitted it."""
    return {
        "pid": 1,
        "scenarios": ["default"],
        "activeScenario": "default",
        "overrideCount": 1,
        "simBundleId": None,
        "proxyPort": 8080,
        "profileFingerprint": config.PROFILE_FINGERPRINT,
        "sequences": [],
        "answers": [],
        **extra,
    }


class _Response:
    """What `urllib`'s opener hands back, as much of it as `api` reads."""

    def __init__(self, payload):
        self._payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Silent:
    """Nothing answers on the port — the transport raises, as it does for a closed one."""


SILENT = _Silent()


def answering(pid=4321, **extra):
    """An answer from a proxy with this pid."""
    return _Response(_health_payload(pid=pid, **extra))


def body(payload):
    """An answer with exactly this body — for the shapes `_health_payload` cannot express."""
    return _Response(payload)


class FakeHealth:
    """Answers `api._open`, and records the port it was asked about.

    With a `table` and no sequence it answers for whatever proxy that table holds on the port asked
    — which is what makes an `up` test's health follow the proxy it actually started.
    """

    def __init__(self, table=None, *, sequence=None, payload=None):
        self.table = table
        self.sequence = None if sequence is None else list(sequence)
        self.payload = payload if callable(payload) else dict(payload or {})
        self.asked = []

    def install(self, monkeypatch):
        monkeypatch.setattr(api, "_open", self._open)
        return self

    def _open(self, request, timeout=1.5):
        asked = urllib.parse.urlsplit(request.full_url).port or config.CONTROL_PORT
        self.asked.append(asked)
        answer = self._answer(asked)
        if isinstance(answer, _Silent):
            raise urllib.error.URLError("[Errno 61] Connection refused")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _answer(self, asked):
        if self.sequence:
            answer = self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]
            return answer(asked) if callable(answer) else answer
        if self.table is None:
            return SILENT
        pid = self.table.marked_pid(asked)
        extra = self.payload() if callable(self.payload) else self.payload
        return SILENT if pid is None else answering(pid, **extra)


# MARK: - simulators


# Devices with the shape `simctl list devices --json` reports, and nothing that could reach a real
# one: every simctl call goes through `sim._run`, which `fake_simctl` replaces wholesale. The ids
# are short and readable rather than real-looking UUIDs — nothing parses them, they are only
# compared — but they keep upper case, so `--simulator phone-1` still tests a case-insensitive
# match. A device's `runtime` names the bucket it is listed under, which is the only place simctl
# says what platform it is; these are shaped like the real identifiers so the same parse is run.
_IOS_RUNTIME = "com.example.SimRuntime.iOS-18-0"


_PHONE = {"udid": "PHONE-1", "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}


def fake_simctl(monkeypatch, devices, *, list_status=0, keychain_status=0, launch_status=0):
    """Stand in for every `xcrun simctl` call and record what was asked of it.

    A test decides what is booted by setting each device's `state`, and what platform it is by
    setting `runtime` (iOS unless it says otherwise) — no simulator on the machine running the
    tests is read, booted, trusted or launched.
    """
    calls = []

    def run(args):
        assert args[0] == "xcrun", f"unexpected shell-out in a simctl test: {args}"
        calls.append(args)
        if "list" in args:
            # Grouped under runtime identifiers, as simctl groups them: the platform is not a
            # field on the device, it is the bucket the device is listed in.
            buckets: dict = {_IOS_RUNTIME: []}
            for device in devices:
                entry = dict(device)
                buckets.setdefault(entry.pop("runtime", _IOS_RUNTIME), []).append(entry)
            payload = {"devices": buckets}
            return subprocess.CompletedProcess(args, list_status, json.dumps(payload), "")
        if "keychain" in args:
            return subprocess.CompletedProcess(
                args, keychain_status, "", "" if keychain_status == 0 else "keychain failed"
            )
        return subprocess.CompletedProcess(args, launch_status, "", "" if launch_status == 0 else "failed to launch")

    monkeypatch.setattr(sim, "_run", run)
    return calls


# MARK: - journals


WIFI = ServiceRef(name="Wi-Fi", device="en0")
CORPORATE = Pac("http://proxy.example.com/corp.pac", True)


def owner(port=None, fingerprint=None, state_root=None):
    return ownership.Owner(
        control_port=config.CONTROL_PORT if port is None else port,
        profile_fingerprint=config.PROFILE_FINGERPRINT if fingerprint is None else fingerprint,
        state_root=str(config.STATE_ROOT) if state_root is None else state_root,
    )


def record(proxy, *, baseline=None, service=WIFI, owned_by=None, simulator=None):
    return ownership.SessionRecord(
        version=ownership.SESSION_VERSION,
        owner=owned_by or owner(),
        service=service,
        baseline=Pac("", False) if baseline is None else baseline,
        proxy=proxy,
        simulator=simulator,
    )


def write_journal(journal):
    """Put a record in the (monkeypatched) per-user root, the way `up` would."""
    store = session.Session()
    store.ensure_root()
    if journal is not None:
        store.write(journal)
    return store


class World:
    """The three doubles and the journal, installed together — the state one CLI invocation reads."""

    def __init__(self, network, table, health, store):
        self.network = network
        self.table = table
        self.health = health
        self.session = store

    def journal(self):
        return self.session.read()

    def pac(self, name="Wi-Fi"):
        return self.network.pac(name)


def world(monkeypatch, journal, *, services=None, route="en0", table=None, health=None):
    """A machine with this journal, this network and this process table, and nothing real behind
    any of them."""
    table = table if table is not None else FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    network = FakeNetwork(services, route=route).install(monkeypatch)
    reader = (health if health is not None else FakeHealth(table)).install(monkeypatch)
    return World(network, table, reader, write_journal(journal))


def ours(port=None):
    return Pac(ownership.our_url(config.CONTROL_PORT if port is None else port), True)


def ours_off(port=None):
    return Pac(ownership.our_url(config.CONTROL_PORT if port is None else port), False)


# MARK: - `up`


def up_after(monkeypatch, profile, journal, network, table, *, health=None, stub_trust=True, bundle_id=None):
    """`up` over a given journal, with everything that shells out replaced.

    The journal is written through `Session.write` into the monkeypatched root, pids come from
    `table`, and the PAC is state in `network` rather than a callable. Returns the `FakeHealth` in
    force.
    """
    sim_field = f', "simBundleId": "{bundle_id}"' if bundle_id else ""
    (profile / "profile.json").write_text(f'{{"hosts": ["api.example.com"]{sim_field}}}', encoding="utf-8")
    config.reload_profile()
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_journal(journal)
    network.install(monkeypatch)
    monkeypatch.setattr(procs, "psutil", table)
    reader = (health or FakeHealth(table)).install(monkeypatch)
    spawning_proxy(monkeypatch, table)
    monkeypatch.setattr(supervisor, "_start_fresh_log", lambda: None)
    if stub_trust:
        monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (True, "trusted"))
    # One booted simulator, so device resolution succeeds without reading this machine's. A test
    # about several booted devices calls `fake_simctl` again with its own list.
    fake_simctl(monkeypatch, [_PHONE])
    return reader


# MARK: - sequences and answers, shared by the evidence tests


_BASE_SEQ = {
    "id": "ovr_a",
    "runId": "r1",
    "advanceOn": "self",
    "nextStep": 1,
    "stepCount": 2,
    "exhausted": False,
    "hasOverrun": False,
    "serves": {},
}


def _polling(states, build):
    """Live state that moves under the poll: each call serves the next state, the last repeats."""
    queue = list(states)

    def payload():
        return build(queue.pop(0) if len(queue) > 1 else queue[0])

    return payload


def _answers_over(*states):
    """Answer counts under the poll, sharing the envelope with `_health_over`.

    Each state overlays the defaults below; `None` means the rule is gone from the scenario. The
    default `runId` is the one a caller would be holding from `reset`, so a state that means to
    change runs has to say so, and a command that stops reading the field fails here."""
    return _polling(
        states,
        lambda state: _health_payload(
            answers=[] if state is None else [{"id": "ovr_a", "active": True, "count": 0, "runId": "run1", **state}]
        ),
    )


FOREIGN_FINGERPRINT = "deadbeefcafe"


class _JsonBody:
    """An HTTPError's body file. `close` is defined because urllib's error objects are closed."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def close(self):
        pass


def _answers_with_a_conflict(monkeypatch, payload=None):
    """Make every control call fail the way the API refuses a foreign profile."""

    def raise_http(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8088/x",
            409,
            "Conflict",
            {},  # type: ignore[arg-type]
            _JsonBody(
                payload
                if payload is not None
                else {
                    "error": "profile_mismatch",
                    "running": FOREIGN_FINGERPRINT,
                    "requested": config.PROFILE_FINGERPRINT,
                }
            ),
        )

    monkeypatch.setattr(api, "_open", raise_http)


def _status_network(monkeypatch, table=None, *, port=None, service=WIFI):
    """A machine whose PAC is this session's, with a journal that says so — what `status` needs to
    reach exit 0."""
    table = table or FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    control_port = config.CONTROL_PORT if port is None else port
    proxy = table.spawn_ref(control_port)
    network = FakeNetwork({service.name: (service.device, Pac(ownership.our_url(control_port), True))})
    network.install(monkeypatch)
    write_journal(record(proxy, service=service))
    return {"network": network, "table": table, "proxy": proxy}


# MARK: - a proxy the CLI can select scenarios on


_SCENARIOS = {"default": 200, "orders-outage": 500, "checkout/orders-outage": 503}


def _fake_proxy(
    monkeypatch,
    *,
    active="default",
    scenarios=("default", "orders-outage"),
    load_problems=(),
    not_whole=(),
    unreachable=False,
    steps=None,
):
    """A live proxy the CLI can select scenarios on, and an app whose launch makes one request.

    `launched` holds what that request was answered with — the scenario that served it, that
    scenario's status, and the sequence step it got. `events` holds what happened in what order,
    for the cases where nothing is launched at all.

    `load_problems` (the flat strings a person reads) and `not_whole` (the same problems keyed by
    scenario) are set independently, because the reason the second field exists is that the first
    cannot be turned into it. `not_whole=None` is a proxy too old to have either.
    """
    state = {
        "active": active,
        "scenarios": list(scenarios),
        "loadProblems": list(load_problems),
        "notWhole": None if not_whole is None else dict(not_whole),
        "steps": dict(steps or {}),
        "events": [],
        "launched": [],
        "devices": [],
    }

    def control(path, method="GET", payload=None, timeout=3.0):
        assert (path, method) == ("/__mock__/scenarios/active", "PUT"), (path, method)
        state["events"].append(("activate", payload["name"]))
        if unreachable:  # what `_control` prints when the proxy stops answering mid-`up`
            click.echo("✗ proxy not reachable — is it running? (`lyrebird up`)")
            raise SystemExit(1)
        if payload["name"] not in state["scenarios"]:  # the API's 404, with the detail it sends
            click.echo(f"✗ no scenario named '{payload['name']}' in this profile")
            raise SystemExit(1)
        state["active"] = payload["name"]
        state["steps"][payload["name"]] = 1  # activating a scenario rewinds its sequences
        return {"active": state["active"]}

    def relaunch(bundle_id, simulator):
        state["events"].append(("relaunch", bundle_id))
        state["devices"].append(simulator.udid)
        state["launched"].append(
            {
                "scenario": state["active"],
                "status": _SCENARIOS[state["active"]],
                "step": state["steps"].get(state["active"], 1),
            }
        )
        return True, bundle_id

    monkeypatch.setattr(api, "_control", control)
    monkeypatch.setattr(sim, "_relaunch", relaunch)
    return state


def _scenario_payload(state):
    """The scenario fields a live proxy reports, overlaid on the health envelope."""
    payload = {
        "activeScenario": state["active"],
        "scenarios": state["scenarios"],
        "loadProblems": state["loadProblems"],
        "scenariosNotWhole": state["notWhole"],
    }
    if state["notWhole"] is None:  # an engine older than the fields, which cannot say
        del payload["loadProblems"]
        del payload["scenariosNotWhole"]
    return payload


def _up_with_a_proxy(profile, monkeypatch, state, *, bundle_id="com.example.Store"):
    """`up` against `state`'s proxy: it starts it, and the launch meets whatever it selected."""
    table = FakePsutil()
    network = FakeNetwork({"Wi-Fi": ("en0", Pac("", False))})
    health = FakeHealth(table, payload=lambda: _scenario_payload(state))
    up_after(monkeypatch, profile, None, network, table, health=health, bundle_id=bundle_id)
    return {"table": table, "network": network}
