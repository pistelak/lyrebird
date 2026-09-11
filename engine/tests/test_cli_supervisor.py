"""Teardown behaviour.

`down` is the command that has to work when everything else has gone wrong — the proxy crashed,
the runtime file is unreadable, the machine was rebooted mid-session. If it silently does nothing,
the user is left with a PAC pointing at a dead port and no indication why.
"""

import fcntl
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest
from click.testing import CliRunner

import api
import cli
import config
import netproxy
import simulator as sim
import supervisor
from cli_doubles import (
    _CORPORATE,
    _LIVE,
    _PHONE,
    _discovery_times_out,
    _health_payload,
    _status_network,
    _up_after_a_crash,
    fake_simctl,
)


def health_until_terminated(monkeypatch, state, pid=4242):
    """Health answers until the proxy is terminated, then stops — as a real proxy does."""
    monkeypatch.setattr(api, "_health", lambda: None if state["terminated"] else {"pid": pid})


def test_down_does_not_claim_to_restore_from_a_record_it_could_not_read(profile, runner, fake_network, monkeypatch):
    """Two regressions, one file. A corrupt runtime file first made `down` find nothing to do and
    report "stopped" while the proxy was still running and the PAC still pointing at it. Fixed to
    work from health, it then read the corrupt file as *absent*: invented "no previous PAC",
    disabled the routing, deleted the only copy of what to put back and reported the restore a
    success — with a corporate PAC never restored. Now it still stops the proxy and switches ours
    off, but says the previous settings are unknown, keeps the file, and exits 1."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    health_until_terminated(monkeypatch, fake_network)
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert fake_network["restored"] == ("Wi-Fi", "", False), "ours switched off: the Mac must not stay routed here"
    assert (4242, "addon.py") in fake_network["terminated"], "the pid must come from health"
    assert "the previous proxy settings it holds are unknown" in result.output
    assert "restored" not in result.output.replace("unknown", "")
    assert config.runtime_file().read_bytes() == b"not json at all\xff", "the bytes may still hold the previous PAC"


def test_down_uses_the_runtime_file_when_it_is_readable(profile, runner, fake_network, monkeypatch):
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {
            "proxyPid": 99,
            "watchdogPid": 98,
            "service": "Wi-Fi",
            "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": True},
        }
    )
    health_until_terminated(monkeypatch, fake_network, pid=99)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert fake_network["restored"] == ("Wi-Fi", "http://proxy.example.com/corp.pac", True)
    assert (98, "_watchdog") in fake_network["terminated"]
    assert not config.runtime_file().exists()


def test_down_says_so_when_there_is_nothing_to_stop(profile, runner, monkeypatch):
    """Better than claiming success: the user needs to know their PAC was not touched."""
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", lambda: None)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert "nothing to stop" in result.output


def test_down_leaves_a_foreign_pac_alone(profile, runner, monkeypatch):
    """A PAC the user set by hand mid-session must survive teardown."""
    touched = []
    stopped = {"terminated": []}
    monkeypatch.setattr(api, "_health", lambda: None if stopped["terminated"] else {"pid": 1})
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(
        netproxy, "pac_status", lambda service: netproxy.PacStatus("http://proxy.example.com/corp.pac", True, False)
    )
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: touched.append(a))
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)  # pid 1 is a stand-in
    monkeypatch.setattr(
        supervisor,
        "_terminate",
        lambda pid, marker: stopped["terminated"].append(pid) or supervisor.Termination.STOPPED,
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert touched == [], "a PAC that is not ours must never be rewritten"
    assert "not ours" in result.output


def _unreadable_pac(service):
    raise netproxy.NetworkSetupError("`networksetup -getautoproxyurl Wi-Fi` failed: 1")


def test_down_uses_the_recorded_service_when_discovery_times_out(profile, runner, fake_network, monkeypatch):
    """`down` asks the OS only when it has no record of its own, so a wedged `route` must not
    reach this path at all — and the restore must happen on the service the last `up` named."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {
            "proxyPid": 99,
            "watchdogPid": 98,
            "service": "Wi-Fi",
            "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": True},
        }
    )
    health_until_terminated(monkeypatch, fake_network, pid=99)
    monkeypatch.setattr(netproxy, "active_service", _discovery_times_out)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert fake_network["restored"] == ("Wi-Fi", "http://proxy.example.com/corp.pac", True)
    assert (99, "addon.py") in fake_network["terminated"]


def test_down_stops_the_proxy_but_refuses_success_when_discovery_times_out(profile, runner, monkeypatch):
    """With nothing recorded there is no service to restore a PAC *on*, and the read that would
    have found one never answered. `down` used to die here with a traceback, before it had
    terminated anything. It must stop the proxy anyway — and then say that the network was not put
    back, because a restore that could not happen is not a success."""
    stopped = {"terminated": []}
    monkeypatch.setattr(api, "_health", lambda: None if stopped["terminated"] else {"pid": 7})
    monkeypatch.setattr(netproxy, "active_service", _discovery_times_out)
    monkeypatch.setattr(
        netproxy, "restore_pac", lambda *a: pytest.fail("nothing may be restored with no service to name")
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate",
        lambda pid, marker: stopped["terminated"].append((pid, marker)) or supervisor.Termination.STOPPED,
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert (7, "addon.py") in stopped["terminated"], "the proxy must be stopped regardless"
    assert "could not restore the proxy settings" in result.output
    assert "none is recorded" in result.output
    assert "did not finish within 5s" in result.output


def test_down_refuses_success_when_there_is_nothing_recorded_and_discovery_times_out(profile, runner, monkeypatch):
    """No proxy answering and no runtime file used to mean "nothing to stop", exit 0 — and it still
    does when discovery says there is no default route. But a discovery that never answered has
    not said that: a PAC of ours could be installed on a service nobody checked, and exit 0 would
    claim the network was looked at."""
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", _discovery_times_out)
    monkeypatch.setattr(
        netproxy, "restore_pac", lambda *a: pytest.fail("nothing may be restored with no service to name")
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "could not be checked" in result.output
    assert "did not finish within 5s" in result.output


def test_down_refuses_to_claim_left_untouched_when_the_pac_cannot_be_read(profile, runner, monkeypatch):
    """A failed `networksetup` used to parse as "no PAC, not ours", so `down` printed "left
    untouched", deleted the runtime file, and the PAC it never read stayed pointing at a dead port."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {
            "proxyPid": 99,
            "service": "Wi-Fi",
            "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": True},
        }
    )
    touched = []
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "pac_status", _unreadable_pac)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: touched.append(a))
    monkeypatch.setattr(supervisor, "_terminate", lambda pid, marker: supervisor.Termination.STOPPED)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "could not" in result.output and "not ours" not in result.output
    assert touched == []
    assert config.runtime_file().exists(), "the record of what to restore must survive a failed attempt"


def test_down_finishes_a_restore_that_failed_half_way(profile, runner, monkeypatch):
    """`restore_pac` hands the URL back before it sets the flag, so a failure between the two
    leaves the previous URL with the wrong state. That is not a PAC somebody set by hand; it is
    ours, half restored. Reading it as "not ours" left the corporate PAC enabled for good."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {
            "proxyPid": 99,
            "service": "Wi-Fi",
            "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": False},
        }
    )
    restored = []
    stopped = {"terminated": []}
    monkeypatch.setattr(api, "_health", lambda: None if stopped["terminated"] else {"pid": 99})
    monkeypatch.setattr(
        netproxy, "pac_status", lambda service: netproxy.PacStatus("http://proxy.example.com/corp.pac", True, False)
    )
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(
        supervisor,
        "_terminate",
        lambda pid, marker: stopped["terminated"].append(pid) or supervisor.Termination.STOPPED,
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert restored == [("Wi-Fi", "http://proxy.example.com/corp.pac", False)]
    assert "not ours" not in result.output


def test_up_says_when_it_cannot_read_the_pac_it_just_installed(profile, runner, monkeypatch):
    """The banner's "PAC is disabled/not ours" is a diagnosis; a read that failed made none.
    And the failure summary only names what was printed as it happened, so this must be."""
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        api,
        "_health",
        lambda: {
            "pid": 1,
            "activeScenario": "default",
            "scenarios": ["default"],
            "overrideCount": 0,
            "proxyPort": 8080,
        },
    )
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (True, "trusted"))
    fake_simctl(monkeypatch, [_PHONE])
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus("", False, False))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: None)
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)
    monkeypatch.setattr(netproxy, "intercepting", _unreadable_pac)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not read the PAC" in result.output
    assert "not ours" not in result.output


def test_up_spawns_no_real_watchdog_subprocess(profile, runner, monkeypatch):
    """The watchdog the tests want is a recorded pid, never a process.

    A real one is a fresh interpreter: it runs the true `netproxy` against this machine's
    `networksetup`, and because it inherits a temporary state directory but the default control
    port, it keeps answering to whatever real proxy holds 8088 and never exits. This asserts on
    the spawn itself rather than on a later `ps`, because by the time a leaked watchdog is
    visible the run that made it has finished and nothing connects the two."""
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(api, "_health", lambda: _health_payload())
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (True, "trusted"))
    fake_simctl(monkeypatch, [_PHONE])
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: None)
    # What sends `up` down the spawning branch: with no watchdog recorded, a pid it believes is
    # ours is the difference between reusing one and starting one. Deliberately NOT using the
    # shared `up` doubles here — they stub the spawn themselves, which is the thing under test.
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)

    # This `up` adopts a proxy that health already reports, so it has no reason to start any
    # process at all. Recording every attempt rather than filtering for `_watchdog` in argv keeps
    # the assertion true if the spawn moves or is renamed — and keeps it from passing if some
    # future caller catches the error and carries on.
    attempted: list[list[str]] = []

    def refuse(args, *rest, **kwargs):
        attempted.append(list(args))
        raise AssertionError(f"a test started a subprocess: {list(args)}")

    monkeypatch.setattr(subprocess, "Popen", refuse)

    result = runner.invoke(cli.cli, ["up"])

    assert attempted == [], f"`up` started a subprocess: {attempted}"
    assert result.exit_code == 0, result.output
    assert config.read_runtime()["watchdogPid"] == 4242


def test_up_records_the_proxy_pid_when_service_discovery_times_out(profile, runner, monkeypatch):
    """An uncaught error from discovery left `up` exiting with the proxy it had just adopted
    running and no pid written — nothing for `down` to find and stop. The run still fails, and the
    message names both what it means (no service, nothing intercepted) and why."""
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        api,
        "_health",
        lambda: {
            "pid": 4321,
            "activeScenario": "default",
            "scenarios": ["default"],
            "overrideCount": 0,
            "proxyPort": 8080,
        },
    )
    monkeypatch.setattr(sim, "trust_ca_in_sim", lambda simulator: (True, "trusted"))
    fake_simctl(monkeypatch, [_PHONE])
    monkeypatch.setattr(netproxy, "active_service", _discovery_times_out)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not detect the active network service" in result.output
    assert "did not finish within 5s" in result.output
    assert config.read_runtime()["proxyPid"] == 4321


def test_watchdog_keeps_the_runtime_file_when_the_restore_keeps_failing(profile, runner, monkeypatch):
    """The watchdog used to suppress the failure and delete the file anyway, which made a
    failed restore permanent and invisible: nothing was left for `down` to act on. And it must
    try more than once: `networksetup` fails transiently during the network changes that kill
    proxies in the first place."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    attempts = []
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "pac_status", lambda service: (attempts.append(1), _unreadable_pac(service)))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert len(attempts) == supervisor._WATCHDOG_RESTORE_ATTEMPTS
    assert config.runtime_file().exists()


def test_watchdog_restores_under_the_lock_up_takes_and_looks_again_once_it_holds_it(profile, runner, monkeypatch):
    """`up` reuses a running watchdog. A replacement that starts after the watchdog's first look
    but before its restore would have its PAC restored over and its runtime file deleted — so the
    restore happens under `up`'s lock, and health is checked again once the lock is held."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    looks = []
    locked = []
    restored = []

    def health():
        """Dead at the first look; by the time the lock is held, `up` has started a replacement
        and recorded it — which is what makes the record's proxy the live one again."""
        looks.append(1)
        if len(looks) == 1:
            return None
        config.write_runtime({**config.read_runtime(), "proxyPid": 2})
        return {"pid": 2}

    class Stop(Exception):
        pass

    def flock(handle, operation):
        locked.append(operation)

    def sleep(seconds):
        raise Stop  # the first poll delay: by then it must be watching again, not restoring

    monkeypatch.setattr(api, "_health", health)
    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(time, "sleep", sleep)

    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])

    assert isinstance(result.exception, Stop), result.output
    assert locked and set(locked) == {fcntl.LOCK_EX}, "the restore must wait for any `up` in progress"
    assert restored == [], "the replacement owns the network now"
    assert config.runtime_file().exists(), "the replacement's runtime file must survive"


def test_watchdog_retires_when_another_proxy_holds_its_control_port(profile, runner, monkeypatch):
    """Issue #59. Health answers "a proxy is up on this port", which is not "my proxy is up": the
    port outlives the proxy that opened it. A watchdog whose proxy died while a later `up` took
    the port never reached the restore that holds its only `return`, so it ran for as long as the
    stranger did — and stayed eligible to repair a PAC for a proxy that was not the one running."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    touched = []
    monkeypatch.setattr(api, "_health", lambda: {"pid": 2})  # a stranger, on the same port
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus("", False, False))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: touched.append(service))
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: touched.append(a))
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("it kept polling a stranger's proxy"))

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert touched == [], "the live proxy owns the network now"
    assert config.runtime_file().exists(), "the record is the only description of what to put back"


def test_watchdog_retires_when_the_record_names_a_live_successor(profile, runner, monkeypatch):
    """`up` replaces a watchdog that watches another service, and records the new one. Two
    processes repairing one service is a state nothing here was designed for, so the one the
    record no longer names stands down."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {"proxyPid": 99, "service": "Wi-Fi", "watchdogPid": 4242, "previousPac": {"url": "", "enabled": False}}
    )
    touched = []
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)
    monkeypatch.setattr(api, "_health", lambda: {"pid": 99})
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus("", False, False))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: touched.append(service))
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("a superseded watchdog kept polling"))

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert touched == [], "the recorded watchdog owns this service"


def test_watchdog_retires_once_down_has_removed_the_record(profile, runner, monkeypatch):
    """With no record there is nothing to protect and nothing to put back — and the port may
    simply belong to another state root's proxy, which is not this watchdog's to serve."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    touched = []
    monkeypatch.setattr(api, "_health", lambda: {"pid": 99})
    monkeypatch.setattr(netproxy, "set_pac", lambda service: touched.append(service))
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("it kept polling with no record"))

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert touched == []


def test_watchdog_follows_the_record_when_up_adopts_a_different_proxy(profile, runner, monkeypatch):
    """`up` reuses a running watchdog across proxies, so identity has to come from the record it
    reads each poll rather than from the pid it started with. Bound to the pid it was spawned for,
    a reused watchdog would retire the moment `up` adopted another one and leave nothing
    watching."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    repaired = []
    live = {"pid": 99}

    def sleep(seconds):
        if live["pid"] == 100:
            raise _Stop
        # Between polls, and so between this watchdog's turns holding the lock: `up` adopted a
        # different proxy and rewrote the record, keeping this watchdog rather than spawning one.
        config.write_runtime({**config.read_runtime(), "proxyPid": 100})
        live["pid"] = 100

    monkeypatch.setattr(api, "_health", lambda: dict(live))
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: repaired.append(service))
    monkeypatch.setattr(time, "sleep", sleep)

    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])

    assert isinstance(result.exception, _Stop), result.output
    assert repaired == ["Wi-Fi", "Wi-Fi"], "it stopped serving the record when the proxy changed"


def test_watchdog_does_not_retire_when_up_replaces_the_proxy_while_it_waits_for_the_lock(profile, runner, monkeypatch):
    """`up` can start a replacement and adopt this very watchdog in the gap between its look at
    health and the lock it takes to decide on it. Comparing the older look against the newer record
    then retires the one watchdog the new proxy has, and leaves its PAC with nothing watching it —
    the shape this whole change exists to remove, arrived at from the other side."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {"proxyPid": 99, "service": "Wi-Fi", "watchdogPid": os.getpid(), "previousPac": {"url": "", "enabled": False}}
    )
    live = {"pid": 99}
    repaired = []

    def took_the_lock(handle, operation):
        # `up` got there first: a new proxy, this watchdog kept, the record already rewritten.
        config.write_runtime({**config.read_runtime(), "proxyPid": 100})
        live["pid"] = 100

    monkeypatch.setattr(fcntl, "flock", took_the_lock)
    monkeypatch.setattr(api, "_health", lambda: dict(live))
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: repaired.append(service))
    monkeypatch.setattr(time, "sleep", lambda seconds: (_ for _ in ()).throw(_Stop()))

    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])

    assert isinstance(result.exception, _Stop), result.output
    assert repaired == ["Wi-Fi"], "it retired on a stale look and left the new proxy unwatched"


def test_watchdog_keeps_serving_a_record_that_names_no_live_watchdog(profile, runner, monkeypatch):
    """`up` holds this lock from before it spawns until after it records the pid, so a watchdog
    that got the lock cannot be looking at a half-written record — an unnamed one means an `up`
    that failed partway. There is no successor to hand over to, and standing down would leave the
    PAC with nobody watching it."""
    repaired = _watch_one_poll(
        monkeypatch, recorded_service="Wi-Fi", pac=netproxy.PacStatus(netproxy.pac_url(), False, True)
    )
    monkeypatch.setattr(
        supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS
    )  # would retire, if one were named

    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])

    assert isinstance(result.exception, _Stop)
    assert repaired == ["Wi-Fi"]


class _Stop(Exception):
    pass


def _watch_one_poll(monkeypatch, *, recorded_service, pac):
    """A live proxy, a runtime record naming `recorded_service`, the PAC as `pac` reports it,
    and a watchdog that runs exactly one poll before the sleep ends it. Returns the `set_pac`
    calls it made."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": recorded_service, "previousPac": {"url": "", "enabled": False}})
    repaired = []

    def stop(seconds):
        raise _Stop

    monkeypatch.setattr(api, "_health", lambda: {"pid": 99})
    monkeypatch.setattr(netproxy, "pac_status", lambda service: pac)
    monkeypatch.setattr(netproxy, "set_pac", lambda service: repaired.append(service))
    monkeypatch.setattr(time, "sleep", stop)
    return repaired


def test_watchdog_re_enables_its_own_service_pac_when_macos_switches_it_off(profile, runner, monkeypatch):
    repaired = _watch_one_poll(
        monkeypatch, recorded_service="Wi-Fi", pac=netproxy.PacStatus(netproxy.pac_url(), False, True)
    )
    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])
    assert isinstance(result.exception, _Stop)
    assert repaired == ["Wi-Fi"]


def test_watchdog_leaves_a_pac_alone_once_the_route_has_moved_to_another_service(profile, runner, monkeypatch):
    """Restoring "no previous PAC" on Wi-Fi leaves our URL installed and disabled — the shape
    the live loop exists to undo. A Wi-Fi watchdog that outlived the move to Ethernet used to
    switch it straight back on, and the next `down` restored Ethernet only."""
    repaired = _watch_one_poll(
        monkeypatch, recorded_service="Ethernet", pac=netproxy.PacStatus(netproxy.pac_url(), False, True)
    )
    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])
    assert isinstance(result.exception, _Stop)
    assert repaired == [], "the record names another service: not this watchdog's PAC to touch"


def test_up_waits_for_the_lock_holder_and_gives_up_only_after_the_timeout(profile, monkeypatch):
    """The holder is another `up`, or the watchdog restoring the network after a crash. Failing at
    once used to say "another up is in progress" while a restore ran; starting anyway would
    interleave an install with that restore on one network service."""
    import fcntl

    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    with open(config.lock_file(), "w") as holder, open(config.lock_file(), "w") as waiter:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="still in progress"):
            supervisor._acquire_lock(waiter, timeout=0.2)
        fcntl.flock(holder, fcntl.LOCK_UN)
        supervisor._acquire_lock(waiter, timeout=0.2)  # released: acquired without error


def test_up_keeps_the_recovery_record_the_watchdog_preserved(profile, runner, monkeypatch):
    """Corporate PAC → Lyrebird → crash → every restore attempt fails → the watchdog keeps the
    runtime file → `up`. Snapshotting the network now would read "ours, so nothing to restore" and
    the corporate PAC would be gone for good at the next `down`."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert config.read_runtime()["previousPac"] == _CORPORATE


def test_up_keeps_the_recovery_record_when_it_cannot_read_the_pac(profile, runner, monkeypatch):
    """The runtime file is rewritten before the PAC is read, so the record has to be in that first
    write: an `up` that fails on the read used to leave a file with a pid and no `previousPac`,
    and `down` then restored "nothing" over the corporate PAC once reads worked again."""
    _up_after_a_crash(profile, monkeypatch, _unreadable_pac)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not install" in result.output
    assert config.read_runtime()["previousPac"] == _CORPORATE


def test_up_keeps_the_recorded_service_when_the_route_is_gone_so_down_can_still_restore(profile, runner, monkeypatch):
    """Crash, failed restore, Wi-Fi switched off, `up`. It used to record `service: null` beside
    the kept PAC, and a `down` before the route came back then found no service, restored
    nothing, and deleted the record — "stopped", with the corporate PAC never put back."""
    _up_after_a_crash(
        profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True), service=None
    )
    assert runner.invoke(cli.cli, ["up"]).exit_code == 0
    assert config.read_runtime()["service"] == "Wi-Fi"
    assert config.read_runtime()["previousPac"] == _CORPORATE

    restored = []
    stopped = {"terminated": []}
    monkeypatch.setattr(api, "_health", lambda: None if stopped["terminated"] else _LIVE)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(
        supervisor,
        "_terminate",
        lambda pid, marker: stopped["terminated"].append(pid) or supervisor.Termination.STOPPED,
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert restored == [("Wi-Fi", _CORPORATE["url"], False)]


def test_up_fails_when_its_final_look_finds_the_pac_not_routing(profile, runner, monkeypatch):
    """Every step succeeded and then the PAC was switched off before the last look. The banner
    said NOT INTERCEPTING; the exit code said 0, because only the steps fed it."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(netproxy, "intercepting", lambda service: False)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "NOT INTERCEPTING" in result.output


def test_up_fails_when_the_proxy_stops_answering_before_the_final_look(profile, runner, monkeypatch):
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True),
        health=[None, _LIVE, None],
    )  # dead at first, up for the wait, gone at the end

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "stopped answering" in result.output


_ETHERNET_PAC = netproxy.PacStatus("http://proxy.example.org/eth.pac", True, False)


def test_up_restores_the_old_service_before_switching_to_a_new_one(profile, runner, monkeypatch):
    """Crash on Wi-Fi, failed restore, then Ethernet becomes the route and `up` runs. Writing a
    record for Ethernet over Wi-Fi's used to leave Wi-Fi's PAC pointing at the dead proxy with
    nothing left to say so — `down` restored Ethernet, deleted the file, and called it stopped."""
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True) if service == "Wi-Fi" else _ETHERNET_PAC,
        service="Ethernet",
    )
    restored = []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert restored == [("Wi-Fi", _CORPORATE["url"], False)]
    runtime = config.read_runtime()
    assert runtime["service"] == "Ethernet"
    assert runtime["previousPac"] == {"url": _ETHERNET_PAC.url, "enabled": True}


def test_up_keeps_the_record_and_fails_when_the_old_service_cannot_be_restored(profile, runner, monkeypatch):
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: _unreadable_pac(service) if service == "Wi-Fi" else _ETHERNET_PAC,
        service="Ethernet",
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not restore the previous PAC on 'Wi-Fi'" in result.output
    runtime = config.read_runtime()
    assert runtime["service"] == "Wi-Fi" and runtime["previousPac"] == _CORPORATE


def test_up_replaces_a_watchdog_that_watches_another_service(profile, runner, monkeypatch):
    """A watchdog is told its service on the command line. Reusing one from a run on Wi-Fi for a
    run on Ethernet had it restore Wi-Fi's record and then delete Ethernet's."""
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True) if service == "Wi-Fi" else _ETHERNET_PAC,
        service="Ethernet",
    )
    config.write_runtime({**config.read_runtime(), "watchdogPid": 77})
    terminated, spawned = [], []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    monkeypatch.setattr(
        supervisor, "_terminate", lambda pid, marker: terminated.append((pid, marker)) or supervisor.Termination.STOPPED
    )
    monkeypatch.setattr(supervisor, "_spawn_watchdog", lambda service: spawned.append(service) or 4242)

    assert runner.invoke(cli.cli, ["up"]).exit_code == 0
    assert (77, "_watchdog") in terminated
    assert spawned == ["Ethernet"]
    assert config.read_runtime()["watchdogPid"] == 4242


def test_a_failed_install_on_the_new_service_cannot_let_the_old_watchdog_delete_its_record(
    profile, runner, monkeypatch
):
    """Wi-Fi → Ethernet, and installing on Ethernet fails after `up` has already written
    Ethernet's record. The Wi-Fi watchdog used to outlive that: at the proxy's next death it read
    Ethernet's record, found Wi-Fi's PAC "not ours", and deleted the record — Ethernet left at
    the dead proxy with its previous PAC forgotten."""
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True) if service == "Wi-Fi" else _ETHERNET_PAC,
        service="Ethernet",
    )
    config.write_runtime({**config.read_runtime(), "watchdogPid": 77})
    terminated = []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    monkeypatch.setattr(
        supervisor, "_terminate", lambda pid, marker: terminated.append((pid, marker)) or supervisor.Termination.STOPPED
    )
    monkeypatch.setattr(netproxy, "set_pac", _unreadable_pac)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1 and "could not install" in result.output
    assert (77, "_watchdog") in terminated, "retired before Ethernet's record existed"
    ethernet = {"url": _ETHERNET_PAC.url, "enabled": True}
    assert config.read_runtime()["previousPac"] == ethernet

    # And even a watchdog that somehow survived must not act on another service's record.
    monkeypatch.setattr(api, "_health", lambda: None)
    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert config.read_runtime()["previousPac"] == ethernet


def test_up_keeps_the_recovery_record_of_a_half_restored_pac(profile, runner, monkeypatch):
    """A restore that failed between its two calls has handed the corporate URL back but left it
    enabled. `down` already treats that as still Lyrebird's to finish; `up` must not read it as
    somebody else's PAC and snapshot the wrong flag as the thing to restore."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus(_CORPORATE["url"], True, False))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert config.read_runtime()["previousPac"] == _CORPORATE


def test_watchdog_restores_on_a_later_attempt_after_a_transient_failure(profile, runner, fake_network, monkeypatch):
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    readings = iter([None])  # the first read fails, the second sees our PAC

    def flaky(service):
        if next(readings, "ok") is None:
            _unreadable_pac(service)
        return netproxy.PacStatus(netproxy.pac_url(), True, True)

    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.GONE)  # it died
    monkeypatch.setattr(netproxy, "pac_status", flaky)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert fake_network["restored"] == ("Wi-Fi", "", False)
    assert not config.runtime_file().exists()


def test_watchdog_clears_the_runtime_file_after_restoring(profile, runner, fake_network, monkeypatch):
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.GONE)  # it died

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert fake_network["restored"] == ("Wi-Fi", "", False)
    assert not config.runtime_file().exists()


def test_down_takes_the_lock_before_it_signals_the_watchdog_or_touches_the_pac(
    profile, runner, fake_network, monkeypatch
):
    """A watchdog repair in flight holds the lock; `down` must wait for it. Signalling first only
    kills the watchdog, not the `networksetup` it already started, and that child could re-enable
    the PAC after `down`'s restore had read back clean — "stopped", with the PAC pointing at the
    stopped proxy and its record deleted."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {"proxyPid": 99, "watchdogPid": 98, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}}
    )
    events = []
    health_until_terminated(monkeypatch, fake_network, pid=99)
    monkeypatch.setattr(supervisor, "_acquire_lock", lambda lock, timeout=0: events.append("lock"))
    monkeypatch.setattr(
        supervisor,
        "_terminate",
        lambda pid, marker: events.append(f"terminate {marker}")
        or fake_network["terminated"].append((pid, marker))
        or supervisor.Termination.STOPPED,
    )
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: events.append("restore"))

    assert runner.invoke(cli.cli, ["down"]).exit_code == 0
    assert events[:3] == ["lock", "terminate _watchdog", "restore"]


def test_down_waits_for_an_overlapping_watchdog_repair_that_holds_the_lock_and_restores_after_it(profile, monkeypatch):
    """The interleaving the lock exists for, driven for real rather than pinned by call order.

    The test above pins the *order* `down` does things in; this one pins what happens when a
    watchdog repair is already inside the lock when `down` arrives. That is the dangerous
    overlap: `_repair_pac` re-enables our PAC, so a `down` running alongside it would have the
    PAC switched back on after its own restore had read the network back clean — leaving the Mac
    pointed at a stopped proxy, its runtime record deleted, and "stopped" on the terminal.

    Threads with separate `open()` calls are a faithful substitute for two processes here because
    a `flock` lock belongs to the open file description, not to the process: two descriptions of
    one file contend on macOS and Linux whether they live in one process or two, and that
    contention is the whole subject. What threads do *not* exercise — SIGTERM actually reaching a
    watchdog — is not what this test claims.

    Determinism comes from a handshake, not from sleeps: the repair parks inside `pac_status`
    while holding the lock, and the main thread waits for the "waiting for another…" diagnostic
    `_acquire_lock` prints the first time its non-blocking `flock` fails. That message is positive
    evidence that `down` is blocked on the lock, so the "nothing of `down` has happened yet"
    assertion below cannot pass merely because `down` was slow off the mark. Every wait is
    bounded and its result asserted, so a lost handshake fails the test instead of hanging it.
    """
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {
            "proxyPid": 99,
            "watchdogPid": 98,
            "service": "Wi-Fi",
            "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": True},
        }
    )

    state = {"url": netproxy.pac_url(), "enabled": False}  # ours, but macOS switched it off
    events = []
    repair_holds_lock = threading.Event()
    down_is_waiting = threading.Event()
    release = threading.Event()

    def pac_status(service):
        if threading.current_thread().name == "repair":
            events.append("repair:pac_status")
            repair_holds_lock.set()
            assert release.wait(timeout=10), "the repair was never released"
        else:
            events.append("down:pac_status")
        return netproxy.PacStatus(state["url"], state["enabled"], state["url"] == netproxy.pac_url())

    def set_pac(service):
        state.update(url=netproxy.pac_url(), enabled=True)
        events.append("repair:set_pac")

    def restore_pac(service, url, enabled):
        state.update(url=url, enabled=enabled)
        events.append("down:restore")

    def terminate(pid, marker):
        events.append(f"down:terminate {marker}")
        return supervisor.Termination.STOPPED

    monkeypatch.setattr(netproxy, "pac_status", pac_status)
    monkeypatch.setattr(netproxy, "set_pac", set_pac)
    monkeypatch.setattr(netproxy, "restore_pac", restore_pac)
    monkeypatch.setattr(supervisor, "_terminate", terminate)
    monkeypatch.setattr(api, "_health", lambda: None if "down:terminate addon.py" in events else {"pid": 99})

    real_echo = cli.click.echo

    def echo(message="", *args, **kwargs):
        if "waiting for another" in str(message):
            down_is_waiting.set()
        return real_echo(message, *args, **kwargs)

    monkeypatch.setattr(cli.click, "echo", echo)

    failures = []
    result = {}

    def worker(name, work):
        def run():
            try:
                work()
            except Exception as error:  # re-raised on the main thread once both are joined
                failures.append(error)

        return threading.Thread(target=run, name=name)

    # Exactly one Click invocation runs at a time, on the `down` thread; nothing else touches
    # Click or global I/O while it runs, and it is joined before the fixtures tear down.
    repair_thread = worker("repair", lambda: supervisor._repair_pac("Wi-Fi"))
    down_thread = worker("down", lambda: result.update(down=CliRunner().invoke(cli.cli, ["down"])))

    repair_thread.start()
    try:
        assert repair_holds_lock.wait(timeout=10), "the repair never reached the lock"
        down_thread.start()
        assert down_is_waiting.wait(timeout=5), "`down` never waited on the lock the repair holds"
        assert [event for event in events if event.startswith("down:")] == [], (
            "`down` signalled the watchdog or touched the PAC while the repair held the lock"
        )
        assert down_thread.is_alive()
    finally:
        # Cleanup and the verdict on the workers both live here: an assertion above must not
        # skip the joins (the fixtures tear down next), and a worker that raised must be heard
        # even when the main thread already has something to say.
        release.set()
        for thread in (repair_thread, down_thread):
            if thread.ident is not None:
                thread.join(timeout=10)
        assert not repair_thread.is_alive() and not down_thread.is_alive(), "a worker never finished"
        if failures:
            raise failures[0]

    assert events == [
        "repair:pac_status",
        "repair:set_pac",
        "down:terminate _watchdog",
        "down:pac_status",
        "down:restore",
        "down:terminate addon.py",
    ], "the repair must finish before `down` reads the PAC it is about to restore"
    assert state == {"url": "http://proxy.example.com/corp.pac", "enabled": True}
    assert not config.runtime_file().exists()
    assert result["down"].exit_code == 0, result["down"].output
    assert "stopped" in result["down"].output

    # The other half of the guarantee: a repair entering afterwards finds no runtime record
    # naming its service, and returns before it reads — let alone writes — the network.
    def must_not_be_called(service):
        raise AssertionError("pac_status must not be called after `down`")

    monkeypatch.setattr(netproxy, "pac_status", must_not_be_called)
    supervisor._repair_pac("Wi-Fi")
    assert len(events) == 6, "a repair that arrived after `down` did something"
    assert state == {"url": "http://proxy.example.com/corp.pac", "enabled": True}


def test_down_reports_a_proxy_that_did_not_stop(profile, runner, fake_network, monkeypatch):
    """SIGTERM is a request. Printing "stopped" over a proxy that is still serving is the lie this
    check exists to prevent."""
    monkeypatch.setattr(api, "_health", lambda: {"pid": 4242})  # never dies
    monkeypatch.setattr(supervisor, "_DOWN_WAIT_SECONDS", 0.3)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "still responding" in result.output


@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_fails_when_not_intercepting_in_either_format(profile, runner, monkeypatch, args):
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))

    assert runner.invoke(cli.cli, ["status", *args]).exit_code == 1


@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_succeeds_only_when_up_and_intercepting(profile, runner, monkeypatch, args):
    monkeypatch.setattr(
        api,
        "_health",
        lambda: {
            "pid": 1,
            "scenarios": ["default"],
            "activeScenario": "default",
            "overrideCount": 0,
            "simBundleId": None,
            "proxyPort": 8080,
        },
    )
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))

    assert runner.invoke(cli.cli, ["status", *args]).exit_code == 0


@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_reports_an_unreadable_pac_as_unproven_not_off(profile, runner, monkeypatch, args):
    """Same exit code as "off", different explanation: a person told DISABLED goes and switches it
    on, which is not the fix for a `networksetup` that is failing."""
    monkeypatch.setattr(
        api,
        "_health",
        lambda: {
            "pid": 1,
            "scenarios": ["default"],
            "activeScenario": "default",
            "overrideCount": 0,
            "simBundleId": None,
            "proxyPort": 8080,
        },
    )
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", _unreadable_pac)

    result = runner.invoke(cli.cli, ["status", *args])

    assert result.exit_code == 1
    if args:
        payload = json.loads(result.output)
        assert payload["intercepting"] is False and payload["pac"] is None
        assert "networksetup" in payload["pacError"]
    else:
        assert "could not read the PAC" in result.output
        assert "disabled" not in result.output.lower() and "not ours" not in result.output


def test_status_fails_when_the_proxy_is_up_but_the_pac_is_off(profile, runner, monkeypatch):
    """Up is not the same as intercepting, and this is the gap the exit code exists to report."""
    monkeypatch.setattr(
        api,
        "_health",
        lambda: {"pid": 1, "scenarios": [], "activeScenario": None, "overrideCount": 0, "simBundleId": None},
    )
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))

    assert runner.invoke(cli.cli, ["status"]).exit_code == 1


def _status_health(fingerprint=None):
    payload = {
        "pid": 1,
        "scenarios": ["default", "orders-outage"],
        "activeScenario": "orders-outage",
        "overrideCount": 3,
        "simBundleId": "com.example.Store",
        "proxyPort": 8080,
    }
    if fingerprint is not None:
        payload["profileFingerprint"] = fingerprint
    return lambda: payload


@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_refuses_a_foreign_profiles_interception_in_either_format(profile, runner, monkeypatch, args):
    """Regression: the PAC on this port is "ours" whoever started the proxy behind it, so a proxy
    running profile A made `lyrebird --profile B status` print INTERCEPT ACTIVE and exit 0 — and
    `lyrebird status && …` went on to drive a proxy mocking someone else's hosts and scenarios."""
    monkeypatch.setattr(api, "_health", _status_health("feedfacef00d"))
    _status_network(monkeypatch)

    result = runner.invoke(cli.cli, ["status", *args])

    assert result.exit_code == 1
    if args:
        payload = json.loads(result.output)
        assert payload["profileMismatch"] is True and payload["intercepting"] is False
        assert payload["proxyUp"] is True, "the port is genuinely held; say so"
        assert payload["runningProfileFingerprint"] == "feedfacef00d"
        assert payload["profileFingerprint"] == config.PROFILE_FINGERPRINT
        # The other proxy's state is not this profile's, and null says so where `[]` would claim
        # this profile has no scenarios.
        assert payload["scenarios"] is None and payload["activeScenario"] is None
        assert payload["overrideCount"] is None and payload["simBundleId"] is None
    else:
        assert "INTERCEPT ACTIVE" not in result.output
        # Both fingerprints, because "a different profile" alone does not say which is which.
        assert "feedfacef00d" in result.output and config.PROFILE_FINGERPRINT in result.output
        assert "lyrebird down" in result.output, "the remedy, and it is not `up` — `up` refuses"
        assert "LYREBIRD_CONTROL_PORT" in result.output, "the other way out: aim at another port"
        assert "lyrebird up" not in result.output
        assert "orders-outage" not in result.output, "another profile's scenario, reported as ours"


def test_status_reports_interception_when_the_fingerprints_agree(profile, runner, monkeypatch):
    """The other half: the check compares fingerprints, it does not just distrust the field."""
    monkeypatch.setattr(api, "_health", _status_health(config.PROFILE_FINGERPRINT))
    _status_network(monkeypatch)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["profileMismatch"] is False and payload["intercepting"] is True
    assert payload["activeScenario"] == "orders-outage"


def test_status_trusts_an_engine_too_old_to_send_a_fingerprint(profile, runner, monkeypatch):
    """A proxy from before the field existed cannot say whose it is, and `up` accepts that reading
    too. Treating silence as a mismatch would break `status` against every running older engine."""
    monkeypatch.setattr(api, "_health", _status_health())
    _status_network(monkeypatch)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["profileMismatch"] is False and payload["runningProfileFingerprint"] is None


def test_importing_the_cli_does_not_parse_the_profile(profile, tmp_path):
    """Regression, in a fresh process because an in-process test imports `config` before it can
    arrange anything: a malformed *default* profile used to raise SystemExit during import, before
    Click had seen `--profile` or the subcommand. `logs` never needs the profile; it must run
    whether the default one is broken or the selected one is."""
    import subprocess
    import sys

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "profile.json").write_text("{not json", encoding="utf-8")
    env = {**os.environ, "LYREBIRD_PROFILE": str(broken), "LYREBIRD_STATE_DIR": str(tmp_path / "state")}

    for args in (["--profile", str(profile), "logs"], ["logs"]):
        result = subprocess.run(
            [sys.executable, str(config.ROOT / "cli.py"), *args], capture_output=True, text=True, env=env, check=False
        )
        assert result.returncode == 0, f"{args}: {result.stderr}"
        assert "(no log)" in result.stdout, f"{args}: {result.stdout!r}"


def test_the_cli_runs_as_a_script_and_lists_every_command_it_documents(profile, tmp_path):
    """The entry point `bin/lyrebird` and the watchdog spawn both run by path, in a fresh process.

    `tests/test_launcher.py` drives that path with a fake interpreter, so it proves nothing about
    the imports; the commands now live in sibling modules that only `cli.py` pulls together, and a
    module it forgot to import or register would be invisible to every in-process test here.
    """
    env = {**os.environ, "LYREBIRD_PROFILE": str(profile), "LYREBIRD_STATE_DIR": str(tmp_path / "state")}
    result = subprocess.run(
        [sys.executable, str(config.ROOT / "cli.py"), "--help"], capture_output=True, text=True, env=env, check=False
    )

    assert result.returncode == 0, result.stderr
    documented = {line.split()[1] for line in (cli.__doc__ or "").splitlines() if line.startswith("    lyrebird ")}
    assert documented, "the docstring's command table is what this checks against"
    # The names under `Commands:`, one per line, not a substring search over the whole screen:
    # `untrust-ca` contains `trust-ca`, so a substring check passed with `trust-ca` unregistered.
    _, _, commands = result.stdout.partition("Commands:")
    listed = {line.split()[0] for line in commands.splitlines() if line.startswith("  ")}
    # `untrust-ca` is documented on a continuation line of the table, so it is added by hand;
    # `_watchdog` is hidden and must stay out of `--help`.
    assert listed == documented | {"untrust-ca"}, (
        f"documented but not listed: {sorted(documented - listed)}; "
        f"listed but not documented: {sorted(listed - documented - {'untrust-ca'})}"
    )


def test_status_prints_json_and_exits_non_zero_against_a_dead_control_port(profile, tmp_path):
    """`--json` decides how `status` prints, never what it means. In a fresh process because the
    point is the whole entry point: a command whose module failed to import would exit 1 too, with
    a traceback on stderr and nothing a caller could parse on stdout."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    env = {
        **os.environ,
        "LYREBIRD_PROFILE": str(profile),
        "LYREBIRD_STATE_DIR": str(tmp_path / "state"),
        "LYREBIRD_CONTROL_PORT": str(closed_port),
    }
    result = subprocess.run(
        [sys.executable, str(config.ROOT / "cli.py"), "status", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1, f"{result.stdout!r} {result.stderr!r}"
    payload = json.loads(result.stdout)
    assert payload["proxyUp"] is False and payload["intercepting"] is False


def test_down_does_not_need_a_readable_profile(profile, runner, monkeypatch):
    """`down` restores the network from runtime state and the live API. A broken profile.json is
    the kind of thing you are trying to recover from, not a reason to be stuck."""
    (profile / "profile.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", lambda: None)

    result = runner.invoke(cli.cli, ["--profile", str(profile), "down"])

    assert result.exit_code == 0, result.output
    assert "nothing to stop" in result.output


def test_up_still_aborts_loudly_on_a_malformed_profile(profile, runner):
    """Moving the read out of import must not soften it: `up` is where a bad profile is refused."""
    (profile / "profile.json").write_text("{not json", encoding="utf-8")

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code != 0
    assert "could not read" in result.output


def test_up_refuses_a_profile_with_no_hosts_before_starting_anything(profile, runner, monkeypatch):
    """With no hosts `up` used to trust the CA, install a DIRECT-only PAC, relaunch the app, print
    INTERCEPT ACTIVE and exit 0 — every step a success, nothing intercepted."""
    import subprocess

    (profile / "profile.json").write_text('{"hosts": []}', encoding="utf-8")
    config.reload_profile()

    def must_not_start(*args, **kwargs):
        raise AssertionError("the proxy must not be started for a profile that intercepts nothing")

    monkeypatch.setattr(subprocess, "Popen", must_not_start)
    monkeypatch.setattr(api, "_health", must_not_start)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code != 0
    assert "no hosts" in result.output


def test_up_starts_the_log_on_a_new_inode(profile, tmp_path):
    """A log left at 0644 by an older version cannot be made private by chmod alone.

    Anyone already holding it keeps reading, because a descriptor carries its own access. Only a
    new inode cuts them off — so this asserts the identity of the file changed, not just its mode.
    """
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.LOG_FILE.write_text("from an older run\n")
    os.chmod(config.LOG_FILE, 0o644)
    stale_inode = config.LOG_FILE.stat().st_ino

    with open(config.LOG_FILE) as reader:
        reader.read()
        supervisor._start_fresh_log()
        with open(config.LOG_FILE, "a", encoding="utf-8") as sink:
            sink.write("a host and a path\n")
        overheard = reader.read()

    assert config.LOG_FILE.stat().st_ino != stale_inode, "the lax inode was truncated, not replaced"
    assert config.LOG_FILE.stat().st_mode & 0o777 == 0o600
    assert overheard == "", f"a reader of the old log still saw traffic: {overheard!r}"


# MARK: - init


def test_init_writes_the_scenarios_directory(profile, runner, tmp_path):
    target = tmp_path / "fresh"
    result = runner.invoke(cli.cli, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert (target / "scenarios").is_dir()
    assert not (target / "sessions").exists()


def test_init_refuses_a_legacy_sessions_layout(profile, runner, tmp_path):
    """`sessions/` was renamed to `scenarios/`. Copying the examples in beside an unrenamed
    directory would leave two directories of scenarios in one profile with only one of them ever
    read, so `init` refuses and writes nothing — the same `mv` fixes it."""
    target = tmp_path / "legacy"
    (target / "sessions").mkdir(parents=True)
    (target / "sessions" / "orders-outage.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(cli.cli, ["init", str(target)])

    assert result.exit_code != 0
    assert f"mv {target / 'sessions'} {target / 'scenarios'}" in result.output
    assert sorted(path.name for path in target.iterdir()) == ["sessions"], "init must have copied nothing"


# MARK: - "stopped" is proven, not assumed


def _alive_and_ours_but_unkillable(monkeypatch):
    """A proxy that is there under every check and survives both signals: the OS seams say alive,
    `ps` says ours, and the signals are accepted but change nothing."""
    sent = []
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)
    monkeypatch.setattr(supervisor, "_signal", lambda pid, sig: sent.append((pid, sig.name)) or True)
    monkeypatch.setattr(supervisor, "_DOWN_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "_KILL_WAIT_SECONDS", 0.05)
    return sent


def _recorded_run(watchdog_pid=98):
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "watchdogPid": watchdog_pid, "service": "Wi-Fi", "previousPac": _CORPORATE})


def test_down_reports_a_proxy_that_is_alive_but_silent(profile, runner, monkeypatch):
    """The control port going quiet was the only proof `down` asked for — and a proxy whose event
    loop has hung is quiet too. It was reported stopped, its record deleted, with the PAC still
    pointing at a process that was still running. Now the pid has to be gone as well."""
    _recorded_run(watchdog_pid=None)
    _status_network(monkeypatch)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    monkeypatch.setattr(api, "_health", lambda: None)  # silent from the first look
    sent = _alive_and_ours_but_unkillable(monkeypatch)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "proxy pid 99 is still running" in result.output
    assert "stopped" not in result.output
    assert config.runtime_file().exists(), "the record is the only description of what to put back"
    assert (99, "SIGKILL") in sent, "SIGTERM was ignored; SIGKILL is the escalation that used to be missing"


def test_down_keeps_the_record_when_the_proxy_cannot_be_checked(profile, runner, monkeypatch):
    """`ps` failing is not "not ours". Read that way, the signal was skipped and "stopped" printed
    over a proxy nothing had looked at."""
    _recorded_run(watchdog_pid=None)
    _status_network(monkeypatch)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)

    def ps_is_broken(pid, marker):
        raise supervisor.ProcessCheckError(f"`ps -p {pid}` failed: ps: cannot allocate memory")

    monkeypatch.setattr(supervisor, "_identity_of", ps_is_broken)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "could not stop pid 99: `ps -p 99` failed" in result.output
    assert "proxy pid 99 is unverified" in result.output
    assert config.runtime_file().exists()


@pytest.mark.parametrize("outcome", [supervisor.Termination.STILL_RUNNING, supervisor.Termination.UNVERIFIED])
def test_down_touches_nothing_while_the_watchdog_lives(profile, runner, monkeypatch, outcome):
    """A watchdog that is not proven gone fights any partial teardown: with the record kept
    because the proxy would not stop, its live loop re-enables the PAC `down` just switched off,
    and its death path restores from that record and deletes it. So nothing is touched — not the
    PAC, not the proxy — until it is gone."""
    _recorded_run()
    _status_network(monkeypatch)
    restored, stopped = [], []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(api, "_health", lambda: {"pid": 99})

    def terminate(pid, marker):
        if marker == "_watchdog":
            return outcome
        stopped.append(pid)
        return supervisor.Termination.STOPPED

    monkeypatch.setattr(supervisor, "_stop", terminate)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert f"the watchdog (pid 98) is {outcome.value} — nothing was changed" in result.output
    assert restored == [] and stopped == []
    assert config.runtime_file().exists()


def test_down_stops_the_proxy_that_answers_when_the_recorded_one_is_gone(profile, runner, monkeypatch):
    """A record naming a dead pid while a replacement answers on the port — an `up` under another
    state root, or one that died before recording its child. Checking only the recorded pid left
    the replacement running and the record kept, so every `down` exited 1 the same way."""
    _recorded_run(watchdog_pid=None)
    _status_network(monkeypatch)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    stopped = []
    monkeypatch.setattr(api, "_health", lambda: None if 100 in stopped else {"pid": 100})

    def terminate(pid, marker):
        if pid in (None, 99):
            return supervisor.Termination.NOT_RUNNING
        stopped.append(pid)
        return supervisor.Termination.STOPPED

    monkeypatch.setattr(supervisor, "_terminate", terminate)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert stopped == [100]
    assert "stopped" in result.output
    assert not config.runtime_file().exists()


def test_down_treats_a_reused_pid_as_a_proxy_that_is_gone(profile, runner, monkeypatch, _no_real_process_identity):
    """A pid alive under another command line has been reused, and reuse only happens after the
    original died. With the port quiet, that *is* the proxy gone — `down` restores and reports
    "stopped" without having signalled anything."""
    _recorded_run(watchdog_pid=None)
    _status_network(monkeypatch)
    restored = []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(supervisor, "_identity_of", _no_real_process_identity)  # the real classifier over a fake `ps`
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        sim, "_run", lambda args: subprocess.CompletedProcess(args, 0, "/usr/bin/some-other-tool\n", "")
    )
    monkeypatch.setattr(supervisor, "_signal", lambda pid, sig: pytest.fail("a reused pid must not be signalled"))

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert restored == [("Wi-Fi", _CORPORATE["url"], _CORPORATE["enabled"])]
    assert not config.runtime_file().exists()


def test_down_keeps_the_record_while_the_port_still_answers(profile, runner, fake_network, monkeypatch):
    """The record was deleted *before* the port was polled, so a `down` that then found the proxy
    still answering exited 1 having already thrown away what the next `down` needed."""
    _recorded_run()
    monkeypatch.setattr(api, "_health", lambda: {"pid": 99})  # never dies
    monkeypatch.setattr(supervisor, "_DOWN_WAIT_SECONDS", 0.05)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "still responding" in result.output
    assert config.runtime_file().exists()


def test_up_refuses_to_migrate_over_a_watchdog_it_could_not_stop(profile, runner, monkeypatch):
    """The old service's watchdog re-enables our PAC wherever it finds it disabled — which is what
    restoring "no previous PAC" leaves on the old service. Its termination result was discarded;
    now `up` goes no further until it is proven gone."""
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True) if service == "Wi-Fi" else _ETHERNET_PAC,
        service="Ethernet",
    )
    config.write_runtime({**config.read_runtime(), "watchdogPid": 77})
    restored, installed = [], []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: installed.append(service))
    monkeypatch.setattr(supervisor, "_terminate", lambda pid, marker: supervisor.Termination.STILL_RUNNING)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "the watchdog for 'Wi-Fi' (pid 77) could not be stopped: still running" in result.output
    assert restored == [] and installed == [], "nothing on the network may change under a live watchdog"
    assert config.read_runtime()["previousPac"] == _CORPORATE, "the record is kept"


@pytest.mark.parametrize("old_watchdog", ["unverifiable", "unstoppable"])
def test_up_refuses_rather_than_spawn_a_second_watchdog(profile, runner, monkeypatch, old_watchdog):
    """A previous watchdog that cannot be checked, or will not stop, is not replaced: while `ps` is
    broken it cannot recognise a successor either (`_should_retire` keeps watching), so spawning
    one puts two loops on one record. `up` exits 1 with the record still naming the old one."""
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True) if service == "Wi-Fi" else _ETHERNET_PAC,
        service="Ethernet",
    )
    config.write_runtime({**config.read_runtime(), "watchdogPid": 77, "previousPac": None})
    spawned = []
    monkeypatch.setattr(supervisor, "_spawn_watchdog", lambda service: spawned.append(service) or 4242)
    if old_watchdog == "unverifiable":

        def ps_is_broken(pid, marker):
            if pid == 99:
                return supervisor.Identity.GONE  # the crashed proxy, which the pre-spawn check asks about first
            raise supervisor.ProcessCheckError(f"`ps -p {pid}` failed: 1")

        monkeypatch.setattr(supervisor, "_identity_of", ps_is_broken)
        expected = "the previous watchdog (pid 77) could not be checked: `ps -p 77` failed: 1"
    else:
        monkeypatch.setattr(supervisor, "_terminate", lambda pid, marker: supervisor.Termination.STILL_RUNNING)
        expected = "the previous watchdog (pid 77) is still running"

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert expected in result.output
    assert spawned == []
    assert config.read_runtime()["watchdogPid"] == 77, "the record keeps naming the one the next `up` must ask about"


def test_up_names_a_child_that_survives_its_startup_cleanup(profile, runner, monkeypatch):
    """A proxy that never became healthy is stopped before `up` gives up on it. When even
    SIGKILL leaves it there, the report used to be the timeout alone — the operator learned of
    the survivor from the next `up` refusing the port."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus("", False, False), health=[None] * 50)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + 13))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)

    class Survivor:
        pid = 4321

        def terminate(self):
            pass

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("mitmdump", timeout)

        def kill(self):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Survivor())

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "did not become healthy in time" in result.output
    assert "the proxy (pid 4321) is still running — stop it by hand" in result.output


def test_watchdog_does_not_retire_when_its_successor_cannot_be_checked(profile, runner, monkeypatch):
    """Retiring on an unverifiable read is how a record ends up with nothing watching it. The
    poll that could not check its successor keeps watching and asks again."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {"proxyPid": 99, "service": "Wi-Fi", "watchdogPid": 4242, "previousPac": {"url": "", "enabled": False}}
    )
    repaired = []

    def ps_is_broken(pid, marker):
        raise supervisor.ProcessCheckError(f"`ps -p {pid}` failed: 1")

    monkeypatch.setattr(supervisor, "_identity_of", ps_is_broken)
    monkeypatch.setattr(api, "_health", lambda: {"pid": 99})
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: repaired.append(service))

    def stop(seconds):
        raise _Stop

    monkeypatch.setattr(time, "sleep", stop)

    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])

    assert isinstance(result.exception, _Stop), "it must reach the sleep, not the return"
    assert repaired == ["Wi-Fi"]


def test_up_fails_when_discovery_fails_even_with_a_recorded_service(profile, runner, monkeypatch):
    """A crash record names yesterday's service. With discovery failing, `up` fell back to it,
    installed and read back its PAC there, and exited 0 — while the route may have moved to a
    service carrying no PAC at all. The install still happens (the record must describe a service
    `down` can restore), but the run is not a success."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    installed = []
    monkeypatch.setattr(netproxy, "set_pac", lambda service: installed.append(service))
    monkeypatch.setattr(netproxy, "active_service", _discovery_times_out)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "the PAC is on 'Wi-Fi' from the last run, which may no longer carry the default route" in result.output
    assert installed == ["Wi-Fi"]
    assert config.read_runtime()["service"] == "Wi-Fi"


def test_down_records_what_is_left_when_the_proxy_will_not_stop(profile, runner, monkeypatch):
    """The record kept for a retry describes what remains, not what was done: the watchdog is
    gone and the PAC restored, so a second `down` must neither signal the old watchdog pid nor
    restore again — the user may have changed the PAC since, and restoring over it a second time
    is `down` undoing a setting it never made."""
    _recorded_run()
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    pac = {"now": netproxy.PacStatus(netproxy.pac_url(), True, True)}
    monkeypatch.setattr(netproxy, "pac_status", lambda service: pac["now"])
    restored = []

    def restore(service, url, enabled):  # the network remembers what `down` put back
        restored.append((service, url, enabled))
        pac["now"] = netproxy.PacStatus(url, enabled, False)

    monkeypatch.setattr(netproxy, "restore_pac", restore)
    monkeypatch.setattr(api, "_health", lambda: None)
    outcomes = {"_watchdog": supervisor.Termination.STOPPED, "addon.py": supervisor.Termination.STILL_RUNNING}
    monkeypatch.setattr(supervisor, "_terminate", lambda pid, marker: outcomes[marker])

    first = runner.invoke(cli.cli, ["down"])

    assert first.exit_code == 1
    assert len(restored) == 1
    assert config.read_runtime() == {"proxyPid": 99, "service": "Wi-Fi"}

    outcomes["addon.py"] = supervisor.Termination.STOPPED
    second = runner.invoke(cli.cli, ["down"])

    assert second.exit_code == 0, second.output
    assert len(restored) == 1, "the PAC was already put back; a retry must not restore it again"
    assert not config.runtime_file().exists()


def test_down_records_the_replacement_pid_before_signalling_it(profile, runner, monkeypatch):
    """The proxy answering under another pid is recorded before it is signalled: a stop that
    failed used to leave the record naming the dead pid, and the next `down` — the replacement
    now silent — found that pid gone, printed "stopped" and deleted the record over a live proxy."""
    _recorded_run(watchdog_pid=None)
    _status_network(monkeypatch)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    monkeypatch.setattr(api, "_health", lambda: {"pid": 100})

    def terminate(pid, marker):
        if pid == 100:
            assert config.read_runtime()["proxyPid"] == 100, "recorded before the signal, not after"
            return supervisor.Termination.STILL_RUNNING
        return supervisor.Termination.NOT_RUNNING

    monkeypatch.setattr(supervisor, "_terminate", terminate)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "proxy pid 100 is still running" in result.output
    assert config.read_runtime()["proxyPid"] == 100


def test_up_names_a_child_it_could_not_signal(profile, runner, monkeypatch):
    """Cleanup after a startup timeout signals the child; a signal that is refused used to
    escape as a traceback, and the child stayed."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus("", False, False), health=[None] * 50)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + 13))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)

    class Unsignallable:
        pid = 4321

        def terminate(self):
            raise PermissionError(1, "Operation not permitted")

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Unsignallable())

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "the proxy (pid 4321) could not be signalled" in result.output
    assert "Traceback" not in result.output


def test_both_children_are_spawned_with_their_identity_in_argv(profile, runner, monkeypatch, _no_real_watchdog):
    """What `_pid_is_ours` reads back from `ps`: without the tokens on the command line, a pid
    reused by another instance's process passes as ours. Asserted on the arguments that reach
    `Popen` through the real spawning paths, not on the builders — a spawn that stopped using
    them would pass a builder test."""
    spawned = []

    class Proc:
        pid = 4321

    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: spawned.append(list(args)) or Proc())

    assert runner.invoke(cli.cli, ["up"]).exit_code == 0
    _no_real_watchdog("Wi-Fi")

    proxy, watchdog = spawned
    assert proxy[1:5] == [
        "--set",
        f"lyrebird_control_port={config.CONTROL_PORT}",
        "--set",
        f"lyrebird_state_root_id={config.state_root_id()}",
    ], "first: the match `ps` yields"
    assert watchdog[-5:] == [
        "--control-port",
        str(config.CONTROL_PORT),
        "--state-root-id",
        config.state_root_id(),
        "Wi-Fi",
    ], "before the service"


def test_down_does_not_forget_a_replacement_that_went_silent(profile, runner, monkeypatch):
    """The replacement answered on `down`'s first reading and hung before the second. Judged on
    the second alone, `down` found the recorded pid gone, printed "stopped" and deleted the record
    over a live proxy it had already seen."""
    _recorded_run(watchdog_pid=None)
    _status_network(monkeypatch)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    readings = iter([{"pid": 100}])  # then silence
    monkeypatch.setattr(api, "_health", lambda: next(readings, None))
    stopped = []

    def terminate(pid, marker):
        stopped.append(pid)
        return supervisor.Termination.NOT_RUNNING if pid in (None, 99) else supervisor.Termination.STILL_RUNNING

    monkeypatch.setattr(supervisor, "_terminate", terminate)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert 100 in stopped
    assert config.read_runtime()["proxyPid"] == 100


def test_down_writes_the_record_it_reconstructed_when_the_proxy_will_not_stop(profile, runner, monkeypatch):
    """No record on disk, so `down` works from health. When the proxy then survives, "the record
    is kept" has to be true: without a file the next `down` knows no pid, finds silence and prints
    "stopped" over the survivor."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    assert not config.runtime_file().exists()
    _status_network(monkeypatch)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    monkeypatch.setattr(api, "_health", lambda: {"pid": 99})
    outcomes = {99: supervisor.Termination.STILL_RUNNING}
    monkeypatch.setattr(
        supervisor, "_terminate", lambda pid, marker: outcomes.get(pid, supervisor.Termination.NOT_RUNNING)
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "the record is kept" in result.output
    assert config.read_runtime()["proxyPid"] == 99, "kept means on disk"


def test_watchdog_keeps_watching_when_its_successor_cannot_be_checked_and_the_proxy_changed(
    profile, runner, monkeypatch
):
    """The unverifiable-successor branch must end the decision. Falling through to the proxy-pid
    comparison retired the only watcher whenever a replacement proxy had taken the port."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime(
        {"proxyPid": 99, "service": "Wi-Fi", "watchdogPid": 100, "previousPac": {"url": "", "enabled": False}}
    )

    def ps_is_broken(pid, marker):
        raise supervisor.ProcessCheckError(f"`ps -p {pid}` failed: 1")

    monkeypatch.setattr(supervisor, "_identity_of", ps_is_broken)
    monkeypatch.setattr(api, "_health", lambda: {"pid": 101})  # a replacement proxy holds the port

    assert supervisor._should_retire() is False


def test_up_records_the_stopped_watchdog_before_the_restore_that_can_fail(profile, runner, monkeypatch):
    """Stopping the old service's watchdog is done; a restore that then fails keeps the record —
    and a record still naming that pid makes the next `up` refuse over a watchdog already gone."""
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True) if service == "Wi-Fi" else _ETHERNET_PAC,
        service="Ethernet",
    )
    config.write_runtime({**config.read_runtime(), "watchdogPid": 77})
    monkeypatch.setattr(supervisor, "_terminate", lambda pid, marker: supervisor.Termination.STOPPED)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: (_ for _ in ()).throw(netproxy.NetworkSetupError("boom")))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not restore the previous PAC on 'Wi-Fi'" in result.output
    assert "watchdogPid" not in config.read_runtime()


def test_up_does_not_claim_intercept_active_when_discovery_failed(profile, runner, monkeypatch):
    """Exit 1 was already right; the banner still said INTERCEPT ACTIVE about a service the
    command could only guess. Neither banner is honest there, so neither is printed."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(netproxy, "active_service", _discovery_times_out)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "INTERCEPT ACTIVE" not in result.output
    assert "NOT INTERCEPTING" not in result.output
    assert "ROUTE UNKNOWN" in result.output


def test_down_restores_the_network_when_the_record_cannot_be_written(profile, runner, monkeypatch):
    """The record is bookkeeping for the next `down`; a full disk must not stop this one. Raised,
    the failed write left the watchdog stopped and the PAC still pointing at the proxy."""
    _recorded_run()
    _status_network(monkeypatch)
    restored = []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(supervisor, "_terminate", lambda pid, marker: supervisor.Termination.STOPPED)

    def disk_full(data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(config, "write_runtime", disk_full)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert len(restored) == 1
    assert "could not update" in result.output
    assert not config.runtime_file().exists()


def test_down_stops_every_replacement_it_observed(profile, runner, monkeypatch):
    """Two readings, two different replacements — the first exited between them, the second is
    the one still there. Acting on the first alone found it gone and printed "stopped" over the
    second."""
    _recorded_run(watchdog_pid=None)
    _status_network(monkeypatch)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    readings = iter([{"pid": 100}, {"pid": 101}])
    monkeypatch.setattr(api, "_health", lambda: next(readings, None))
    stopped = []

    def terminate(pid, marker):
        stopped.append(pid)
        return supervisor.Termination.STILL_RUNNING if pid == 101 else supervisor.Termination.NOT_RUNNING

    monkeypatch.setattr(supervisor, "_terminate", terminate)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert stopped[-2:] == [100, 101]
    assert config.read_runtime()["proxyPid"] == 101


# MARK: - A record that cannot be read is not one that is absent


def test_down_leaves_another_state_directorys_proxy_alone(profile, runner, fake_network, monkeypatch):
    """Two state directories can run Lyrebird on one port in turn. This one's record is stale and
    corrupt; the proxy answering was started under the other, whose own record holds its previous
    PAC and whose own watchdog restores it. Stopping it from here — and disabling the routing it
    installed — used to be exactly what `down` did. It is not this Lyrebird's; identity says so."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    monkeypatch.setattr(api, "_health", lambda: {"pid": 4242, "profileFingerprint": config.PROFILE_FINGERPRINT})
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.THEIRS
    )  # another state root's argv

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "was not started by this Lyrebird" in result.output
    assert fake_network["restored"] is None and fake_network["terminated"] == []
    assert config.runtime_file().exists()


def test_up_refuses_to_start_over_a_record_it_cannot_read(profile, runner, monkeypatch):
    """The migration and the snapshot act on what the record says. Started over one that cannot
    be read, `up` snapshotted the network afresh and the previous PAC the file held was lost."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    config.runtime_file().write_bytes(b"{not json")
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("nothing may start"))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "cannot be read" in result.output and "nothing started" in result.output
    assert spawned == []
    assert config.runtime_file().read_bytes() == b"{not json"


def test_up_records_its_child_before_the_startup_wait(profile, runner, monkeypatch):
    """`up` spawned first and wrote the record only after the PAC was dealt with, so a child that
    outlived a failed startup — timeout, a check that failed — had no record naming it, and
    `down` could not find it. The pid goes into the existing record at once — asserted from
    inside the first health poll, so a write moved into the timeout branch fails here — and the
    previous PAC and watchdog it holds are kept."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus("", False, False))
    config.write_runtime({**config.read_runtime(), "watchdogPid": 77})

    spawned = []

    def silent_health():
        if spawned:  # the look before the spawn sees the crash record; every poll after it, the child
            assert config.read_runtime()["proxyPid"] == 4321, "recorded before the first poll, not after the wait"
        return None

    monkeypatch.setattr(api, "_health", silent_health)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + 13))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)

    class Survivor:
        pid = 4321

        def terminate(self):
            pass

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("mitmdump", timeout)

        def kill(self):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or Survivor())

    result = runner.invoke(cli.cli, ["up"])

    # A SystemExit, not an AssertionError from inside the poll masquerading as exit 1.
    assert isinstance(result.exception, SystemExit) and result.exit_code == 1, result.output
    assert config.read_runtime()["proxyPid"] == 4321, "recorded before the wait, so `down` can find it"
    assert config.read_runtime()["previousPac"] == _CORPORATE, "into the record that was there, not over it"
    assert config.read_runtime()["watchdogPid"] == 77


def test_status_reports_a_record_it_cannot_read(profile, runner, monkeypatch):
    """`status` read a corrupt record as absent and reported INTERCEPT ACTIVE with exit 0 — over a
    run whose `down` could not restore anything."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    _status_network(monkeypatch)
    monkeypatch.setattr(api, "_health", lambda: _health_payload())

    as_json = runner.invoke(cli.cli, ["status", "--json"])
    text = runner.invoke(cli.cli, ["status"])

    assert as_json.exit_code == 1
    assert "cannot be read" in json.loads(as_json.output)["runtimeError"]
    assert text.exit_code == 1
    assert "runtime record" in text.output and "cannot restore" in text.output


def test_watchdog_keeps_watching_over_a_record_it_cannot_read(profile, runner, monkeypatch):
    """Unreadable is not "down has been": the poll that cannot read the record neither retires nor
    repairs, and asks again."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    repaired = []
    monkeypatch.setattr(api, "_health", lambda: {"pid": 99})
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: repaired.append(service))

    def stop(seconds):
        raise _Stop

    monkeypatch.setattr(time, "sleep", stop)

    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])

    assert isinstance(result.exception, _Stop), "it must reach the sleep, not the return"
    assert repaired == [], "whose service this is could not be read; nothing is repaired on a guess"
    assert config.runtime_file().read_bytes() == b"not json at all\xff"


def test_watchdog_switches_ours_off_and_keeps_a_record_it_cannot_read(profile, runner, monkeypatch):
    """The proxy died and the record is unreadable: what to put back is unknown, that the Mac must
    not stay routed at a dead port is not. Ours goes off; the file stays for `down` and the person
    reading it. Before, the corrupt file was read as absent and deleted after a "restore"."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    restored = []
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert restored == [("Wi-Fi", "", False)]
    assert config.runtime_file().read_bytes() == b"not json at all\xff"


def test_down_touches_nothing_when_the_proxys_identity_cannot_be_checked(profile, runner, fake_network, monkeypatch):
    """Suppressed, a `ps` that failed let `down` disable another directory's PAC and only then
    report the pid as unverified. Whose proxy this is has to be known before anything is touched."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    monkeypatch.setattr(api, "_health", lambda: {"pid": 4242, "profileFingerprint": config.PROFILE_FINGERPRINT})
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)

    def ps_is_broken(pid, marker):
        raise supervisor.ProcessCheckError(f"`ps -p {pid}` failed: 1")

    monkeypatch.setattr(supervisor, "_identity_of", ps_is_broken)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "could not check whose proxy answers" in result.output and "nothing was changed" in result.output
    assert fake_network["restored"] is None and fake_network["terminated"] == []


def test_down_does_not_say_nothing_to_stop_over_a_record_it_cannot_read(profile, runner, monkeypatch):
    """Proxy and watchdog dead, Wi-Fi off, record corrupt: "nothing to stop — no runtime state"
    with exit 0 was said over a file that may hold the only copy of the previous PAC."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", lambda: None)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "nothing to stop" not in result.output
    assert "cannot be read" in result.output and "what the record holds is unknown" in result.output
    assert config.runtime_file().exists()


def test_up_stops_its_child_when_it_cannot_record_it(profile, runner, monkeypatch):
    """The first write exists so a child that outlives a failed startup can be found; a write that
    fails and is carried past leaves exactly the child it was meant to make findable."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus("", False, False))
    signalled = []

    class Child:
        pid = 4321

        def terminate(self):
            signalled.append("SIGTERM")

        def wait(self, timeout):
            return 0

        def poll(self):
            return 0 if signalled else None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Child())

    def disk_full(data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(config, "write_runtime", disk_full)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not record the proxy it started" in result.output
    assert signalled == ["SIGTERM"]
    assert "still running" not in result.output


def test_up_refuses_to_start_over_a_recorded_proxy_that_is_alive(profile, runner, monkeypatch):
    """The recorded proxy is alive with its control port hung, so health is silent. Started over
    it, the child lost the port race and died, the record was rewritten with the child's pid,
    and the survivor was forgotten — the next `down` found "nothing" and said stopped."""
    _up_after_a_crash(profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS
    )  # 99 is alive, and ours
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("nothing may start over a live proxy"))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "the recorded proxy (pid 99) is still running but not answering" in result.output
    assert config.read_runtime()["proxyPid"] == 99


def test_up_does_not_adopt_another_state_directorys_proxy(profile, runner, monkeypatch):
    """The same profile under another `LYREBIRD_STATE_DIR` answers health with the same
    fingerprint. Adopted, it went into this directory's record with a watchdog of its own, and
    this directory's `down` then refused it by identity — `up` had created state its own `down`
    could not clean."""
    _up_after_a_crash(
        profile, monkeypatch, lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True), health=[]
    )
    config.runtime_file().unlink()
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.THEIRS
    )  # its argv names another root
    spawned = []
    monkeypatch.setattr(supervisor, "_spawn_watchdog", lambda service: spawned.append(service) or 4242)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "the proxy already running on port 8088 (pid 4321) was not started by this Lyrebird" in result.output
    assert spawned == [] and not config.runtime_file().exists()


def test_down_does_not_say_stopped_when_no_pid_was_ever_known(profile, runner, fake_network, monkeypatch):
    """Corrupt record, health silent — a proxy with a hung control port looks exactly like none.
    Ours is switched off, nothing was signalled, and "proxy stopped" would be a claim about a
    process this command never identified."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    monkeypatch.setattr(api, "_health", lambda: None)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert fake_network["restored"] == ("Wi-Fi", "", False)
    assert "proxy stopped" not in result.output and "\nstopped" not in result.output
    assert "none was found running for port 8088" in result.output and "the file is kept" in result.output


def test_status_reports_the_proxys_own_unreadable_record(profile, runner, monkeypatch):
    """The same profile under another state directory answers here with its own record broken;
    this directory's record is fine — or absent — and `status` used to say all was well."""
    _status_network(monkeypatch)
    monkeypatch.setattr(
        api, "_health", lambda: _health_payload(runtimeError="/elsewhere/runtime-8088.json cannot be read: x")
    )

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["runtimeError"].startswith("/elsewhere/")


def test_up_refuses_a_proxy_that_took_the_port_during_startup(profile, runner, monkeypatch):
    """Silent at the first look, somebody else answers once the child is spawned: another `up`
    under another state directory — whose lock this one does not hold — won the port race, or a
    proxy with a hung control port woke up. The first non-null health used to be taken as the
    child's: recorded under the child's pid, given a PAC and a watchdog, INTERCEPT ACTIVE."""
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus("", False, False),
        health=[None, {**_LIVE, "pid": 7777, "profileFingerprint": config.PROFILE_FINGERPRINT}],
    )
    signalled, installed = [], []

    class Child:
        pid = 4321

        def terminate(self):
            signalled.append("SIGTERM")

        def wait(self, timeout):
            return 0

        def poll(self):
            return 0 if signalled else None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Child())
    monkeypatch.setattr(netproxy, "set_pac", lambda service: installed.append(service))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "another proxy (pid 7777) answered on port 8088 while this one was starting" in result.output
    assert signalled == ["SIGTERM"], "the child this `up` started is stopped again"
    assert installed == []


def test_up_fails_when_its_final_look_finds_another_proxy(profile, runner, monkeypatch):
    """The port changed hands between the start and the final look: whatever answers now is not
    what the PAC was installed for, and the banner used to say INTERCEPT ACTIVE about it."""
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True),
        health=[None, _LIVE, {**_LIVE, "pid": 8888}],
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "is now pid 8888, not 4321 — the port changed hands" in result.output
    assert "INTERCEPT ACTIVE" not in result.output


def test_down_refuses_a_health_without_a_pid(profile, runner, fake_network, monkeypatch):
    """A responder that is Lyrebird-shaped but names no pid cannot be checked for whose it is.
    Accepted, `down` changed the PAC and only then found the process still answering."""
    _recorded_run()
    monkeypatch.setattr(api, "_health", lambda: {"proxyUp": True})

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "answered without a pid" in result.output
    assert fake_network["restored"] is None and fake_network["terminated"] == []


def test_down_finds_a_silent_proxy_by_its_command_line(profile, runner, fake_network, monkeypatch):
    """Health cannot reach a proxy whose control port is hung, and "no health" used to mean "no
    proxy": the PAC was changed under it and nothing was signalled. Its command line still names
    this port, so the process table finds it, identity says it is ours, and it is stopped."""
    _recorded_run(watchdog_pid=None)
    config.runtime_file().unlink()
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(supervisor, "_proxies_on_port", lambda: [4321])
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert (4321, "addon.py") in fake_network["terminated"]


def test_down_refuses_a_silent_proxy_of_another_state_directory(profile, runner, fake_network, monkeypatch):
    """Corrupt record here, another directory's proxy on the port with its control port hung:
    with no pid from health, identity could not be checked and its PAC was disabled from here."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(supervisor, "_proxies_on_port", lambda: [4321])
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.THEIRS)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "a proxy found running on port 8088 (pid 4321) was not started by this Lyrebird" in result.output
    assert fake_network["restored"] is None and fake_network["terminated"] == []


def test_down_refuses_an_unmarked_proxy_its_record_does_not_name(profile, runner, fake_network, monkeypatch):
    """A proxy older than the identity tokens cannot say whose it is. Accepted on the marker
    alone, another directory's old proxy was stopped and its PAC disabled from a directory whose
    record was corrupt. Only a readable record naming its pid can vouch for it."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    monkeypatch.setattr(api, "_health", lambda: {"pid": 4242, "profileFingerprint": config.PROFILE_FINGERPRINT})
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.UNMARKED)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "predates Lyrebird's identity tokens and this directory's record does not name it" in result.output
    assert fake_network["restored"] is None and fake_network["terminated"] == []


def test_down_accepts_an_unmarked_proxy_its_record_names(profile, runner, fake_network, monkeypatch):
    """The readable record is what vouches for a pre-token proxy: an upgrade must still be able
    to stop the proxy the previous version started."""
    _recorded_run(watchdog_pid=None)
    health_until_terminated(monkeypatch, fake_network, pid=99)
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.UNMARKED)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert (99, "addon.py") in fake_network["terminated"]


def test_up_fails_when_its_final_look_reports_a_broken_record(profile, runner, monkeypatch):
    """The proxy reads the record too. Its final health saying the record is unreadable means
    the `down` this `up` promises cannot restore from it; `up` used to print INTERCEPT ACTIVE
    over that and exit 0, while `status` on the same health exits 1."""
    _up_after_a_crash(
        profile,
        monkeypatch,
        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True),
        health=[None, _LIVE, {**_LIVE, "runtimeError": "runtime-8088.json cannot be read: x"}],
    )

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "the proxy reports its runtime record unreadable" in result.output


def test_watchdog_keeps_the_record_of_a_proxy_that_is_alive_but_silent(profile, runner, monkeypatch):
    """Health silent is what the death path sees — and what a live proxy with a hung control
    port looks like. Restoring the PAC is right either way; deleting the record was not: it
    named the survivor, and `down` then had nothing left to stop it by. The restore obligation
    is dropped, the pid kept."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 4321, "service": "Wi-Fi", "previousPac": _CORPORATE})
    restored = []
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert restored == [("Wi-Fi", _CORPORATE["url"], _CORPORATE["enabled"])]
    assert config.read_runtime() == {"proxyPid": 4321, "service": "Wi-Fi"}


def test_down_stops_a_silent_proxy_even_when_the_record_names_a_stale_pid(profile, runner, fake_network, monkeypatch):
    """The record names dead pid 99; the scan finds a live, silent, verified proxy 4321. Used
    only when the record named nothing, the scan's answer was checked and then dropped: `down`
    stopped 99, deleted the record and printed "stopped" over 4321."""
    _recorded_run(watchdog_pid=None)
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(supervisor, "_proxies_on_port", lambda: [4321])
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert (4321, "addon.py") in fake_network["terminated"]


def test_down_gates_a_proxy_first_seen_on_its_second_reading(profile, runner, fake_network, monkeypatch):
    """A pre-token proxy from another directory that timed out on the first health reading and
    answers the second. Recorded and signalled straight away, the record `down` had just written
    was what appeared to vouch for it. Every candidate passes the gate against the *original*
    record."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    readings = iter([None, {"pid": 4321, "profileFingerprint": config.PROFILE_FINGERPRINT}])
    monkeypatch.setattr(api, "_health", lambda: next(readings, None))
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.UNMARKED)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "predates Lyrebird's identity tokens" in result.output and "it is left running" in result.output
    assert (4321, "addon.py") not in fake_network["terminated"]


def test_down_stops_a_silent_proxy_when_there_is_no_route(profile, runner, fake_network, monkeypatch):
    """No record, health silent, Wi-Fi off — and the scan finds this directory's proxy. "Nothing
    to stop" was decided from health and the record alone, so the proxy it had just found and
    verified was left running under an exit 0."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(api, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", lambda: None)
    monkeypatch.setattr(supervisor, "_proxies_on_port", lambda: [4321])
    monkeypatch.setattr(supervisor, "_identity_of", lambda pid, marker: supervisor.Identity.OURS)

    result = runner.invoke(cli.cli, ["down"])

    assert "nothing to stop" not in result.output
    assert (4321, "addon.py") in fake_network["terminated"]
