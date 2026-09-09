"""Doubles more than one CLI test module leans on: a fake `simctl` and the devices it reports,
a fake proxy the CLI can select scenarios on, the health payloads the commands read, and the `up`
that everything shelling out is stubbed under.

Not named `test_*`, so pytest does not collect it. A double only one module uses lives in that
module.
"""

import json
import subprocess
import urllib.error
import urllib.request

import click

import api
import config
import netproxy
import simulator as sim
import supervisor


def _discovery_times_out():
    """`netproxy.active_service` could not raise until its `route`/`networksetup` calls were given
    a per-command bound. Now it can, and the three commands that ask it — `up`, `down`, `status` —
    each ask outside any handler, so an uncaught error there is a traceback in the one place the
    operator most needs an answer."""
    raise netproxy.NetworkSetupError("`route -n get default` did not finish within 5s")


_CORPORATE = {"url": "http://proxy.example.com/corp.pac", "enabled": False}


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


_LIVE = {"pid": 4321, "activeScenario": "default", "scenarios": ["default"], "overrideCount": 0, "proxyPort": 8080}


def _up_after_a_crash(profile, monkeypatch, pac_status, *, service="Wi-Fi", health=None, stub_trust=True):
    """`up` with the proxy dead, a runtime file the watchdog kept (it could not restore), and the
    PAC in whatever state `pac_status` reports. Everything that shells out is stubbed. `health` is
    the sequence of answers `_health` gives; by default dead at the first look and live after.

    `stub_trust=False` leaves the real `trust_ca_in_sim` in place, for the tests that are about
    *which device* the CA goes to — a stub swallows the simctl call those tests exist to inspect.
    """
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": _CORPORATE})
    answers = iter([None] if health is None else health)

    class Proc:
        pid = 4321

    monkeypatch.setattr(api, "_health", lambda: next(answers, _LIVE))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(supervisor, "_start_fresh_log", lambda: None)
    if stub_trust:
        monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (True, "trusted"))
    # One booted simulator, so device resolution succeeds without reading this machine's. A test
    # about several booted devices calls `fake_simctl` again with its own list.
    fake_simctl(monkeypatch, [_PHONE])
    monkeypatch.setattr(netproxy, "active_service", lambda: service)
    installed = {"ours": False}  # `set_pac` installs ours, and every read after it sees that

    def read(service):
        if installed["ours"]:
            return netproxy.PacStatus(netproxy.pac_url(), True, True)
        return pac_status(service)

    def install(service):
        installed["ours"] = True

    monkeypatch.setattr(netproxy, "pac_status", read)
    monkeypatch.setattr(netproxy, "set_pac", install)
    monkeypatch.setattr(supervisor, "_pid_is_ours", lambda pid, marker: True)
    monkeypatch.setattr(supervisor, "_spawn_watchdog", lambda service: 4242)


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

    monkeypatch.setattr(urllib.request, "urlopen", raise_http)


def _status_network(monkeypatch):
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))


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
        previous = state["active"]
        state["active"] = payload["name"]
        state["steps"][payload["name"]] = 1  # activating a scenario rewinds its sequences
        return {"active": state["active"], "previous": {"name": previous, "overrideCount": 0}}

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


def _live(state):
    reading = {
        **_LIVE,
        "activeScenario": state["active"],
        "scenarios": state["scenarios"],
        "loadProblems": state["loadProblems"],
        "scenariosNotWhole": state["notWhole"],
    }
    if state["notWhole"] is None:  # an engine older than the fields, which cannot say
        del reading["loadProblems"]
        del reading["scenariosNotWhole"]
    return reading


def _up_with_a_proxy(profile, monkeypatch, state, *, adopt=False, bundle_id="com.example.Store"):
    """`up` against `state`'s proxy — either starting it, or adopting one already running."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    sim = f', "simBundleId": "{bundle_id}"' if bundle_id else ""
    (profile / "profile.json").write_text(f'{{"hosts": ["api.example.com"]{sim}}}', encoding="utf-8")
    # Health tracks the fake proxy, so the last look reports the scenario that was actually selected.
    first = iter([] if adopt else [None])  # dead at the first look unless we are adopting one
    monkeypatch.setattr(api, "_health", lambda: next(first, _live(state)))
