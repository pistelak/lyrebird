"""Teardown behaviour.

`down` is the command that has to work when everything else has gone wrong — the proxy crashed,
the runtime file is unreadable, the machine was rebooted mid-session. If it silently does nothing,
the user is left with a PAC pointing at a dead port and no indication why.
"""


import json
import os
import subprocess
import threading

import click
import pytest
from click.testing import CliRunner

import cli
import config
import netproxy
import rules


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fake_network(monkeypatch):
    """A Wi-Fi service whose PAC is currently ours and enabled."""
    state = {"service": "Wi-Fi", "restored": None, "terminated": []}

    monkeypatch.setattr(netproxy, "active_service", lambda: state["service"])
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))

    def restore(service, url, enabled):
        state["restored"] = (service, url, enabled)

    monkeypatch.setattr(netproxy, "restore_pac", restore)
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: state["terminated"].append((pid, marker)))
    return state


def health_until_terminated(monkeypatch, state, pid=4242):
    """Health answers until the proxy is terminated, then stops — as a real proxy does."""
    monkeypatch.setattr(cli, "_health",
                        lambda: None if state["terminated"] else {"pid": pid})


def test_down_recovers_when_the_runtime_file_is_unreadable(profile, runner, fake_network, monkeypatch):
    """Regression: a corrupt runtime file made `down` find nothing to do and report "stopped"
    while the proxy was still running and the PAC still pointing at it. Verified against a real
    proxy before this test existed."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    health_until_terminated(monkeypatch, fake_network)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert fake_network["restored"] is not None, "the PAC must be restored even with no runtime state"
    assert (4242, "addon.py") in fake_network["terminated"], "the pid must come from health"


def test_down_uses_the_runtime_file_when_it_is_readable(profile, runner, fake_network, monkeypatch):
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "watchdogPid": 98, "service": "Wi-Fi",
                          "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": True}})
    health_until_terminated(monkeypatch, fake_network, pid=99)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert fake_network["restored"] == ("Wi-Fi", "http://proxy.example.com/corp.pac", True)
    assert (98, "_watchdog") in fake_network["terminated"]
    assert not config.runtime_file().exists()


def test_down_says_so_when_there_is_nothing_to_stop(profile, runner, monkeypatch):
    """Better than claiming success: the user needs to know their PAC was not touched."""
    monkeypatch.setattr(cli, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", lambda: None)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert "nothing to stop" in result.output


def test_down_leaves_a_foreign_pac_alone(profile, runner, monkeypatch):
    """A PAC the user set by hand mid-session must survive teardown."""
    touched = []
    stopped = {"terminated": []}
    monkeypatch.setattr(cli, "_health", lambda: None if stopped["terminated"] else {"pid": 1})
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus("http://proxy.example.com/corp.pac", True, False))
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: touched.append(a))
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: stopped["terminated"].append(pid))

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert touched == [], "a PAC that is not ours must never be rewritten"
    assert "not ours" in result.output




def _unreadable_pac(service):
    raise netproxy.NetworkSetupError("`networksetup -getautoproxyurl Wi-Fi` failed: 1")


def test_down_refuses_to_claim_left_untouched_when_the_pac_cannot_be_read(profile, runner, monkeypatch):
    """A failed `networksetup` used to parse as "no PAC, not ours", so `down` printed "left
    untouched", deleted the runtime file, and the PAC it never read stayed pointing at a dead port."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi",
                          "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": True}})
    touched = []
    monkeypatch.setattr(cli, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "pac_status", _unreadable_pac)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: touched.append(a))
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: None)

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
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi",
                          "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": False}})
    restored = []
    stopped = {"terminated": []}
    monkeypatch.setattr(cli, "_health", lambda: None if stopped["terminated"] else {"pid": 99})
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus("http://proxy.example.com/corp.pac", True, False))
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: stopped["terminated"].append(pid))

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert restored == [("Wi-Fi", "http://proxy.example.com/corp.pac", False)]
    assert "not ours" not in result.output


def test_up_says_when_it_cannot_read_the_pac_it_just_installed(profile, runner, monkeypatch):
    """The banner's "PAC is disabled/not ours" is a diagnosis; a read that failed made none.
    And the failure summary only names what was printed as it happened, so this must be."""
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "_health", lambda: {"pid": 1, "activeSession": "default", "sessions": ["default"],
                                                "overrideCount": 0, "proxyPort": 8080})
    monkeypatch.setattr(cli, "trust_ca_in_sim", lambda simulator: (True, "trusted"))
    fake_simctl(monkeypatch, [_PHONE])
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus("", False, False))
    monkeypatch.setattr(netproxy, "set_pac", lambda service: None)
    monkeypatch.setattr(cli, "_pid_is_ours", lambda pid, marker: True)
    monkeypatch.setattr(netproxy, "intercepting", _unreadable_pac)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not read the PAC" in result.output
    assert "not ours" not in result.output


def test_watchdog_keeps_the_runtime_file_when_the_restore_keeps_failing(profile, runner, monkeypatch):
    """The watchdog used to suppress the failure and delete the file anyway, which made a
    failed restore permanent and invisible: nothing was left for `down` to act on. And it must
    try more than once: `networksetup` fails transiently during the network changes that kill
    proxies in the first place."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    attempts = []
    monkeypatch.setattr(cli, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "pac_status", lambda service: (attempts.append(1), _unreadable_pac(service)))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert len(attempts) == cli._WATCHDOG_RESTORE_ATTEMPTS
    assert config.runtime_file().exists()


def test_watchdog_restores_under_the_lock_up_takes_and_looks_again_once_it_holds_it(profile, runner, monkeypatch):
    """`up` reuses a running watchdog. A replacement that starts after the watchdog's first look
    but before its restore would have its PAC restored over and its runtime file deleted — so the
    restore happens under `up`'s lock, and health is checked again once the lock is held."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    health = iter([None])   # dead at the first look; live by the time the lock is held
    locked = []
    restored = []

    class Stop(Exception):
        pass

    def flock(handle, operation):
        locked.append(operation)

    def sleep(seconds):
        raise Stop   # the first poll delay: by then it must be watching again, not restoring

    monkeypatch.setattr(cli, "_health", lambda: next(health, {"pid": 2}))
    monkeypatch.setattr(cli.fcntl, "flock", flock)
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(cli.time, "sleep", sleep)

    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])

    assert isinstance(result.exception, Stop), result.output
    assert locked and set(locked) == {cli.fcntl.LOCK_EX}, "the restore must wait for any `up` in progress"
    assert restored == [], "the replacement owns the network now"
    assert config.runtime_file().exists(), "the replacement's runtime file must survive"


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

    monkeypatch.setattr(cli, "_health", lambda: {"pid": 99})
    monkeypatch.setattr(netproxy, "pac_status", lambda service: pac)
    monkeypatch.setattr(netproxy, "set_pac", lambda service: repaired.append(service))
    monkeypatch.setattr(cli.time, "sleep", stop)
    return repaired


def test_watchdog_re_enables_its_own_service_pac_when_macos_switches_it_off(profile, runner, monkeypatch):
    repaired = _watch_one_poll(monkeypatch, recorded_service="Wi-Fi",
                               pac=netproxy.PacStatus(netproxy.pac_url(), False, True))
    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])
    assert isinstance(result.exception, _Stop)
    assert repaired == ["Wi-Fi"]


def test_watchdog_leaves_a_pac_alone_once_the_route_has_moved_to_another_service(profile, runner, monkeypatch):
    """Restoring "no previous PAC" on Wi-Fi leaves our URL installed and disabled — the shape
    the live loop exists to undo. A Wi-Fi watchdog that outlived the move to Ethernet used to
    switch it straight back on, and the next `down` restored Ethernet only."""
    repaired = _watch_one_poll(monkeypatch, recorded_service="Ethernet",
                               pac=netproxy.PacStatus(netproxy.pac_url(), False, True))
    result = runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"])
    assert isinstance(result.exception, _Stop)
    assert repaired == [], "the record names another service: not this watchdog's PAC to touch"


def test_up_waits_for_the_lock_holder_and_gives_up_only_after_the_timeout(profile, monkeypatch):
    """The holder is another `up`, or the watchdog restoring the network after a crash. Failing at
    once used to say "another up is in progress" while a restore ran; starting anyway would
    interleave an install with that restore on one network service."""
    import fcntl

    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    with open(config.lock_file(), "w") as holder, open(config.lock_file(), "w") as waiter:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="still in progress"):
            cli._acquire_lock(waiter, timeout=0.2)
        fcntl.flock(holder, fcntl.LOCK_UN)
        cli._acquire_lock(waiter, timeout=0.2)   # released: acquired without error


_CORPORATE = {"url": "http://proxy.example.com/corp.pac", "enabled": False}


# Devices with the shape `simctl list devices --json` reports, and nothing that could reach a real
# one: every simctl call goes through `cli._run`, which `fake_simctl` replaces wholesale. The ids
# are short and readable rather than real-looking UUIDs — nothing parses them, they are only
# compared — but they keep upper case, so `--simulator phone-1` still tests a case-insensitive
# match. A device's `runtime` names the bucket it is listed under, which is the only place simctl
# says what platform it is; these are shaped like the real identifiers so the same parse is run.
_IOS_RUNTIME = "com.example.SimRuntime.iOS-18-0"
_WATCH_RUNTIME = "com.example.SimRuntime.watchOS-11-0"

_PHONE = {"udid": "PHONE-1", "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}
_PAD = {"udid": "PAD-1", "name": "iPad Pro 13-inch", "state": "Booted", "isAvailable": True}
# The one that boots alongside a phone without anybody asking for it.
_WATCH = {"udid": "WATCH-1", "name": "Apple Watch Series 11",
          "state": "Booted", "isAvailable": True, "runtime": _WATCH_RUNTIME}


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
            return subprocess.CompletedProcess(args, keychain_status, "",
                                               "" if keychain_status == 0 else "keychain failed")
        return subprocess.CompletedProcess(args, launch_status, "",
                                           "" if launch_status == 0 else "failed to launch")

    monkeypatch.setattr(cli, "_run", run)
    return calls


def simctl_calls(calls, subcommand):
    return [call for call in calls if subcommand in call]


_LIVE = {"pid": 4321, "activeSession": "default", "sessions": ["default"], "overrideCount": 0,
         "proxyPort": 8080}


def _up_after_a_crash(profile, monkeypatch, pac_status, *, service="Wi-Fi", health=None,
                      stub_trust=True):
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

    monkeypatch.setattr(cli, "_health", lambda: next(answers, _LIVE))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(cli, "_start_fresh_log", lambda: None)
    if stub_trust:
        monkeypatch.setattr(cli, "trust_ca_in_sim", lambda simulator: (True, "trusted"))
    # One booted simulator, so device resolution succeeds without reading this machine's. A test
    # about several booted devices calls `fake_simctl` again with its own list.
    fake_simctl(monkeypatch, [_PHONE])
    monkeypatch.setattr(netproxy, "active_service", lambda: service)
    installed = {"ours": False}   # `set_pac` installs ours, and every read after it sees that

    def read(service):
        if installed["ours"]:
            return netproxy.PacStatus(netproxy.pac_url(), True, True)
        return pac_status(service)

    def install(service):
        installed["ours"] = True

    monkeypatch.setattr(netproxy, "pac_status", read)
    monkeypatch.setattr(netproxy, "set_pac", install)
    monkeypatch.setattr(cli, "_pid_is_ours", lambda pid, marker: True)
    monkeypatch.setattr(cli, "_spawn_watchdog", lambda service: 4242)


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
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True), service=None)
    assert runner.invoke(cli.cli, ["up"]).exit_code == 0
    assert config.read_runtime()["service"] == "Wi-Fi"
    assert config.read_runtime()["previousPac"] == _CORPORATE

    restored = []
    stopped = {"terminated": []}
    monkeypatch.setattr(cli, "_health", lambda: None if stopped["terminated"] else _LIVE)
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: stopped["terminated"].append(pid))

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert restored == [("Wi-Fi", _CORPORATE["url"], False)]


def test_up_fails_when_its_final_look_finds_the_pac_not_routing(profile, runner, monkeypatch):
    """Every step succeeded and then the PAC was switched off before the last look. The banner
    said NOT INTERCEPTING; the exit code said 0, because only the steps fed it."""
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    monkeypatch.setattr(netproxy, "intercepting", lambda service: False)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "NOT INTERCEPTING" in result.output


def test_up_fails_when_the_proxy_stops_answering_before_the_final_look(profile, runner, monkeypatch):
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True),
                      health=[None, _LIVE, None])   # dead at first, up for the wait, gone at the end

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "stopped answering" in result.output


_ETHERNET_PAC = netproxy.PacStatus("http://proxy.example.org/eth.pac", True, False)


def test_up_restores_the_old_service_before_switching_to_a_new_one(profile, runner, monkeypatch):
    """Crash on Wi-Fi, failed restore, then Ethernet becomes the route and `up` runs. Writing a
    record for Ethernet over Wi-Fi's used to leave Wi-Fi's PAC pointing at the dead proxy with
    nothing left to say so — `down` restored Ethernet, deleted the file, and called it stopped."""
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True)
                      if service == "Wi-Fi" else _ETHERNET_PAC,
                      service="Ethernet")
    restored = []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: restored.append(a))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert restored == [("Wi-Fi", _CORPORATE["url"], False)]
    runtime = config.read_runtime()
    assert runtime["service"] == "Ethernet"
    assert runtime["previousPac"] == {"url": _ETHERNET_PAC.url, "enabled": True}


def test_up_keeps_the_record_and_fails_when_the_old_service_cannot_be_restored(profile, runner, monkeypatch):
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: _unreadable_pac(service) if service == "Wi-Fi" else _ETHERNET_PAC,
                      service="Ethernet")

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert "could not restore the previous PAC on 'Wi-Fi'" in result.output
    runtime = config.read_runtime()
    assert runtime["service"] == "Wi-Fi" and runtime["previousPac"] == _CORPORATE


def test_up_replaces_a_watchdog_that_watches_another_service(profile, runner, monkeypatch):
    """A watchdog is told its service on the command line. Reusing one from a run on Wi-Fi for a
    run on Ethernet had it restore Wi-Fi's record and then delete Ethernet's."""
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True)
                      if service == "Wi-Fi" else _ETHERNET_PAC,
                      service="Ethernet")
    config.write_runtime({**config.read_runtime(), "watchdogPid": 77})
    terminated, spawned = [], []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: terminated.append((pid, marker)))
    monkeypatch.setattr(cli, "_spawn_watchdog", lambda service: spawned.append(service) or 4242)

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
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True)
                      if service == "Wi-Fi" else _ETHERNET_PAC,
                      service="Ethernet")
    config.write_runtime({**config.read_runtime(), "watchdogPid": 77})
    terminated = []
    monkeypatch.setattr(netproxy, "restore_pac", lambda *a: None)
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: terminated.append((pid, marker)))
    monkeypatch.setattr(netproxy, "set_pac", _unreadable_pac)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1 and "could not install" in result.output
    assert (77, "_watchdog") in terminated, "retired before Ethernet's record existed"
    ethernet = {"url": _ETHERNET_PAC.url, "enabled": True}
    assert config.read_runtime()["previousPac"] == ethernet

    # And even a watchdog that somehow survived must not act on another service's record.
    monkeypatch.setattr(cli, "_health", lambda: None)
    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert config.read_runtime()["previousPac"] == ethernet


def test_up_keeps_the_recovery_record_of_a_half_restored_pac(profile, runner, monkeypatch):
    """A restore that failed between its two calls has handed the corporate URL back but left it
    enabled. `down` already treats that as still Lyrebird's to finish; `up` must not read it as
    somebody else's PAC and snapshot the wrong flag as the thing to restore."""
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: netproxy.PacStatus(_CORPORATE["url"], True, False))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert config.read_runtime()["previousPac"] == _CORPORATE


def test_watchdog_restores_on_a_later_attempt_after_a_transient_failure(profile, runner, fake_network, monkeypatch):
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    readings = iter([None])   # the first read fails, the second sees our PAC

    def flaky(service):
        if next(readings, "ok") is None:
            _unreadable_pac(service)
        return netproxy.PacStatus(netproxy.pac_url(), True, True)

    monkeypatch.setattr(cli, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "pac_status", flaky)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    assert runner.invoke(cli.cli, ["_watchdog", "Wi-Fi"]).exit_code == 0
    assert fake_network["restored"] == ("Wi-Fi", "", False)
    assert not config.runtime_file().exists()


def test_watchdog_clears_the_runtime_file_after_restoring(profile, runner, fake_network, monkeypatch):
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 99, "service": "Wi-Fi", "previousPac": {"url": "", "enabled": False}})
    monkeypatch.setattr(cli, "_health", lambda: None)

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
    config.write_runtime({"proxyPid": 99, "watchdogPid": 98, "service": "Wi-Fi",
                          "previousPac": {"url": "", "enabled": False}})
    events = []
    health_until_terminated(monkeypatch, fake_network, pid=99)
    monkeypatch.setattr(cli, "_acquire_lock", lambda lock, timeout=0: events.append("lock"))
    monkeypatch.setattr(cli, "_terminate", lambda pid, marker: events.append(f"terminate {marker}")
                        or fake_network["terminated"].append((pid, marker)))
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
    config.write_runtime({"proxyPid": 99, "watchdogPid": 98, "service": "Wi-Fi",
                          "previousPac": {"url": "http://proxy.example.com/corp.pac", "enabled": True}})

    state = {"url": netproxy.pac_url(), "enabled": False}   # ours, but macOS switched it off
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

    monkeypatch.setattr(netproxy, "pac_status", pac_status)
    monkeypatch.setattr(netproxy, "set_pac", set_pac)
    monkeypatch.setattr(netproxy, "restore_pac", restore_pac)
    monkeypatch.setattr(cli, "_terminate", terminate)
    monkeypatch.setattr(cli, "_health",
                        lambda: None if "down:terminate addon.py" in events else {"pid": 99})

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
            except Exception as error:   # re-raised on the main thread once both are joined
                failures.append(error)

        return threading.Thread(target=run, name=name)

    # Exactly one Click invocation runs at a time, on the `down` thread; nothing else touches
    # Click or global I/O while it runs, and it is joined before the fixtures tear down.
    repair_thread = worker("repair", lambda: cli._repair_pac("Wi-Fi"))
    down_thread = worker("down", lambda: result.update(down=CliRunner().invoke(cli.cli, ["down"])))

    repair_thread.start()
    try:
        assert repair_holds_lock.wait(timeout=10), "the repair never reached the lock"
        down_thread.start()
        assert down_is_waiting.wait(timeout=5), "`down` never waited on the lock the repair holds"
        assert [event for event in events if event.startswith("down:")] == [], \
            "`down` signalled the watchdog or touched the PAC while the repair held the lock"
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

    assert events == ["repair:pac_status", "repair:set_pac", "down:terminate _watchdog",
                      "down:pac_status", "down:restore", "down:terminate addon.py"], \
        "the repair must finish before `down` reads the PAC it is about to restore"
    assert state == {"url": "http://proxy.example.com/corp.pac", "enabled": True}
    assert not config.runtime_file().exists()
    assert result["down"].exit_code == 0, result["down"].output
    assert "stopped" in result["down"].output

    # The other half of the guarantee: a repair entering afterwards finds no runtime record
    # naming its service, and returns before it reads — let alone writes — the network.
    def must_not_be_called(service):
        raise AssertionError("pac_status must not be called after `down`")

    monkeypatch.setattr(netproxy, "pac_status", must_not_be_called)
    cli._repair_pac("Wi-Fi")
    assert len(events) == 6, "a repair that arrived after `down` did something"
    assert state == {"url": "http://proxy.example.com/corp.pac", "enabled": True}


def test_down_reports_a_proxy_that_did_not_stop(profile, runner, fake_network, monkeypatch):
    """SIGTERM is a request. Printing "stopped" over a proxy that is still serving is the lie this
    check exists to prevent."""
    monkeypatch.setattr(cli, "_health", lambda: {"pid": 4242})   # never dies
    monkeypatch.setattr(cli, "_DOWN_WAIT_SECONDS", 0.3)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "still responding" in result.output


# `status` is a query, but its exit code is a claim about the machine, and an agent acts on it.
# The pair below exists because the two output formats once disagreed: --json exited 1 when
# nothing was being intercepted while the human form exited 0, so `lyrebird status && …` ran
# happily against a proxy that was mocking nothing.

@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_fails_when_not_intercepting_in_either_format(profile, runner, monkeypatch, args):
    monkeypatch.setattr(cli, "_health", lambda: None)
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))

    assert runner.invoke(cli.cli, ["status", *args]).exit_code == 1


@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_succeeds_only_when_up_and_intercepting(profile, runner, monkeypatch, args):
    monkeypatch.setattr(cli, "_health",
                        lambda: {"pid": 1, "sessions": ["default"], "activeSession": "default",
                                 "overrideCount": 0, "simBundleId": None, "proxyPort": 8080})
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))

    assert runner.invoke(cli.cli, ["status", *args]).exit_code == 0


@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_reports_an_unreadable_pac_as_unproven_not_off(profile, runner, monkeypatch, args):
    """Same exit code as "off", different explanation: a person told DISABLED goes and switches it
    on, which is not the fix for a `networksetup` that is failing."""
    monkeypatch.setattr(cli, "_health",
                        lambda: {"pid": 1, "sessions": ["default"], "activeSession": "default",
                                 "overrideCount": 0, "simBundleId": None, "proxyPort": 8080})
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
    monkeypatch.setattr(cli, "_health",
                        lambda: {"pid": 1, "sessions": [], "activeSession": None,
                                 "overrideCount": 0, "simBundleId": None})
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), False, True))

    assert runner.invoke(cli.cli, ["status"]).exit_code == 1


# `/health` is unscoped, so the proxy holding the control port may belong to another profile. The
# three below pin the comparison `up` already makes: an enabled, owned PAC plus a proxy that is
# intercepting is *not* this profile's interception unless the fingerprints agree.

def _status_health(fingerprint=None):
    payload = {"pid": 1, "sessions": ["default", "orders-outage"], "activeSession": "orders-outage",
               "overrideCount": 3, "simBundleId": "com.example.Store", "proxyPort": 8080}
    if fingerprint is not None:
        payload["profileFingerprint"] = fingerprint
    return lambda: payload


@pytest.mark.parametrize("args", [[], ["--json"]])
def test_status_refuses_a_foreign_profiles_interception_in_either_format(profile, runner,
                                                                        monkeypatch, args):
    """Regression: the PAC on this port is "ours" whoever started the proxy behind it, so a proxy
    running profile A made `lyrebird --profile B status` print INTERCEPT ACTIVE and exit 0 — and
    `lyrebird status && …` went on to drive a proxy mocking someone else's hosts and sessions."""
    monkeypatch.setattr(cli, "_health", _status_health("feedfacef00d"))
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
        # this profile has no sessions.
        assert payload["sessions"] is None and payload["activeSession"] is None
        assert payload["overrideCount"] is None and payload["simBundleId"] is None
    else:
        assert "INTERCEPT ACTIVE" not in result.output
        # Both fingerprints, because "a different profile" alone does not say which is which.
        assert "feedfacef00d" in result.output and config.PROFILE_FINGERPRINT in result.output
        assert "lyrebird down" in result.output, "the remedy, and it is not `up` — `up` refuses"
        assert "LYREBIRD_CONTROL_PORT" in result.output, "the other way out: aim at another port"
        assert "lyrebird up" not in result.output
        assert "orders-outage" not in result.output, "another profile's session, reported as ours"


def test_status_reports_interception_when_the_fingerprints_agree(profile, runner, monkeypatch):
    """The other half: the check compares fingerprints, it does not just distrust the field."""
    monkeypatch.setattr(cli, "_health", _status_health(config.PROFILE_FINGERPRINT))
    _status_network(monkeypatch)

    result = runner.invoke(cli.cli, ["status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["profileMismatch"] is False and payload["intercepting"] is True
    assert payload["activeSession"] == "orders-outage"


def test_status_trusts_an_engine_too_old_to_send_a_fingerprint(profile, runner, monkeypatch):
    """A proxy from before the field existed cannot say whose it is, and `up` accepts that reading
    too. Treating silence as a mismatch would break `status` against every running older engine."""
    monkeypatch.setattr(cli, "_health", _status_health())
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
        result = subprocess.run([sys.executable, str(config.ROOT / "cli.py"), *args],
                                capture_output=True, text=True, env=env, check=False)
        assert result.returncode == 0, f"{args}: {result.stderr}"
        assert "(no log)" in result.stdout, f"{args}: {result.stdout!r}"


def test_down_does_not_need_a_readable_profile(profile, runner, monkeypatch):
    """`down` restores the network from runtime state and the live API. A broken profile.json is
    the kind of thing you are trying to recover from, not a reason to be stuck."""
    (profile / "profile.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cli, "_health", lambda: None)
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
    monkeypatch.setattr(cli, "_health", must_not_start)

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
        cli._start_fresh_log()
        with open(config.LOG_FILE, "a", encoding="utf-8") as sink:
            sink.write("a host and a path\n")
        overheard = reader.read()

    assert config.LOG_FILE.stat().st_ino != stale_inode, "the lax inode was truncated, not replaced"
    assert config.LOG_FILE.stat().st_mode & 0o777 == 0o600
    assert overheard == "", f"a reader of the old log still saw traffic: {overheard!r}"


# MARK: - Sequence commands
#
# `sequence wait` is the command an agent leans on to prove a transition happened, so its failure
# modes matter more than its happy path: a wait that hangs for its full timeout on something that
# already happened sends the operator looking in the wrong place.

_BASE_SEQ = {"id": "ovr_a", "runId": "r1", "advanceOn": "self", "nextStep": 1, "stepCount": 2,
             "exhausted": False, "hasOverrun": False, "serves": {}}


def _health_payload(**extra):
    """The envelope every health response carries, so a double cannot pin a shape the API never
    sends — and so a command that starts reading another field fails here rather than passing
    against a payload that omitted it."""
    return {"pid": 1, "sessions": ["default"], "activeSession": "default",
            "overrideCount": 1, "simBundleId": None, "proxyPort": 8080,
            "profileFingerprint": config.PROFILE_FINGERPRINT,
            "sequences": [], "answers": [], **extra}


def _polling(states, build):
    """Live state that moves under the poll: each call serves the next state, the last repeats."""
    queue = list(states)

    def payload():
        return build(queue.pop(0) if len(queue) > 1 else queue[0])
    return payload


def _health_over(*states):
    """Sequence state under the poll. `None` for a state means the rule is gone from the session."""
    return _polling(states, lambda state: _health_payload(
        sequences=[] if state is None else [{**_BASE_SEQ, **state}]))


def _health_with(**state):
    return _health_over(state)


def test_reset_names_what_it_rewound(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_control",
                        lambda *a, **k: {"session": "default", "reset": {"ovr_a": "abc123"}})
    result = runner.invoke(cli.cli, ["reset"])
    assert result.exit_code == 0
    assert "ovr_a" in result.output
    assert "abc123" in result.output, "the run id is what `assert-answered --run` is given"


def test_reset_json_hands_back_the_run_id_to_assert_with(profile, runner, monkeypatch):
    """The boundary has to be retainable by a script, not just readable by a person: an id that
    only exists inside a coloured line is an id no test harness can pass to the assertion."""
    monkeypatch.setattr(cli, "_control",
                        lambda *a, **k: {"session": "default", "reset": {"ovr_a": "abc123"}})
    result = runner.invoke(cli.cli, ["reset", "ovr_a", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"session": "default", "reset": {"ovr_a": "abc123"}}


def test_reset_json_reports_an_empty_reset_as_an_empty_map(profile, runner, monkeypatch):
    """`--json` decides how this is printed and nothing else — same exit, same meaning, and an
    empty map is a real answer rather than the absence of one."""
    monkeypatch.setattr(cli, "_control", lambda *a, **k: {"session": "default", "reset": {}})
    result = runner.invoke(cli.cli, ["reset", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"session": "default", "reset": {}}


def test_reset_says_so_when_there_is_nothing_to_rewind(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_control", lambda *a, **k: {"session": "default", "reset": {}})
    result = runner.invoke(cli.cli, ["reset"])
    assert result.exit_code == 0
    assert "nothing to reset" in result.output


def test_sequence_wait_rejects_an_unknown_sequence(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", lambda: {"sequences": []})
    result = runner.invoke(cli.cli, ["sequence", "wait", "nope", "--step", "1"])
    assert result.exit_code == 1
    assert "no sequence" in result.output


def test_sequence_wait_rejects_a_step_that_does_not_exist(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _health_with())
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "5"])
    assert result.exit_code == 1
    assert "out of range" in result.output


def test_sequence_wait_fails_at_once_when_the_step_already_passed(profile, runner, monkeypatch):
    """This has to come from live state, not the traffic buffer: /recent holds a bounded window, so
    the event may be long evicted while the fact that it happened is still true."""
    monkeypatch.setattr(cli, "_health", _health_with(nextStep=None, exhausted=True))
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "30"])
    assert result.exit_code == 1
    assert "already past" in result.output
    assert "reset ovr_a" in result.output, "say how to fix it"


SERVED = {"sequenceId": "ovr_a", "runId": "r1", "selectedStep": 1, "stepCount": 2,
          "method": "GET", "path": "/api/items", "status": 200,
          "time": "2026-08-16T10:00:00+00:00"}


def test_sequence_wait_succeeds_when_the_step_is_served(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _health_over({}, {"serves": {"1": 1}}))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [SERVED])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 0
    assert "served step 1/2" in result.output
    assert "/api/items" in result.output, "the /recent detail when the entry is still there"


def test_sequence_wait_survives_the_serve_being_evicted_from_recent(profile, runner, monkeypatch):
    """The failure that motivated the serve counter: /recent is a 200-entry window, so under enough
    traffic the serve's entry is gone before the next poll — and a wait reading only the traffic
    buffer timed out on something that happened. The counter in live state cannot be evicted."""
    monkeypatch.setattr(cli, "_health", _health_over({}, {"serves": {"1": 1}}))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 0
    assert "served step 1/2" in result.output


def test_sequence_wait_succeeds_at_once_when_the_step_was_already_served_this_run(profile, runner, monkeypatch):
    """The serve counter lives in the runtime entry a reset drops, so everything in it happened
    after the last reset — it IS the postcondition, verified. The documented workflow is
    reset → trigger → wait, and when the action lands before the wait starts, refusing or timing
    out would report failure on a transition that completed."""
    monkeypatch.setattr(cli, "_health", _health_with(advanceOn="match", serves={"1": 3}))
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "30"])
    assert result.exit_code == 0
    assert "served step 1/2 this run (3×, before the wait began)" in result.output


def test_sequence_wait_fails_fast_when_the_run_advances_past_the_step_without_serving_it(profile, runner, monkeypatch):
    """Two advance requests can march the cursor over the awaited step while an `advanceOn` rule
    serves nothing. That wait can never be satisfied, and burning the rest of the timeout would
    blame the sequence for not serving rather than the traffic for advancing it."""
    monkeypatch.setattr(cli, "_health", _health_over({}, {"nextStep": None, "exhausted": True}))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "30"])
    assert result.exit_code == 1
    assert "advanced past step 1 without serving it" in result.output


def test_sequence_wait_survives_a_transient_health_failure(profile, runner, monkeypatch):
    """One dropped health poll must read as "could not check right now", not "the rule is gone" —
    those are different claims, and the second one exits the wait."""
    responses = [_health_with()(), None, _health_with(serves={"1": 1})()]
    monkeypatch.setattr(cli, "_health", lambda: responses.pop(0) if len(responses) > 1 else responses[0])
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 0
    assert "served step 1/2" in result.output


def test_sequence_wait_says_so_when_the_control_api_is_down(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", lambda: None)
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 1
    assert "cannot reach the control API" in result.output
    assert "no sequence" not in result.output, "'could not read it' must not claim 'nothing here'"


def test_sequence_wait_reports_losing_the_control_api_mid_wait(profile, runner, monkeypatch):
    responses = [_health_with()(), None]
    monkeypatch.setattr(cli, "_health", lambda: responses.pop(0) if len(responses) > 1 else responses[0])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "0"])
    assert result.exit_code == 1
    assert "lost the control API" in result.output
    assert "did not serve" not in result.output, "whether it served is unknown, and the message must not decide"


def test_sequence_wait_ignores_an_event_from_an_earlier_run(profile, runner, monkeypatch):
    """A reset clears the serve counter with the runtime entry, so a leftover /recent event from
    the previous run must not satisfy the wait on its own."""
    monkeypatch.setattr(cli, "_health", _health_with(runId="r2"))
    monkeypatch.setattr(cli, "_get_json", lambda path, timeout=2: [
        {"sequenceId": "ovr_a", "runId": "r1", "selectedStep": 1, "stepCount": 2,
         "method": "GET", "path": "/api/items", "status": 200}])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1", "--timeout", "0"])
    assert result.exit_code == 1


def test_sequence_wait_fails_fast_when_the_run_changes_mid_wait(profile, runner, monkeypatch):
    """A reset mid-wait invalidates the baseline. Polling on regardless would burn the timeout and
    then blame the sequence for not serving."""
    monkeypatch.setattr(cli, "_health", _health_over({}, {"runId": "r2"}))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 1
    assert "was reset" in result.output


def test_sequence_wait_fails_fast_when_the_rule_vanishes_mid_wait(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _health_over({}, None))
    monkeypatch.setattr(cli, "_get_json", lambda _path, timeout=2: [])
    result = runner.invoke(cli.cli, ["sequence", "wait", "ovr_a", "--step", "1"])
    assert result.exit_code == 1
    assert "was removed" in result.output


def test_status_json_carries_answer_counts(profile, runner, monkeypatch):
    """AGENTS.md documents `answers` in `status --json`; it was in /health and never forwarded."""
    monkeypatch.setattr(cli, "_health",
                        _answers_over({"count": 2}))
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    result = runner.invoke(cli.cli, ["status", "--json"])
    assert json.loads(result.output)["answers"] == [
        {"id": "ovr_a", "active": True, "count": 2, "runId": "run1"}]


def test_status_json_carries_sequences(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _health_with())
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    result = runner.invoke(cli.cli, ["status", "--json"])
    assert result.exit_code == 0
    assert '"sequences"' in result.output and "ovr_a" in result.output


# MARK: - Proving a mock was in play
#
# A negative UI assertion passes whether or not the mock applied, so the suite needs a command that
# fails. Every test below is a way that command could have reported success it had not earned.

def _answers_over(*states):
    """Answer counts under the poll, sharing the envelope with `_health_over`.

    Each state overlays the defaults below; `None` means the rule is gone from the session. The
    default `runId` is the one a caller would be holding from `reset`, so a state that means to
    change runs has to say so, and a command that stops reading the field fails here."""
    return _polling(states, lambda state: _health_payload(
        answers=[] if state is None
        else [{"id": "ovr_a", "active": True, "count": 0, "runId": "run1", **state}]))


def test_assert_answered_succeeds_when_the_rule_answered(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 3}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 0
    assert "3 request(s)" in result.output


def test_assert_answered_rejects_an_unknown_id_rather_than_reporting_zero(profile, runner, monkeypatch):
    """A typo and a rule that never fired are different bugs with different fixes, and reading one
    as the other is how you spend an afternoon on a matcher that was always correct."""
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 1}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_typo"])
    assert result.exit_code == 1
    assert "no rule 'ovr_typo'" in result.output
    assert "has not answered" not in result.output


def test_assert_answered_says_so_when_the_control_api_is_unreachable(profile, runner, monkeypatch):
    """"I could not ask" is not "it answered nothing"."""
    monkeypatch.setattr(cli, "_health", lambda: None)
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "cannot reach the control API" in result.output


def test_assert_answered_refuses_a_proxy_that_cannot_report_counts(profile, runner, monkeypatch):
    """An engine too old to report counts must not be read as a rule that answered nothing — that
    turns a restart into a debugging session."""
    monkeypatch.setattr(cli, "_health", lambda: {"activeSession": "default", "sequences": []})
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "does not report answer counts" in result.output


def test_assert_answered_fails_at_once_for_an_inactive_rule(profile, runner, monkeypatch):
    """Matching skips a disabled rule entirely, so waiting cannot help."""
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 0, "active": False}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--timeout", "30"])
    assert result.exit_code == 1
    assert "never answer" in result.output


def test_assert_answered_lists_the_paths_that_did_arrive(profile, runner, monkeypatch):
    """A count cannot tell "the app went somewhere else" from "the path pattern is wrong"; the
    paths can."""
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 0}))
    monkeypatch.setattr(cli, "_get_json", lambda *a, **k: [
        {"method": "GET", "path": "/api/v2/items"},
        {"method": "GET", "path": "/api/v2/items"},
    ])
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "/api/v2/items" in result.output
    assert "explain-match" in result.output


def test_assert_answered_names_an_empty_proxy_as_a_routing_problem(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 0}))
    monkeypatch.setattr(cli, "_get_json", lambda *a, **k: [])
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "relaunch the app" in result.output


def test_assert_answered_succeeds_on_an_answer_that_lands_mid_wait(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health",
                        _answers_over({"count": 0}, {"count": 0}, {"count": 2}))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--timeout", "30"])
    assert result.exit_code == 0
    assert "2 request(s)" in result.output


def test_assert_answered_rejects_a_negative_timeout(profile, runner):
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--timeout", "-1"])
    assert result.exit_code == 1
    assert "must not be negative" in result.output


# MARK: - Binding the assertion to the run the caller started
#
# A count answers "has this rule answered in *some* run". The run a test set up ends whenever
# anything resets the rule, replaces it under the same id, or switches session — and the rule id
# looks identical on the other side of that. Every test here is a way the command could report a
# stranger's evidence as the test's own, which is worse than reporting none: it is a green pass.

def test_assert_answered_refuses_a_count_from_another_run(profile, runner, monkeypatch):
    """The defect this option exists for: something reset the rule between the action and the
    assertion, the app fetched again, and the count is now three — for a run the test never set
    up. Reported as success it is a pass nobody earned."""
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 3, "runId": "run2"}))
    monkeypatch.setattr(cli, "_get_json", lambda *a, **k: [])
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 3, "the assertion was not made, which is not 'it answered nothing'"
    assert "run2" in result.output and "run1" in result.output
    assert "answered 3" not in result.output


def test_assert_answered_accepts_a_count_from_the_run_it_was_given(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 2, "runId": "run1"}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 0
    assert "2 request(s)" in result.output and "run1" in result.output


def test_assert_answered_refuses_a_run_that_ended_while_it_waited(profile, runner, monkeypatch):
    """The same substitution, arriving mid-poll. Continuing to watch would eventually see the new
    run answer and return success on evidence produced after the boundary was destroyed."""
    monkeypatch.setattr(cli, "_health", _answers_over(
        {"count": 0, "runId": "run1"}, {"count": 0, "runId": "run2"}, {"count": 5, "runId": "run2"}))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cli, "_get_json", lambda *a, **k: [])
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1", "--timeout", "30"])
    assert result.exit_code == 3
    assert "not run run1" in result.output
    assert "5 request(s)" not in result.output


def test_assert_answered_tells_a_missing_run_from_a_run_with_no_answers(profile, runner, monkeypatch):
    """`runId: null` says the rule has no run at all — its state was dropped by a session switch or
    a replacement. That is not "the run you named happened and nothing answered", and the two need
    different fixes, so they must not share an exit code."""
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 0, "runId": None}))
    monkeypatch.setattr(cli, "_get_json", lambda *a, **k: [])
    bound = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert bound.exit_code == 3
    assert "no run at all" in bound.output

    monkeypatch.setattr(cli, "_health", _answers_over({"count": 0, "runId": None}))
    plain = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert plain.exit_code == 1, "the weaker assertion still just reports no answers"
    assert "has not answered" in plain.output


def test_assert_answered_refuses_a_proxy_that_cannot_report_run_identity(profile, runner, monkeypatch):
    """An engine old enough to count answers but not to say which run they belong to. Ignoring
    --run there would silently downgrade the assertion to the one it was called to avoid."""
    monkeypatch.setattr(cli, "_health", lambda: _health_payload(
        answers=[{"id": "ovr_a", "active": True, "count": 4}]))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 3
    assert "does not report which run" in result.output
    assert "4 request(s)" not in result.output


def test_assert_answered_without_a_run_reads_whatever_run_is_current(profile, runner, monkeypatch):
    """The documented weaker semantics, pinned so they stay deliberate: no --run means the command
    asks about the run the proxy is in when it looks, whichever run that turns out to be."""
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 3, "runId": "a-run-nobody-held"}))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 0
    assert "3 request(s)" in result.output


def test_assert_answered_rejects_an_empty_run(profile, runner):
    """An empty --run would compare equal to nothing and unequal to everything by accident; a run
    that cannot be named is refused at the boundary instead."""
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", ""])
    assert result.exit_code == 1
    assert "must name a run id" in result.output


def test_assert_answered_refuses_a_rule_that_vanishes_while_it_waits(profile, runner, monkeypatch):
    """A session switched mid-wait to one that does not carry this id destroys the boundary rather
    than answering the question about it. Reported as 1 it would read as "the mock did not apply",
    which is a claim this command was in no position to make."""
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 0, "runId": "run1"}, None))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1", "--timeout", "30"])
    assert result.exit_code == 3
    assert "no rule 'ovr_a'" in result.output
    assert "cannot be checked" in result.output


def test_assert_answered_without_a_run_still_reports_a_missing_rule_as_one(profile, runner, monkeypatch):
    """The weaker assertion keeps every exit code it had: only --run can produce 3."""
    monkeypatch.setattr(cli, "_health", _answers_over(None))
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])
    assert result.exit_code == 1
    assert "no rule 'ovr_a'" in result.output


def test_assert_answered_cannot_prove_a_run_against_a_proxy_that_counts_but_cannot_name_runs(
        profile, runner, monkeypatch):
    """Version skew in the other field. An engine that cannot count at all certainly cannot say
    which run its counts are in, so under --run both refusals have to arrive as the same code — a
    harness branching on 3 must not have to learn which flavour of skew it hit."""
    monkeypatch.setattr(cli, "_health", lambda: {"activeSession": "default", "sequences": []})
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 3
    assert "does not report answer counts" in result.output


def test_assert_answered_with_a_run_reports_an_unreachable_proxy_as_unproven(profile, runner, monkeypatch):
    """"I could not ask" is not "the rule answered nothing in your run" — and under --run there is
    a code that says so, so the one meaning left for 1 is a real, made assertion that failed."""
    monkeypatch.setattr(cli, "_health", lambda: None)
    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])
    assert result.exit_code == 3
    assert "cannot reach the control API" in result.output


# MARK: - explain-match

_RULES = [
    {"id": "ovr_broad", "mode": "replace", "match": {"method": "GET", "path": "/api/items"}},
    {"id": "ovr_alpha", "mode": "replace",
     "match": {"method": "GET", "path": "/api/items", "query": {"kind": "alpha"}}},
    {"id": "ovr_other", "mode": "replace", "match": {"method": "GET", "path": "/api/orders"}},
    {"id": "ovr_off", "active": False, "mode": "replace", "match": {"path": "/api/items"}},
]


def test_explain_match_names_the_winner_and_what_it_shadowed(profile, runner, monkeypatch):
    """The over-match made visible: the broad rule answers alpha, beta and gamma alike, and nothing
    in the tool used to say so until it had already happened."""
    monkeypatch.setattr(cli, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/api/items?kind=alpha"])
    assert result.exit_code == 0
    assert "ovr_alpha" in result.output
    assert "also matched" in result.output and "ovr_broad" in result.output


def test_explain_match_reads_a_repeated_query_key_the_way_the_wire_does(profile, runner, monkeypatch):
    """mitmproxy's MultiDict returns the first value; `dict(parse_qsl(...))` kept the last. For
    `?kind=beta&kind=alpha` the proxy selects on beta while this command explained alpha — a
    selection it then swore would happen."""
    monkeypatch.setattr(cli, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/api/items?kind=beta&kind=alpha"])
    assert result.exit_code == 0
    assert "rule wants 'alpha', request has 'beta'" in result.output
    assert "ovr_alpha is selected" not in result.output


def test_explain_match_says_why_each_rule_missed(profile, runner, monkeypatch):
    monkeypatch.setattr(cli, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/api/items?kind=beta"])
    assert result.exit_code == 0
    assert "ovr_broad" in result.output
    assert "rule wants 'alpha', request has 'beta'" in result.output


def test_explain_match_exits_non_zero_when_nothing_is_selected(profile, runner, monkeypatch):
    """Usable as a check in a script: a rule you cannot select is a rule that will never fire."""
    monkeypatch.setattr(cli, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "POST", "/api/nothing"])
    assert result.exit_code == 1
    assert "no active rule" in result.output


def test_explain_match_reports_an_inactive_rule_separately(profile, runner, monkeypatch):
    """It matches and still cannot answer. Listing it among the misses would be a lie; leaving it
    out entirely loses the answer to "why is my rule not firing?"."""
    monkeypatch.setattr(cli, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/api/items"])
    assert "inactive" in result.output and "ovr_off" in result.output


def test_explain_match_does_not_claim_a_patch_will_answer(profile, runner, monkeypatch):
    """Whether a patch answers depends on the upstream content type, which no dry run can know."""
    monkeypatch.setattr(cli, "_control",
                        lambda *a, **k: [{"id": "p", "mode": "patch", "match": {"path": "/a"}}])
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/a"])
    assert result.exit_code == 0
    assert "is selected" in result.output
    assert "only if the upstream response is JSON" in result.output


def test_control_surfaces_the_apis_detail_not_just_its_slug(profile, runner, monkeypatch):
    """The API sends the sentence that names the problem and a slug for it. Printing the slug is
    how a supported matcher field ends up looking unsupported."""
    import urllib.error

    class _Body:
        @staticmethod
        def read():
            return json.dumps({"error": "invalid_payload",
                               "detail": "match: unknown field 'kind'"}).encode()

    def raise_http(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://x", 400, "Bad Request", {}, _Body())  # type: ignore[arg-type]

    monkeypatch.setattr(cli.urllib.request, "urlopen", raise_http)
    result = runner.invoke(cli.cli, ["override", "add", '{"mode":"replace"}'])
    assert result.exit_code == 1
    assert "unknown field 'kind'" in result.output


# MARK: - Which profile a control call means
#
# One proxy holds the control port. With profile A running, `lyrebird --profile B use X` reached A,
# switched A's session and printed success — so the operator watched an unchanged profile B. Every
# call now names its profile, and a command that reads or writes for the wrong one fails.

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
        raise cli.urllib.error.HTTPError(
            "http://127.0.0.1:8088/x", 409, "Conflict", {},   # type: ignore[arg-type]
            _JsonBody(payload if payload is not None else
                      {"error": "profile_mismatch", "running": FOREIGN_FINGERPRINT,
                       "requested": config.PROFILE_FINGERPRINT}))

    monkeypatch.setattr(cli.urllib.request, "urlopen", raise_http)


def _records_the_request(monkeypatch, payload):
    """Capture the outgoing `Request` and answer it with `payload`."""
    seen = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        @staticmethod
        def read():
            return json.dumps(payload).encode()

    def fake_urlopen(request, timeout=None):
        seen["headers"] = {name.lower(): value for name, value in request.header_items()}
        return _Response()

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_a_mutation_names_the_profile_it_means(profile, runner, monkeypatch):
    """Without the header the API cannot tell a call meant for it from one meant for a profile that
    is not running, so it serves both and the caller never learns which one it changed."""
    seen = _records_the_request(monkeypatch, {"id": "ovr_a", "active": True})

    result = runner.invoke(cli.cli, ["override", "add", '{"mode":"replace","status":204}'])

    assert result.exit_code == 0
    assert seen["headers"]["x-lyrebird-profile"] == config.PROFILE_FINGERPRINT


def test_a_read_names_the_profile_it_means_too(profile, monkeypatch):
    """A read answered by another profile's proxy reports its sessions, counters and traffic as
    this profile's — a wrong answer, not a missing one."""
    seen = _records_the_request(monkeypatch, [])

    assert cli._get_json("/__mock__/recent") == []
    assert seen["headers"]["x-lyrebird-profile"] == config.PROFILE_FINGERPRINT


def test_use_refuses_to_switch_a_session_in_someone_elses_profile(profile, runner, monkeypatch):
    """The bug in its original form: `--profile B use X` switched profile A and said "active: X"."""
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["use", "smoke"])

    assert result.exit_code == 1
    assert FOREIGN_FINGERPRINT in result.output, "say which profile actually holds the port"
    assert config.PROFILE_FINGERPRINT in result.output, "and which one was asked for"
    assert "lyrebird down" in result.output, "and how to get out of it"


def test_a_refused_call_does_not_print_the_bare_slug(profile, runner, monkeypatch):
    """`profile_mismatch` alone names neither fingerprint, so it reads as a bug in the command
    rather than as two proxies being confused for one."""
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["reset"])

    assert result.exit_code == 1
    assert "profile_mismatch" not in result.output


def test_a_polling_read_refused_for_the_wrong_profile_is_not_reported_as_unreachable(
        profile, runner, monkeypatch):
    """`_get_json` answers None for "not reachable", and a 409 is the opposite of that: the proxy is
    up and talking. Reporting it as silence sends the operator to look for a dead port."""
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["wait-ready", "--timeout", "1"])

    assert result.exit_code == 1
    assert "no proxied requests" not in result.output
    assert FOREIGN_FINGERPRINT in result.output


def test_up_refuses_to_adopt_a_proxy_running_another_profile(profile, runner, monkeypatch):
    """`up` must not report INTERCEPT ACTIVE for a proxy serving somebody else's rules."""
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    config.reload_profile()
    monkeypatch.setattr(cli, "_health", lambda: _health_payload(
        profileFingerprint=FOREIGN_FINGERPRINT))

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert FOREIGN_FINGERPRINT in result.output
    assert "lyrebird down" in result.output


@pytest.mark.parametrize("command", [
    ["sequence", "wait", "ovr_a", "--step", "1"],
    ["assert-answered", "ovr_a"],
])
def test_a_command_reading_health_refuses_another_profiles_reading(profile, runner, monkeypatch,
                                                                   command):
    """Health is unscoped at the API — that is how `down` recovers across profiles — so a command
    that *interprets* a reading has to compare the fingerprint itself, or it reports a stranger's
    sequence cursors and answer counts as evidence about this profile's rules."""
    monkeypatch.setattr(cli, "_health", lambda: _health_payload(
        profileFingerprint=FOREIGN_FINGERPRINT,
        sequences=[_BASE_SEQ], answers=[{"id": "ovr_a", "active": True, "count": 7}]))
    monkeypatch.setattr(cli.time, "sleep",
                        lambda _seconds: pytest.fail("a mismatch must fail before any polling"))

    result = runner.invoke(cli.cli, command)

    assert result.exit_code == 1
    assert FOREIGN_FINGERPRINT in result.output
    assert "answered 7" not in result.output, "no claim may be made from the wrong profile's state"


def test_a_bound_assertion_reports_a_foreign_profile_as_unproven(profile, runner, monkeypatch):
    """The port can change hands between the reset and the assertion. Refusing is right, but under
    --run exit 1 claims "the rule is in your run and answered nothing" — about a run this command
    never got to look at, in a store that is not even the one the reset drew its boundary in."""
    monkeypatch.setattr(cli, "_health", lambda: _health_payload(
        profileFingerprint=FOREIGN_FINGERPRINT,
        answers=[{"id": "ovr_a", "active": True, "count": 7, "runId": "run1"}]))
    monkeypatch.setattr(cli.time, "sleep",
                        lambda _seconds: pytest.fail("a mismatch must fail before any polling"))

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1", "--timeout", "30"])

    assert result.exit_code == 3
    assert FOREIGN_FINGERPRINT in result.output
    assert "answered 7" not in result.output, "a matching run id from a stranger proves nothing"


def test_a_bound_assertion_refuses_a_profile_that_changes_under_the_poll(profile, runner, monkeypatch):
    """The same swap arriving mid-wait: this profile's proxy is stopped and another profile's is
    started on the port. The fingerprint is re-read every poll, so the wait ends where it lost the
    ability to answer — rather than burning its timeout on, or believing, a stranger's counters."""
    monkeypatch.setattr(cli, "_health", _polling(
        [{}, {"profileFingerprint": FOREIGN_FINGERPRINT}],
        lambda state: _health_payload(
            answers=[{"id": "ovr_a", "active": True, "count": 0, "runId": "run1"}], **state)))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1", "--timeout", "30"])

    assert result.exit_code == 3
    assert FOREIGN_FINGERPRINT in result.output


def test_a_bound_assertion_refuses_a_profile_that_takes_the_port_before_the_diagnostic_read(
        profile, runner, monkeypatch):
    """The last call a failing assertion makes is the `/recent` read behind its "what did arrive"
    hint, and unlike `/health` that one is scoped, so the API refuses it with a 409. Exiting 1 there
    prints a profile mismatch under the code that means "your run was checked and nothing answered",
    which is the one reading of exit 1 that has to stay true."""
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 0, "runId": "run1"}))
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a", "--run", "run1"])

    assert result.exit_code == 3
    assert FOREIGN_FINGERPRINT in result.output


def test_an_unbound_assertion_still_exits_one_when_the_diagnostic_read_is_refused(
        profile, runner, monkeypatch):
    """The same moment without --run: no boundary was claimed, so nothing here may start returning
    a code the older contract never had."""
    monkeypatch.setattr(cli, "_health", _answers_over({"count": 0, "runId": "run1"}))
    _answers_with_a_conflict(monkeypatch)

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])

    assert result.exit_code == 1
    assert FOREIGN_FINGERPRINT in result.output


def test_a_health_reading_without_a_fingerprint_is_still_accepted(profile, runner, monkeypatch):
    """An engine that predates the field cannot say which profile it runs. Refusing it would turn
    an upgrade into a breakage, so — exactly as `up` does — a missing fingerprint is allowed."""
    health = _health_payload(answers=[{"id": "ovr_a", "active": True, "count": 1}])
    del health["profileFingerprint"]
    monkeypatch.setattr(cli, "_health", lambda: health)

    result = runner.invoke(cli.cli, ["assert-answered", "ovr_a"])

    assert result.exit_code == 0


def test_down_still_stops_a_proxy_that_belongs_to_another_profile(profile, runner, fake_network,
                                                                  monkeypatch):
    """`down` is the recovery command: it must put the network back whatever is running, or the
    scoping added everywhere else would strand the Mac pointing at a proxy it may not name."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.runtime_file().write_bytes(b"not json at all\xff")
    monkeypatch.setattr(cli, "_health", lambda: None if fake_network["terminated"] else
                        {"pid": 4242, "profileFingerprint": FOREIGN_FINGERPRINT})

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0
    assert fake_network["restored"] is not None, "the PAC must be restored whoever owns the proxy"
    assert (4242, "addon.py") in fake_network["terminated"]


def test_override_add_help_lists_every_override_field(profile, runner):
    """Validation now rejects anything outside this vocabulary, so a field missing from the help is
    a rule the author cannot write and cannot find out about."""
    result = runner.invoke(cli.cli, ["override", "add", "--help"])
    assert result.exit_code == 0
    # The block itself, not substrings: `body` is a substring of `bodyContains` in the matcher
    # block below it, so `field in output` passes with the `body` entry deleted.
    block = result.output.split("An override accepts these fields", 1)[1].split("`match` accepts", 1)[0]
    listed = {line.split()[0] for line in block.splitlines() if line.startswith("    ")}
    assert listed == set(rules.OVERRIDE_FIELDS)


def test_override_add_help_lists_every_matcher_field(profile, runner):
    """The capability that already existed but could not be found from the tool itself."""
    result = runner.invoke(cli.cli, ["override", "add", "--help"])
    for field in rules.MATCHER_FIELDS:
        assert field in result.output


def _status_network(monkeypatch):
    monkeypatch.setattr(netproxy, "active_service", lambda: "Wi-Fi")
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))


def test_status_json_says_null_when_the_engine_cannot_report(profile, runner, monkeypatch):
    """A proxy still running from before these fields existed cannot answer the question. Reporting
    `[]` would say "nothing has answered", which is a different claim from "I could not ask"."""
    monkeypatch.setattr(cli, "_health", lambda: {"activeSession": "default", "sessions": []})
    _status_network(monkeypatch)
    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)
    assert payload["answers"] is None and payload["sequences"] is None


def test_status_json_says_empty_when_the_engine_reports_nothing_to_show(profile, runner, monkeypatch):
    """The other half of the distinction: a current engine sends one entry per rule, so an empty
    list is a real state and must not be confused with the case above."""
    monkeypatch.setattr(cli, "_health", _health_payload)
    _status_network(monkeypatch)
    payload = json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)
    assert payload["answers"] == [] and payload["sequences"] == []


# MARK: - Offline inspection: `validate` and `explain-match --session`
#
# These commands exist because the alternative was starting a proxy, changing the Mac's network
# settings and walking through an app to discover a typo in a JSON file. So the thing worth pinning
# hardest is the negative: no proxy, no network, and nothing written.

@pytest.fixture
def offline(monkeypatch):
    """Fails the test if anything reaches the proxy, the network or the store."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline command must not talk to the proxy or construct a Store")

    monkeypatch.setattr(cli, "_control", refuse)
    monkeypatch.setattr(cli, "_health", refuse)
    monkeypatch.setattr(netproxy, "active_service", refuse)
    monkeypatch.setattr(cli.store.Store, "__init__", refuse)


def write_session(profile, name, payload):
    """A session file exactly as an operator would hand-write one."""
    path = profile / "sessions" / f"{name}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


_WHOLE = {"name": "whole", "overrides": [
    {"id": "ovr_orders", "mode": "replace", "status": 500,
     "match": {"method": "GET", "path": "/api/v1/orders/*"}},
]}
_PARTIAL = {"name": "partial", "overrides": [
    {"id": "ovr_orders", "mode": "replace", "status": 500,
     "match": {"method": "GET", "path": "/api/v1/orders/*"}},
    {"id": "ovr_typo", "mode": "replace", "status": 200, "match": {"paths": "/api/v1/users"}},
    {"id": "ovr_orders", "mode": "replace", "status": 204, "match": {"path": "/api/v1/dupe"}},
]}


def test_validate_accepts_a_session_that_loads_whole(profile, runner, offline):
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "whole"])
    assert result.exit_code == 0
    assert "1 rule(s)" in result.output


def test_validate_reports_malformed_json_and_exits_non_zero(profile, runner, offline):
    write_session(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["validate", "broken"])
    assert result.exit_code == 1
    assert "not loaded at all" in result.output
    assert "broken.json" in result.output


def test_validate_reports_a_partly_loaded_session_rule_by_rule(profile, runner, offline):
    """The failure the issue is about: startup keeps the rules it can read and carries on, so a
    scenario runs live and quietly missing the one rule the test depends on. "Something in this
    file is wrong" costs the same walk through the app this exists to avoid, so each dropped rule
    is named by index and by the field that dropped it — an unknown matcher field here, and a
    duplicate id, which cannot be kept because two rules sharing one share a cursor and an answer
    count and cannot be removed independently."""
    write_session(profile, "partial", _PARTIAL)
    result = runner.invoke(cli.cli, ["validate", "partial"])
    assert result.exit_code == 1
    assert "1 rule(s) kept, 2 dropped" in result.output
    assert "partial.json" in result.output
    assert "override[1]" in result.output and "'paths'" in result.output
    assert "override[2]" in result.output and "duplicate id" in result.output


def test_validate_refuses_an_unsupported_schema_version(profile, runner, offline):
    write_session(profile, "future", {"schemaVersion": 2, "name": "future", "overrides": []})
    result = runner.invoke(cli.cli, ["validate", "future"])
    assert result.exit_code == 1
    assert "schemaVersion" in result.output and "not loaded at all" in result.output


def test_validate_reports_a_file_that_points_at_itself(profile, runner, offline):
    """`Path.resolve()` raises RuntimeError on a symlink loop, and it is not an OSError. Uncaught,
    the command died on a traceback — with `--json`, before printing any of the diagnostics it
    promises, which is the one output a caller cannot recover from."""
    loop = profile / "sessions" / "loop.json"
    loop.symlink_to(loop)
    text = runner.invoke(cli.cli, ["validate", "loop"])
    machine = runner.invoke(cli.cli, ["validate", "--json"])
    assert text.exit_code == 1 and "cannot resolve path" in text.output
    assert machine.exit_code == 1
    payload = json.loads(machine.output)
    assert any("loop.json" in problem
               for session in payload["sessions"] for problem in session["problems"])


def test_validate_reports_a_delay_that_is_not_a_finite_number(profile, runner, offline):
    """`1e309` parses as `inf` and `int(inf)` raises OverflowError, which is not a ValidationError
    and not even a ValueError — so the rule that should have been one line of diagnostics took the
    whole command down instead."""
    write_session(profile, "wild", {"name": "wild", "overrides": [
        {"id": "ovr_slow", "mode": "replace", "status": 200, "delayMs": 1e309},
    ]})
    result = runner.invoke(cli.cli, ["validate", "wild", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert any("override[0]" in problem and "finite" in problem
               for problem in payload["sessions"][0]["problems"])


def test_explain_match_reports_a_file_that_points_at_itself(profile, runner, offline):
    loop = profile / "sessions" / "loop.json"
    loop.symlink_to(loop)
    result = runner.invoke(cli.cli, ["explain-match", "--session", "loop", "--json", "GET", "/a"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] is None
    assert any("cannot resolve path" in problem for problem in payload["problems"])


def test_validate_json_separates_not_loaded_from_loaded_with_problems(profile, runner, offline):
    write_session(profile, "broken", "{not json")
    write_session(profile, "partial", _PARTIAL)
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    by_name = {session["name"]: session for session in payload["sessions"]}
    assert by_name["whole"]["loaded"] is True and by_name["whole"]["ok"] is True
    assert by_name["whole"]["overrideCount"] == 1 and by_name["whole"]["problems"] == []
    assert by_name["partial"]["loaded"] is True and by_name["partial"]["ok"] is False
    assert by_name["partial"]["overrideCount"] == 1
    # null, not 0: a refused file has no rule count, and 0 reads as a session that loaded empty.
    assert by_name["broken"]["loaded"] is False and by_name["broken"]["overrideCount"] is None
    assert by_name["broken"]["problems"]


def test_validate_checks_every_session_when_none_is_named(profile, runner, offline):
    write_session(profile, "whole", _WHOLE)
    write_session(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["validate"])
    assert result.exit_code == 1
    assert "whole" in result.output and "broken" in result.output
    assert "2 session(s)" in result.output


def test_validate_refuses_a_session_that_does_not_exist(profile, runner, offline):
    """A typo and a scenario with no rules need different fixes. Reporting an empty session for a
    name nobody wrote is the same false success `--clone-from` was fixed for."""
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "whol"])
    assert result.exit_code == 1
    assert "no session 'whol'" in result.output
    assert "whole" in result.output, "the names that do exist are the useful half of the answer"


def test_validate_refuses_a_name_that_would_escape_the_profile(profile, runner, offline):
    result = runner.invoke(cli.cli, ["validate", "../../etc/passwd"])
    assert result.exit_code == 1
    assert "invalid session name" in result.output


def test_validate_fails_when_there_is_nothing_to_validate(profile, runner, offline):
    """A command named for checking sessions that checked none has not validated anything. The
    usual cause is the wrong --profile, so exiting 0 would hide it behind a green tick."""
    result = runner.invoke(cli.cli, ["validate"])
    assert result.exit_code == 1
    assert "no session files" in result.output


def test_validate_json_stays_json_when_the_session_does_not_exist(profile, runner, offline):
    """A caller that pipes this into `jq` gets prose on exactly the failures it most needs to read.
    Every way the command can fail comes back in the documented shape."""
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "whol", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["sessions"] == []
    assert any("no session 'whol'" in problem for problem in payload["problems"])


def test_validate_json_stays_json_when_there_is_nothing_to_validate(profile, runner, offline):
    result = runner.invoke(cli.cli, ["validate", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["sessions"] == []
    assert any("no session files" in problem for problem in payload["problems"])


def test_validate_json_stays_json_for_a_name_that_could_not_be_one(profile, runner, offline):
    result = runner.invoke(cli.cli, ["validate", "../../etc/passwd", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert any("invalid session name" in problem for problem in payload["problems"])


def test_validate_does_not_bless_a_session_symlinked_out_of_the_profile(profile, runner, offline,
                                                                       tmp_path):
    """Reported as a bulk-mode gap: `validate NAME` refused the escaping file by name while
    `validate` blessed the very same file found by the glob."""
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "outside", "overrides": []}), encoding="utf-8")
    (profile / "sessions" / "sneaky.json").symlink_to(outside)
    bulk = runner.invoke(cli.cli, ["validate"])
    named = runner.invoke(cli.cli, ["validate", "sneaky"])
    assert bulk.exit_code == 1 and named.exit_code == 1, "one file, one verdict"
    assert "sneaky" in bulk.output


def test_validate_writes_nothing_to_the_profile(profile, runner, offline):
    """Inspection must not rewrite the profile or change the active session — no synthesised
    `default.json`, no re-indented file, no active-session pointer."""
    path = write_session(profile, "whole", _WHOLE)
    original = path.read_bytes()
    before = sorted(p.name for p in (profile / "sessions").iterdir())
    assert runner.invoke(cli.cli, ["validate"]).exit_code == 0
    assert path.read_bytes() == original
    assert sorted(p.name for p in (profile / "sessions").iterdir()) == before
    assert not config.STATE_FILE.exists()


def test_explain_match_against_a_file_never_asks_the_proxy(profile, runner, offline):
    """The whole point: the answer comes from the file and the engine's own matching code, with no
    proxy running and nothing on the machine changed."""
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(
        cli.cli, ["explain-match", "--session", "whole", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 0
    assert "ovr_orders is selected" in result.output


def test_explain_match_against_a_file_uses_the_engines_own_matcher(profile, runner, offline):
    """Same reasons, same wording as the live command — a reimplementation would drift, and the
    command exists to be believed."""
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["explain-match", "--session", "whole", "GET", "/api/v1/users"])
    assert result.exit_code == 1
    assert "does not match" in result.output and "/api/v1/orders/*" in result.output


def test_explain_match_against_a_partly_loaded_file_exits_non_zero(profile, runner, offline):
    """A ranking computed without the rules the loader dropped answers "which rule wins" while
    hiding that the rule you asked about was never a candidate."""
    write_session(profile, "partial", _PARTIAL)
    result = runner.invoke(
        cli.cli, ["explain-match", "--session", "partial", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 1, "the ranking does not cover the rules the file was written with"
    assert "ovr_orders is selected" in result.output
    assert "dropped at load" in result.output and "override[1]" in result.output


def test_explain_match_names_the_file_it_could_not_read(profile, runner, offline):
    """"Nothing would be selected" is the sentence an empty session produces, and this is not that:
    every rule in the file is absent, not out-ranked."""
    write_session(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["explain-match", "--session", "broken", "GET", "/a"])
    assert result.exit_code == 1
    assert "broken.json" in result.output
    assert "no active rule would be selected" not in result.output


def test_explain_match_refuses_a_session_that_does_not_exist(profile, runner, offline):
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["explain-match", "--session", "nope", "GET", "/a"])
    assert result.exit_code == 1
    assert "no session 'nope'" in result.output


def test_explain_match_json_carries_the_diagnostics_for_a_file(profile, runner, offline):
    write_session(profile, "partial", _PARTIAL)
    result = runner.invoke(
        cli.cli, ["explain-match", "--session", "partial", "--json", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] == "ovr_orders"
    assert [c["id"] for c in payload["candidates"]] == ["ovr_orders"]
    assert any("override[1]" in problem for problem in payload["problems"])


def test_explain_match_json_for_an_unreadable_file_still_has_the_live_shape(profile, runner, offline):
    write_session(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["explain-match", "--session", "broken", "--json", "GET", "/a"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] is None and payload["candidates"] == []
    assert payload["problems"]


def test_explain_match_against_the_proxy_does_not_claim_its_session_loaded_whole(profile, runner,
                                                                                monkeypatch):
    """`problems: []` on the live path would assert something this command never checked — the
    running session's load problems live in the proxy, not in a file it read."""
    monkeypatch.setattr(cli, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "--json", "GET", "/api/items?kind=alpha"])
    assert result.exit_code == 0
    assert "problems" not in json.loads(result.output)

# MARK: - Selecting the scenario before the app is launched
#
# An app makes its first requests *while it launches*, so a session selected after the relaunch is
# a session the launch never saw — and an app that caches its launch response goes on showing the
# old scenario however green a later `lyrebird use` looked. These tests therefore do not assert on
# call order for its own sake: the fake `_relaunch` records what the app's launch request was
# answered with, and that answer is the evidence.

_SCENARIOS = {"default": 200, "orders-outage": 500}


def _fake_proxy(monkeypatch, *, active="default", sessions=("default", "orders-outage"),
                load_problems=(), not_whole=(), unreachable=False, steps=None):
    """A live proxy the CLI can select sessions on, and an app whose launch makes one request.

    `launched` holds what that request was answered with — the session that served it, that
    scenario's status, and the sequence step it got. `events` holds what happened in what order,
    for the cases where nothing is launched at all.

    `load_problems` (the flat strings a person reads) and `not_whole` (the same problems keyed by
    session) are set independently, because the reason the second field exists is that the first
    cannot be turned into it. `not_whole=None` is a proxy too old to have either.
    """
    state = {"active": active, "sessions": list(sessions), "loadProblems": list(load_problems),
             "notWhole": None if not_whole is None else dict(not_whole),
             "steps": dict(steps or {}), "events": [], "launched": [], "devices": []}

    def control(path, method="GET", payload=None, timeout=3.0):
        assert (path, method) == ("/__mock__/sessions/active", "PUT"), (path, method)
        state["events"].append(("activate", payload["name"]))
        if unreachable:   # what `_control` prints when the proxy stops answering mid-`up`
            click.echo("✗ proxy not reachable — is it running? (`lyrebird up`)")
            raise SystemExit(1)
        if payload["name"] not in state["sessions"]:   # the API's 404, with the detail it sends
            click.echo(f"✗ no session named '{payload['name']}' in this profile")
            raise SystemExit(1)
        previous = state["active"]
        state["active"] = payload["name"]
        state["steps"][payload["name"]] = 1   # activating a session rewinds its sequences
        return {"active": state["active"], "previous": {"name": previous, "overrideCount": 0}}

    def relaunch(bundle_id, simulator):
        state["events"].append(("relaunch", bundle_id))
        state["devices"].append(simulator.udid)
        state["launched"].append({"session": state["active"],
                                  "status": _SCENARIOS[state["active"]],
                                  "step": state["steps"].get(state["active"], 1)})
        return True, bundle_id

    monkeypatch.setattr(cli, "_control", control)
    monkeypatch.setattr(cli, "_relaunch", relaunch)
    return state


def _live(state):
    reading = {**_LIVE, "activeSession": state["active"], "sessions": state["sessions"],
               "loadProblems": state["loadProblems"], "sessionsNotWhole": state["notWhole"]}
    if state["notWhole"] is None:   # an engine older than the fields, which cannot say
        del reading["loadProblems"]
        del reading["sessionsNotWhole"]
    return reading


def _up_with_a_proxy(profile, monkeypatch, state, *, adopt=False, bundle_id="com.example.Store"):
    """`up` against `state`'s proxy — either starting it, or adopting one already running."""
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))
    sim = f', "simBundleId": "{bundle_id}"' if bundle_id else ""
    (profile / "profile.json").write_text(f'{{"hosts": ["api.example.com"]{sim}}}', encoding="utf-8")
    # Health tracks the fake proxy, so the last look reports the session that was actually selected.
    first = iter([] if adopt else [None])   # dead at the first look unless we are adopting one
    monkeypatch.setattr(cli, "_health", lambda: next(first, _live(state)))


@pytest.mark.parametrize("adopt", [False, True], ids=["fresh start", "adopting a running proxy"])
def test_up_selects_the_session_before_it_relaunches_the_app(profile, runner, monkeypatch, adopt):
    """The bug: `up` relaunched the app and only the documented `use` afterwards selected the
    scenario, so the launch was answered by whatever session happened to be active — and an app
    that cached that response kept the wrong state on screen for the rest of the run."""
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state, adopt=adopt)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 0, result.output
    assert state["launched"] == [{"session": "orders-outage", "status": 500, "step": 1}], \
        "the app's launch request was answered by a session the caller did not ask for"
    assert state["events"] == [("activate", "orders-outage"), ("relaunch", "com.example.Store")]
    assert state["devices"] == [_PHONE["udid"]], "the launch goes to the resolved device, by UDID"


def test_up_rewinds_the_session_it_selects_so_the_launch_starts_at_step_one(
        profile, runner, monkeypatch):
    """`--use` on the session that is already active is not a no-op: activation rewinds its
    sequences, and without it the relaunch resumes mid-scenario at whatever step the last run
    left behind."""
    state = _fake_proxy(monkeypatch, active="orders-outage", steps={"orders-outage": 3})
    _up_with_a_proxy(profile, monkeypatch, state, adopt=True)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 0, result.output
    assert state["launched"] == [{"session": "orders-outage", "status": 500, "step": 1}]


def test_up_does_not_relaunch_under_a_fallback_when_the_session_is_unknown(
        profile, runner, monkeypatch):
    """Relaunching anyway would run the whole suite against the previous session and report every
    step of `up` as a success."""
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "nope"])

    assert result.exit_code == 1
    assert state["launched"] == [], "the app was launched against the session already active"
    assert "no session named 'nope'" in result.output
    assert "default, orders-outage" in result.output, "say which sessions there are"
    assert "NOT relaunched" in result.output
    assert "lyrebird down" in result.output


def test_up_does_not_relaunch_a_session_whose_overrides_were_dropped(profile, runner, monkeypatch):
    """The session is in `sessions` and activates happily, but the rules the scenario is made of
    did not survive the load — so the launch would meet a scenario that is not the one on disk."""
    problem = "orders-outage.json: override[1]: unknown field 'statsu'"
    state = _fake_proxy(monkeypatch, load_problems=[problem],
                        not_whole={"orders-outage": [problem]})
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["launched"] == [] and state["events"] == []
    assert "did not load whole" in result.output
    assert "unknown field 'statsu'" in result.output


def test_up_does_not_relaunch_when_the_named_session_was_replaced_by_an_empty_default(
        profile, runner, monkeypatch):
    """A malformed `default.json` is skipped and an empty in-memory `default` stands in for it.
    The session list cannot tell those two apart, so only the load problem can."""
    problem = "skipped default.json: Expecting value: line 1 column 1"
    state = _fake_proxy(monkeypatch, load_problems=[problem], not_whole={"default": [problem]})
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "default"])

    assert result.exit_code == 1
    assert state["launched"] == [] and state["events"] == []
    assert "session 'default' did not load whole" in result.output


@pytest.mark.parametrize("problem", [
    "skipped other.json: Expecting value: line 1 column 1",
    # The store reports the *file* name, and a file may be named `orders-outage.json: backup.json`
    # — a name it rejects, so no session is reported against it at all. The string it leaves in
    # `loadProblems` nonetheless begins exactly like a problem with `orders-outage`, which is why
    # the decision is taken from `sessionsNotWhole` and not from these strings.
    "skipped orders-outage.json: backup.json: invalid session name "
    "'orders-outage.json: backup' — use letters, digits, dot, dash or underscore",
], ids=["another session", "a file whose name begins with this session's"])
def test_up_ignores_a_load_problem_belonging_to_no_session_of_this_name(
        profile, runner, monkeypatch, problem):
    """Only the named session's own failure decides. A broken file nobody asked for is not a
    reason to refuse to start the scenario they did — and `loadProblems` alone cannot tell the
    two apart."""
    state = _fake_proxy(monkeypatch, load_problems=[problem])
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 0, result.output
    assert state["launched"] == [{"session": "orders-outage", "status": 500, "step": 1}]


def test_up_does_not_relaunch_when_the_proxy_cannot_say_what_loaded(profile, runner, monkeypatch):
    """Adopting a proxy started before `loadProblems` existed. Silence is not "the session loaded
    whole": reading it as an empty list relaunches the app against a session nothing checked, and
    prints exactly what a checked one prints."""
    state = _fake_proxy(monkeypatch, not_whole=None)
    _up_with_a_proxy(profile, monkeypatch, state, adopt=True)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["launched"] == [] and state["events"] == []
    assert "cannot say whether 'orders-outage' loaded whole" in result.output
    assert "restart the proxy" in result.output


@pytest.mark.parametrize("bundle_id", ["com.example.Store", None], ids=["simBundleId set", "unset"])
def test_up_does_not_relaunch_when_the_activation_call_fails(
        profile, runner, monkeypatch, bundle_id):
    """The proxy stopped answering between the PAC install and the switch. Launching now would put
    the app in front of exactly the session the caller was trying to replace — and asking for the
    launch by hand is that same wrong launch, typed by a person. The final look still runs: the
    proxy is up and the PAC is installed, and an operator not told so walks away believing the
    network was left alone."""
    state = _fake_proxy(monkeypatch, unreachable=True)
    _up_with_a_proxy(profile, monkeypatch, state, bundle_id=bundle_id)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["launched"] == []
    assert state["events"] == [("activate", "orders-outage")]
    assert "proxy not reachable" in result.output, "the API's own detail is kept"
    assert "NOT relaunched" in result.output
    assert "RELAUNCH THE APP NOW" not in result.output
    assert "INTERCEPT ACTIVE" in result.output


def test_up_keeps_the_reason_when_the_refusal_carries_it_instead_of_printing_it(
        profile, runner, monkeypatch):
    """`_control` prints its own error and exits 1, but a profile mismatch puts the message —
    both fingerprints — *in* the SystemExit. Catching the exit to add "the app was not relaunched"
    must not be what throws that away."""
    state = _fake_proxy(monkeypatch)

    def mismatched(name):
        raise SystemExit("✗ that proxy is running a different profile (a1b2c3 ≠ d4e5f6)")

    _up_with_a_proxy(profile, monkeypatch, state)
    monkeypatch.setattr(cli, "_activate_session", mismatched)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["launched"] == []
    assert "a1b2c3 ≠ d4e5f6" in result.output
    assert "NOT relaunched" in result.output


@pytest.mark.parametrize("bundle_id", ["com.example.Store", None], ids=["simBundleId set", "unset"])
def test_up_no_relaunch_launches_nothing_and_says_who_owns_it(
        profile, runner, monkeypatch, bundle_id):
    """With a UI runner owning the app, `up` launching it is a second launch racing the first —
    and the reminder to launch one by hand is the same instruction to the operator."""
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state, bundle_id=bundle_id)

    result = runner.invoke(cli.cli, ["up", "--no-relaunch"])

    assert result.exit_code == 0, result.output
    assert state["launched"] == []
    assert "relaunch skipped" in result.output
    assert "RELAUNCH THE APP NOW" not in result.output


def test_up_use_with_no_relaunch_still_selects_the_session(profile, runner, monkeypatch):
    """The caller launches the app itself, and needs the scenario live before it does."""
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage", "--no-relaunch"])

    assert result.exit_code == 0, result.output
    assert state["events"] == [("activate", "orders-outage")]
    assert state["active"] == "orders-outage"


def test_up_leaves_the_active_session_alone_when_no_use_is_given(profile, runner, monkeypatch):
    """`up` is not a session switch. Selecting one unasked would rewind a scenario the operator
    set up by hand before running it."""
    state = _fake_proxy(monkeypatch, active="orders-outage")
    _up_with_a_proxy(profile, monkeypatch, state)

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert state["events"] == [("relaunch", "com.example.Store")]


@pytest.mark.parametrize("args", [["--relaunch", "com.example.Store", "--no-relaunch"],
                                  ["--no-relaunch", "--relaunch", "com.example.Store"],
                                  ["--use", "  "]])
def test_up_refuses_a_contradictory_launch_request_before_it_starts_anything(profile, runner, args):
    """Answering "which of these did you mean" after the network has been rewired is too late.
    There is no profile here, and `up` must not get as far as complaining about that."""
    result = runner.invoke(cli.cli, ["up", *args])

    assert result.exit_code == 2, result.output
    assert "no profile" not in result.output, "the usage error must come before any side effect"


# MARK: - Which simulator the CA and the relaunch land on
#
# `booted` is what these tests exist to keep out of the simctl arguments: `simctl help` says that
# with several devices booted it "will choose one of them", and says nothing about which. A CA
# trusted on a device nobody chose reads exactly like a CA trusted on the right one — until the
# app under test rejects the certificate on the device the runner is actually driving.


def _generated_ca(monkeypatch, tmp_path):
    """A CA file on disk, so `trust_ca_in_sim` gets as far as shelling out."""
    cert = tmp_path / "mitmproxy-ca-cert.pem"
    cert.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_ca_cert", lambda: cert)


def test_trust_ca_uses_the_one_booted_simulator_without_being_told(profile, runner, monkeypatch, tmp_path):
    """The convenient case stays convenient: one booted device needs no --simulator."""
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [_PHONE])

    result = runner.invoke(cli.cli, ["trust-ca"])

    assert result.exit_code == 0, result.output
    keychain = simctl_calls(calls, "keychain")
    assert len(keychain) == 1 and _PHONE["udid"] in keychain[0]
    assert "booted" not in keychain[0], "the device must be named by UDID, never left to simctl"
    assert _PHONE["name"] in result.output


def test_trust_ca_refuses_when_more_than_one_simulator_is_booted(profile, runner, monkeypatch, tmp_path):
    """The bug this option exists for: `keychain booted` let simctl pick, so the CA could be
    installed on a device the caller never meant and reported as a success."""
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["trust-ca"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "keychain") == [], "nothing may be trusted while the target is a guess"
    assert _PHONE["udid"] in result.output and _PAD["udid"] in result.output
    assert "--simulator" in result.output


def test_trust_ca_targets_the_named_simulator_among_several_booted(profile, runner, monkeypatch, tmp_path):
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["trust-ca", "--simulator", _PAD["name"]])

    assert result.exit_code == 0, result.output
    assert simctl_calls(calls, "keychain")[0][3] == _PAD["udid"]


def test_trust_ca_accepts_a_udid_in_either_case(profile, runner, monkeypatch, tmp_path):
    """A UDID copied out of an `xcodebuild -destination` line can arrive lowercased."""
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [_PHONE, _PAD])
    assert _PHONE["udid"].lower() != _PHONE["udid"], "this test needs an id with case to fold"

    result = runner.invoke(cli.cli, ["trust-ca", "--simulator", _PHONE["udid"].lower()])

    assert result.exit_code == 0, result.output
    assert simctl_calls(calls, "keychain")[0][3] == _PHONE["udid"]


def test_trust_ca_refuses_a_simulator_that_is_not_booted(profile, runner, monkeypatch, tmp_path):
    """Absent and shut down are different problems with different answers, and neither answer is
    "trust it somewhere else"."""
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [{**_PAD, "state": "Shutdown"}])

    result = runner.invoke(cli.cli, ["trust-ca", "--simulator", _PAD["name"]])

    assert result.exit_code == 1
    assert simctl_calls(calls, "keychain") == []
    assert "not booted" in result.output
    assert f"simctl boot {_PAD['udid']}" in result.output


def test_trust_ca_refuses_a_simulator_that_does_not_exist(profile, runner, monkeypatch, tmp_path):
    """Naming a device that is not there must not quietly fall back to the booted one — that is
    the substituted default this codebase keeps having to remove."""
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [_PHONE])

    result = runner.invoke(cli.cli, ["trust-ca", "--simulator", "iPhone 4"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "keychain") == []
    assert "iPhone 4" in result.output
    assert _PHONE["udid"] not in result.output, "it must not offer the device it was not asked for"


def test_trust_ca_refuses_a_simulator_whose_runtime_is_missing(profile, runner, monkeypatch, tmp_path):
    _generated_ca(monkeypatch, tmp_path)
    unavailable = {**_PAD, "state": "Shutdown", "isAvailable": False,
                   "availabilityError": "runtime profile not found"}
    calls = fake_simctl(monkeypatch, [unavailable])

    result = runner.invoke(cli.cli, ["trust-ca", "--simulator", _PAD["udid"]])

    assert result.exit_code == 1
    assert simctl_calls(calls, "keychain") == []
    assert "runtime profile not found" in result.output


def test_trust_ca_does_not_read_a_failed_device_listing_as_no_simulator(profile, runner, monkeypatch, tmp_path):
    """"I could not ask" is not "there are none": one sends you to the log, the other sends you to
    boot a simulator that may already be running."""
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [_PHONE], list_status=1)

    result = runner.invoke(cli.cli, ["trust-ca"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "keychain") == []
    assert "simctl list devices" in result.output
    assert "no booted simulator" not in result.output


def test_trust_ca_fails_when_simctl_refuses_the_certificate(profile, runner, monkeypatch, tmp_path):
    """A command named for trusting a CA exits non-zero when it trusted nothing."""
    _generated_ca(monkeypatch, tmp_path)
    fake_simctl(monkeypatch, [_PHONE], keychain_status=1)

    result = runner.invoke(cli.cli, ["trust-ca"])

    assert result.exit_code == 1
    assert "keychain failed" in result.output


def _up_on_a_device(profile, monkeypatch, tmp_path, devices, **simctl):
    """`up` for a profile that names an app, with the real CA trust and relaunch reaching a fake
    simctl. Nothing is stubbed between `up` and the simctl arguments the tests below read."""
    _up_after_a_crash(profile, monkeypatch,
                      lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True),
                      stub_trust=False)
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8")
    _generated_ca(monkeypatch, tmp_path)
    return fake_simctl(monkeypatch, devices, **simctl)


def test_up_neither_trusts_nor_relaunches_when_the_simulator_is_ambiguous(profile, runner,
                                                                          monkeypatch, tmp_path):
    """Two booted devices and no choice made: `up` says so and exits non-zero, rather than letting
    simctl pick a device for the CA and pick again for the launch."""
    calls = _up_on_a_device(profile, monkeypatch, tmp_path, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") == [] and simctl_calls(calls, "keychain") == []
    assert "NOT trusted" in result.output and "nothing was relaunched" in result.output
    assert "simulator" not in config.read_runtime(), "no device was used, so none may be recorded"


def test_up_trusts_and_relaunches_on_the_simulator_it_was_given(profile, runner, monkeypatch, tmp_path):
    """Both device operations go to the named device — not to the other booted one, and not to
    `booted` — and `status` can say which device that was."""
    calls = _up_on_a_device(profile, monkeypatch, tmp_path, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["up", "--simulator", _PAD["udid"]])

    assert result.exit_code == 0, result.output
    assert [call[3] for call in simctl_calls(calls, "keychain")] == [_PAD["udid"]]
    assert [call[3] for call in simctl_calls(calls, "launch")] == [_PAD["udid"]]
    assert [call[3] for call in simctl_calls(calls, "terminate")] == [_PAD["udid"]]
    assert config.read_runtime()["simulator"] == {"udid": _PAD["udid"], "name": _PAD["name"]}


def test_up_fails_when_the_ca_cannot_be_trusted_on_the_chosen_device(profile, runner, monkeypatch, tmp_path):
    """The device was resolved, so the failure is simctl's — and `up` is still not allowed to
    report interception it did not establish."""
    _up_on_a_device(profile, monkeypatch, tmp_path, [_PAD], keychain_status=1)

    result = runner.invoke(cli.cli, ["up", "--simulator", _PAD["name"]])

    assert result.exit_code == 1
    assert "keychain failed" in result.output


def test_up_fails_when_the_app_cannot_be_launched_on_the_chosen_device(profile, runner, monkeypatch, tmp_path):
    _up_on_a_device(profile, monkeypatch, tmp_path, [_PAD], launch_status=1)

    result = runner.invoke(cli.cli, ["up", "--simulator", _PAD["name"]])

    assert result.exit_code == 1
    assert f"com.example.Store is not installed in {_PAD['name']}" in result.output


def test_status_reports_the_simulator_the_last_up_used(profile, runner, monkeypatch):
    """Reported from the runtime file rather than by looking again: the question is which device
    this session trusted, and a fresh lookup would name whatever is booted now."""
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 1, "service": "Wi-Fi",
                          "simulator": {"udid": _PAD["udid"], "name": _PAD["name"]}})
    monkeypatch.setattr(cli, "_health",
                        lambda: {"pid": 1, "sessions": ["default"], "activeSession": "default",
                                 "overrideCount": 0, "simBundleId": None, "proxyPort": 8080})
    monkeypatch.setattr(netproxy, "pac_status",
                        lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))

    assert json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)["simulator"] == {
        "udid": _PAD["udid"], "name": _PAD["name"]}
    plain = runner.invoke(cli.cli, ["status"])
    assert _PAD["udid"] in plain.output
    assert "not scoped to it" in plain.output, "device selection must not read as traffic isolation"


def test_up_still_selects_the_session_when_there_is_no_simulator_to_relaunch_on(profile, runner,
                                                                                monkeypatch):
    """`--use` and `--simulator` fail independently.

    A caller left to launch the app by hand still asked for that scenario, so the selection
    happens; the relaunch does not, because there is no device to relaunch on; and `up` exits 1
    about the device rather than reporting a run it did not set up.
    """
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state)
    fake_simctl(monkeypatch, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["events"] == [("activate", "orders-outage")]
    assert state["launched"] == [] and state["devices"] == []
    assert "simulators are booted" in result.output
    assert "nothing was relaunched" in result.output


# MARK: - Eligibility: what counts as a device to choose between


def test_a_booted_watch_does_not_make_the_only_iphone_ambiguous(profile, runner, monkeypatch, tmp_path):
    """A paired watch boots alongside its phone. Counting it as a candidate would make "which one
    did you mean?" the normal state of a Mac running a watch app's companion."""
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [_PHONE, _WATCH])

    result = runner.invoke(cli.cli, ["trust-ca"])

    assert result.exit_code == 0, result.output
    assert [call[3] for call in simctl_calls(calls, "keychain")] == [_PHONE["udid"]]


def test_trust_ca_refuses_when_the_only_booted_simulator_is_not_ios(profile, runner, monkeypatch, tmp_path):
    """Sole booted device is not the same as eligible: the CA belongs in the simulator running the
    iOS app under test, and a watch is not it just because it is the only thing up."""
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [_WATCH])

    result = runner.invoke(cli.cli, ["trust-ca"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "keychain") == []
    assert "no booted iOS simulator" in result.output
    assert _WATCH["name"] in result.output and "watchOS" in result.output


def test_trust_ca_refuses_an_explicitly_named_watch(profile, runner, monkeypatch, tmp_path):
    """Naming it does not make it usable — and the message says what is wrong with the device
    rather than reporting it as missing or unbooted."""
    _generated_ca(monkeypatch, tmp_path)
    calls = fake_simctl(monkeypatch, [_PHONE, _WATCH])

    result = runner.invoke(cli.cli, ["trust-ca", "--simulator", _WATCH["udid"]])

    assert result.exit_code == 1
    assert simctl_calls(calls, "keychain") == []
    assert "watchOS simulator" in result.output
    assert "not booted" not in result.output, "it is booted; that is not the problem"


def test_trust_ca_refuses_a_booted_device_simctl_calls_unavailable(profile, runner, monkeypatch, tmp_path):
    """Booted is simctl's word for the device's state, not a promise it can be used."""
    _generated_ca(monkeypatch, tmp_path)
    broken = {**_PAD, "isAvailable": False, "availabilityError": "runtime profile not found"}
    calls = fake_simctl(monkeypatch, [broken])

    for args in (["trust-ca"], ["trust-ca", "--simulator", _PAD["udid"]]):
        result = runner.invoke(cli.cli, args)
        assert result.exit_code == 1, f"{args}: {result.output}"
        assert simctl_calls(calls, "keychain") == []


# MARK: - `relaunch`: the app's button and the CLI take the same road


def test_relaunch_uses_the_device_up_recorded_not_whatever_is_booted(profile, runner, monkeypatch):
    """The menu-bar app's Relaunch runs this. It used to run `simctl launch booted`, which lets
    simctl choose between two booted devices — half the time the one that never got the CA."""
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8")
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 1, "service": "Wi-Fi",
                          "simulator": {"udid": _PAD["udid"], "name": _PAD["name"]}})
    calls = fake_simctl(monkeypatch, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["relaunch"])

    assert result.exit_code == 0, result.output
    assert [call[3] for call in simctl_calls(calls, "launch")] == [_PAD["udid"]]
    assert [call[3] for call in simctl_calls(calls, "terminate")] == [_PAD["udid"]]
    assert not any("booted" in call for call in calls), "simctl must never be left to choose"


def test_relaunch_refuses_when_the_recorded_device_has_since_shut_down(profile, runner, monkeypatch):
    """The recorded UDID is checked, not trusted: the CA lives on that device, so relaunching
    anywhere else would put the app in front of a certificate it does not trust."""
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8")
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    config.write_runtime({"proxyPid": 1, "service": "Wi-Fi",
                          "simulator": {"udid": _PAD["udid"], "name": _PAD["name"]}})
    calls = fake_simctl(monkeypatch, [_PHONE, {**_PAD, "state": "Shutdown"}])

    result = runner.invoke(cli.cli, ["relaunch"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") == [], "it must not relaunch on the other booted device"
    assert "not booted" in result.output and _PAD["udid"] in result.output


def test_relaunch_refuses_rather_than_pick_between_two_booted_devices(profile, runner, monkeypatch):
    """With no run recorded — no `up` yet, or one that failed to resolve a device — the rule is
    `up`'s own: the single candidate, or a refusal that lists them."""
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8")
    calls = fake_simctl(monkeypatch, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["relaunch"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") == []
    assert "--simulator" in result.output


def test_relaunch_says_what_is_missing_when_no_bundle_id_can_be_found(profile, runner, monkeypatch):
    """`--relaunch`'s failure mode, in a command whose only job is the launch: it must not exit 0
    having launched nothing."""
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    calls = fake_simctl(monkeypatch, [_PHONE])

    result = runner.invoke(cli.cli, ["relaunch"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") == []
    assert "simBundleId" in result.output


def test_relaunch_reports_a_launch_simctl_refused(profile, runner, monkeypatch):
    calls = fake_simctl(monkeypatch, [_PHONE], launch_status=1)

    result = runner.invoke(cli.cli, ["relaunch", "com.example.Store"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") != []
    assert "com.example.Store is not installed" in result.output
