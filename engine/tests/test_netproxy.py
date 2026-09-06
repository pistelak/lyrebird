"""macOS network-service and PAC parsing.

This is the module that rewrites the user's system proxy configuration, and until now it had no
tests at all. Both parsers sit behind a single `_run` seam, so they can be exercised without
touching the network or shelling out.
"""

import subprocess

import pytest

import netproxy


def fake_run(monkeypatch, stdout="", returncode=0, stderr=""):
    """Replace the one subprocess seam. Records the argv of every call for assertions."""
    calls = []

    def _run(args, check=False):
        calls.append(args)
        result = subprocess.CompletedProcess(args, returncode, stdout, stderr)
        if check and returncode != 0:
            raise netproxy.NetworkSetupError(f"`{' '.join(args)}` failed: {stderr or returncode}")
        return result

    monkeypatch.setattr(netproxy, "_run", _run)
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

    def _run(args, check=False):
        return subprocess.CompletedProcess(args, 0, next(outputs), "")

    monkeypatch.setattr(netproxy, "_run", _run)
    assert netproxy.active_service() == "Wi-Fi"


def test_active_service_is_none_without_a_default_route(monkeypatch):
    fake_run(monkeypatch, stdout="")
    assert netproxy.active_service() is None


def test_active_service_is_none_when_no_service_matches(monkeypatch):
    outputs = iter(["  interface: utun9\n", SERVICE_ORDER])

    def _run(args, check=False):
        return subprocess.CompletedProcess(args, 0, next(outputs), "")

    monkeypatch.setattr(netproxy, "_run", _run)
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

    def _run(args, check=False):
        calls.append(args)
        verb = args[1]
        if verb in fail:
            if check:
                raise netproxy.NetworkSetupError(f"`{' '.join(args)}` failed: 1")
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

    monkeypatch.setattr(netproxy, "_run", _run)
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


# MARK: - The PAC we advertise
