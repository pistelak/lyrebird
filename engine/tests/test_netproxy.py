"""macOS network-service and PAC parsing.

This is the module that rewrites the user's system proxy configuration. The seam is
`subprocess.run`, so everything above it — including `_run`'s translation of a non-zero exit into
`NetworkSetupError` — is exercised without touching the network or shelling out.
"""

import subprocess

import pytest

import netproxy


def fake_run(monkeypatch, stdout="", returncode=0, stderr=""):
    """Replace the one subprocess seam. Records the argv of every call for assertions.

    Only the exit status is faked; turning a non-zero one into `NetworkSetupError` is left to the
    real `_run`, which is the point of patching this far down.
    """
    calls = []

    def _subprocess_run(args, *rest, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    return calls


# MARK: - pac_status parsing

def test_pac_status_reads_url_and_enabled(monkeypatch, hosts):
    fake_run(monkeypatch, stdout=f"URL: {netproxy.pac_url()}\nEnabled: Yes\n")
    status = netproxy.pac_status("Wi-Fi")
    assert status.url == netproxy.pac_url()
    assert status.enabled is True
    assert status.ours is True


def test_pac_status_raises_when_networksetup_fails(monkeypatch):
    """An unreadable PAC is not an absent one. Read as `("", False, False)`, a failed query made
    `down` say "not ours — left untouched" over a PAC it never saw."""
    fake_run(monkeypatch, stdout="", returncode=1, stderr="** Error: The parameters were not valid.")
    with pytest.raises(netproxy.NetworkSetupError):
        netproxy.pac_status("Wi-Fi")


def test_pac_status_treats_null_as_no_url(monkeypatch):
    """macOS prints `(null)` for an unset PAC. Read literally it would look like a foreign PAC,
    and `down` would decline to clear it."""
    fake_run(monkeypatch, stdout="URL: (null)\nEnabled: No\n")
    status = netproxy.pac_status("Wi-Fi")
    assert status.url == ""
    assert status.enabled is False


# MARK: - intercepting()

def test_intercepting_requires_both_enabled_and_ours(monkeypatch):
    fake_run(monkeypatch, stdout=f"URL: {netproxy.pac_url()}\nEnabled: No\n")
    assert netproxy.intercepting("Wi-Fi") is False

    fake_run(monkeypatch, stdout="URL: http://proxy.example.com/corp.pac\nEnabled: Yes\n")
    assert netproxy.intercepting("Wi-Fi") is False

    fake_run(monkeypatch, stdout=f"URL: {netproxy.pac_url()}\nEnabled: Yes\n")
    assert netproxy.intercepting("Wi-Fi") is True


# MARK: - active_service parsing

SERVICE_ORDER = """An asterisk (*) denotes that a network service is disabled.
(1) Wi-Fi
(Hardware Port: Wi-Fi, Device: en0)

(2) Thunderbolt Bridge
(Hardware Port: Thunderbolt Bridge, Device: bridge0)

"""


def test_active_service_maps_the_default_route_to_a_service_name(monkeypatch):
    outputs = iter(["   gateway: 192.0.2.1\n  interface: en0\n", SERVICE_ORDER])

    def _subprocess_run(args, *rest, **kwargs):
        return subprocess.CompletedProcess(args, 0, next(outputs), "")

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    assert netproxy.active_service() == "Wi-Fi"


def test_active_service_is_none_without_a_default_route(monkeypatch):
    fake_run(monkeypatch, stdout="")
    assert netproxy.active_service() is None


def test_active_service_is_none_when_no_service_matches(monkeypatch):
    outputs = iter(["  interface: utun9\n", SERVICE_ORDER])

    def _subprocess_run(args, *rest, **kwargs):
        return subprocess.CompletedProcess(args, 0, next(outputs), "")

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    assert netproxy.active_service() is None


# MARK: - Failures are raised, not swallowed

def test_set_pac_raises_when_the_setting_does_not_take(monkeypatch):
    """networksetup can exit 0 and not apply the change; the read-back is what catches that."""
    fake_run(monkeypatch, stdout="URL: (null)\nEnabled: No\n", returncode=0)
    with pytest.raises(netproxy.NetworkSetupError):
        netproxy.set_pac("Wi-Fi")


def fake_networksetup(monkeypatch, url="", enabled=False, *, fail=(), inert=()):
    """A `networksetup` with state: `-set…` calls change what `-getautoproxyurl` reports next.

    `fail` names verbs that exit non-zero; `inert` names verbs that exit 0 without applying —
    the behaviour `set_pac`'s read-back exists for. Records the argv of every call.
    """
    state = {"url": url, "enabled": enabled}
    calls = []

    def _subprocess_run(args, *rest, **kwargs):
        calls.append(args)
        verb = args[1]
        if verb in fail:
            return subprocess.CompletedProcess(args, 1, "", "failed")
        if verb == "-getautoproxyurl":
            out = f"URL: {state['url'] or '(null)'}\nEnabled: {'Yes' if state['enabled'] else 'No'}\n"
            return subprocess.CompletedProcess(args, 0, out, "")
        if verb not in inert:
            if verb == "-setautoproxyurl":
                state["url"] = args[3]
                state["enabled"] = True   # as macOS does: setting the URL switches the PAC on
            elif verb == "-setautoproxystate":
                state["enabled"] = args[3] == "on"
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    return calls, state


def test_restore_pac_reinstates_a_previous_url(monkeypatch, hosts):
    calls, state = fake_networksetup(monkeypatch, netproxy.pac_url(), True)
    netproxy.restore_pac("Wi-Fi", "http://proxy.example.com/corp.pac", enabled=True)
    assert ["networksetup", "-setautoproxyurl", "Wi-Fi", "http://proxy.example.com/corp.pac"] in calls
    assert ["networksetup", "-setautoproxystate", "Wi-Fi", "on"] in calls
    assert state == {"url": "http://proxy.example.com/corp.pac", "enabled": True}


@pytest.mark.parametrize("ours_enabled", [True, False])
@pytest.mark.parametrize("recorded_flag", [True, False])
def test_restore_pac_with_no_previous_url_only_switches_off(monkeypatch, hosts, ours_enabled, recorded_flag):
    """macOS rejects an empty URL, so "there was nothing before" means disable, not clear — and
    it means disable whatever flag was recorded next to the empty URL. Honouring `enabled: true`
    there once left Lyrebird's own PAC switched on after `down` had said "direct networking
    restored"."""
    calls, state = fake_networksetup(monkeypatch, netproxy.pac_url(), ours_enabled)
    netproxy.restore_pac("Wi-Fi", "", enabled=recorded_flag)
    assert [c for c in calls if c[1].startswith("-set")] == [["networksetup", "-setautoproxystate", "Wi-Fi", "off"]]
    assert state["enabled"] is False


def test_restore_pac_raises_when_the_setting_does_not_take(monkeypatch, hosts):
    """The same read-back `set_pac` has: exit 0 from `networksetup` is not evidence."""
    fake_networksetup(monkeypatch, netproxy.pac_url(), True, inert=("-setautoproxyurl", "-setautoproxystate"))
    with pytest.raises(netproxy.NetworkSetupError):
        netproxy.restore_pac("Wi-Fi", "http://proxy.example.com/corp.pac", enabled=True)


def test_restore_pac_sets_the_state_after_the_url_because_setting_the_url_enables_it(monkeypatch, hosts):
    """macOS switches the PAC on when its URL is set. State-then-URL restored a disabled corporate
    PAC as an enabled one and then failed its own read-back."""
    calls, state = fake_networksetup(monkeypatch, netproxy.pac_url(), True)
    netproxy.restore_pac("Wi-Fi", "http://proxy.example.com/corp.pac", enabled=False)
    verbs = [c[1] for c in calls if c[1].startswith("-set")]
    assert verbs == ["-setautoproxyurl", "-setautoproxystate"]
    assert state == {"url": "http://proxy.example.com/corp.pac", "enabled": False}


@pytest.mark.parametrize("ours_enabled", [True, False])
@pytest.mark.parametrize("enabled", [True, False])
def test_restore_pac_partial_failure_leaves_the_url_ours_or_the_target(monkeypatch, hosts, ours_enabled, enabled):
    """Whatever fails, the URL afterwards is one the retry in `cli._restore_previous_pac`
    recognises: still ours, or already the one being restored. Never a third thing."""
    target = "http://proxy.example.com/corp.pac"
    for failing in ("-setautoproxyurl", "-setautoproxystate"):
        _, state = fake_networksetup(monkeypatch, netproxy.pac_url(), ours_enabled, fail=(failing,))
        with pytest.raises(netproxy.NetworkSetupError):
            netproxy.restore_pac("Wi-Fi", target, enabled=enabled)
        assert state["url"] in (netproxy.pac_url(), target), failing


# MARK: - A command that never returns

def test_every_command_is_bounded(monkeypatch, hosts):
    """The bound is on `subprocess.run` itself, so it is asserted there. Without it a hung
    `networksetup` took its whole caller with it — including `/health`, which runs on the proxy's
    event loop and whose silence the watchdog reads as a dead proxy worth restoring over."""
    timeouts = []

    def _subprocess_run(args, *rest, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        out = f"URL: {netproxy.pac_url()}\nEnabled: Yes\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    netproxy.pac_status("Wi-Fi")
    assert timeouts == [netproxy._COMMAND_TIMEOUT]


TIMES_OUT = {
    "pac_status": lambda: netproxy.pac_status("Wi-Fi"),
    "set_pac": lambda: netproxy.set_pac("Wi-Fi"),
    "restore_pac": lambda: netproxy.restore_pac("Wi-Fi", "http://proxy.example.com/corp.pac", True),
    "active_service": netproxy.active_service,
}


@pytest.mark.parametrize("name", list(TIMES_OUT))
def test_a_command_that_does_not_finish_is_raised_not_read_as_no_pac(monkeypatch, hosts, name):
    """A `networksetup` that never answered has said nothing about the PAC. Reported as empty
    output it would parse as "no PAC", which is the mistake `pac_status` exists to prevent — and
    the one that makes `down` delete the only record of what to put back."""
    def _subprocess_run(args, *rest, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    with pytest.raises(netproxy.NetworkSetupError) as raised:
        TIMES_OUT[name]()
    assert "did not finish within 5s" in str(raised.value)


# MARK: - The PAC we advertise
