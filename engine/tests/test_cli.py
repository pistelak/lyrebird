"""Teardown behaviour.

`down` is the command that has to work when everything else has gone wrong — the proxy crashed,
the runtime file is unreadable, the machine was rebooted mid-session. If it silently does nothing,
the user is left with a PAC pointing at a dead port and no indication why.
"""


import json
import os

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
    monkeypatch.setattr(cli, "trust_ca_in_sim", lambda: (True, "trusted"))
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
    assert locked == [cli.fcntl.LOCK_EX], "the restore must wait for any `up` in progress"
    assert restored == [], "the replacement owns the network now"
    assert config.runtime_file().exists(), "the replacement's runtime file must survive"


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


_LIVE = {"pid": 4321, "activeSession": "default", "sessions": ["default"], "overrideCount": 0,
         "proxyPort": 8080}


def _up_after_a_crash(profile, monkeypatch, pac_status, *, service="Wi-Fi", health=None):
    """`up` with the proxy dead, a runtime file the watchdog kept (it could not restore), and the
    PAC in whatever state `pac_status` reports. Everything that shells out is stubbed. `health` is
    the sequence of answers `_health` gives; by default dead at the first look and live after."""
    import subprocess

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
    monkeypatch.setattr(cli, "trust_ca_in_sim", lambda: (True, "trusted"))
    monkeypatch.setattr(netproxy, "active_service", lambda: service)
    monkeypatch.setattr(netproxy, "pac_status", pac_status)
    monkeypatch.setattr(netproxy, "set_pac", lambda service: None)
    monkeypatch.setattr(cli, "_pid_is_ours", lambda pid, marker: True)


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
    assert json.loads(result.output)["answers"] == [{"id": "ovr_a", "active": True, "count": 2}]


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

    Each state overlays the defaults below; `None` means the rule is gone from the session."""
    return _polling(states, lambda state: _health_payload(
        answers=[] if state is None else [{"id": "ovr_a", "active": True, "count": 0, **state}]))


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
