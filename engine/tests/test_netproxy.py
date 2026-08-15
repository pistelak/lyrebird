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


def test_restore_pac_reinstates_a_previous_url(monkeypatch):
    calls = fake_run(monkeypatch, stdout="URL: (null)\nEnabled: No\n")
    netproxy.restore_pac("Wi-Fi", "http://proxy.example.com/corp.pac", enabled=True)
    assert ["networksetup", "-setautoproxyurl", "Wi-Fi", "http://proxy.example.com/corp.pac"] in calls
    assert ["networksetup", "-setautoproxystate", "Wi-Fi", "on"] in calls


def test_restore_pac_with_no_previous_url_only_switches_off(monkeypatch):
    """macOS rejects an empty URL, so "there was nothing before" means disable, not clear."""
    calls = fake_run(monkeypatch, stdout="URL: (null)\nEnabled: No\n")
    netproxy.restore_pac("Wi-Fi", "", enabled=False)
    assert calls == [["networksetup", "-setautoproxystate", "Wi-Fi", "off"]]


# MARK: - The PAC we advertise
