"""Shared fixtures. Every test runs against a throwaway profile and state directory, so nothing
here can touch a real profile on the developer's machine."""

import os
import tempfile

import pytest
from click.testing import CliRunner

# config resolves its paths at import from the environment. Pointed at a directory that does not
# exist, nothing a test does by accident — a bare `configure()`, a `reload_profile()` — can reach
# a contributor's real ~/.config/lyrebird, and the state directory keeps every write in a temp tree.
os.environ.setdefault("LYREBIRD_PROFILE", os.path.join(tempfile.gettempdir(), "lyrebird-tests-absent"))
os.environ.setdefault("LYREBIRD_STATE_DIR", os.path.join(tempfile.gettempdir(), "lyrebird-tests-state"))

# Every engine import must follow the environment defaults above: `config` resolves its paths at
# import, and the rest import it.
import api
import config
import netproxy
import store
import supervisor


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A temporary profile + state directory, with config re-resolved to point at them."""
    profile_dir = tmp_path / "profile"
    (profile_dir / "scenarios").mkdir(parents=True)
    monkeypatch.setenv("LYREBIRD_STATE_DIR", str(tmp_path / "state"))
    config.configure(str(profile_dir))
    yield profile_dir
    # Reload as well as reconfigure: several tests call reload_profile() directly, and leaving
    # INTERCEPT_HOSTS pointing at a deleted temp profile makes later tests order-dependent.
    config.configure()
    config.reload_profile()


@pytest.fixture
def hosts(profile):
    """A profile configured to intercept a single host."""
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8"
    )
    config.reload_profile()
    yield ["api.example.com"]
    config.reload_profile()


@pytest.fixture(autouse=True)
def _restore_resolved_paths():
    """`config.configure()` rebinds module globals, so a test that calls it leaves its profile and
    state paths in place for everything that runs afterwards. Autouse and declared here so it tears
    down after monkeypatch has put the environment back."""
    yield
    config.configure()


@pytest.fixture(autouse=True)
def _no_real_watchdog(monkeypatch):
    """`up` starts the watchdog with `subprocess.Popen`, and a subprocess inherits no monkeypatch:
    it is a fresh interpreter running the real `netproxy` against the real `networksetup`. The
    environment above gives it a temporary profile and state directory but sets no control port,
    so it resolves the default 8088 — and for as long as a real proxy answers health there, the
    loop never reaches its only `return` and outlives pytest, repairing the PAC on the
    contributor's own Wi-Fi.

    Doubled here rather than in each test that calls `up`: a test that forgets leaves a process
    behind and passes anyway, which is not a failure anything would report. Pinned by
    `test_up_spawns_no_real_watchdog_subprocess`."""
    monkeypatch.setattr(supervisor, "_spawn_watchdog", lambda service: 4242)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fake_network(monkeypatch):
    """A Wi-Fi service whose PAC is currently ours and enabled."""
    state = {"service": "Wi-Fi", "restored": None, "terminated": []}

    monkeypatch.setattr(netproxy, "active_service", lambda: state["service"])
    monkeypatch.setattr(netproxy, "pac_status", lambda service: netproxy.PacStatus(netproxy.pac_url(), True, True))

    def restore(service, url, enabled):
        state["restored"] = (service, url, enabled)

    monkeypatch.setattr(netproxy, "restore_pac", restore)
    monkeypatch.setattr(supervisor, "_terminate", lambda pid, marker: state["terminated"].append((pid, marker)))
    return state


@pytest.fixture
def offline(monkeypatch):
    """Fails the test if anything reaches the proxy, the network or the store."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline command must not talk to the proxy or construct a Store")

    monkeypatch.setattr(api, "_control", refuse)
    monkeypatch.setattr(api, "_health", refuse)
    monkeypatch.setattr(netproxy, "active_service", refuse)
    monkeypatch.setattr(store.Store, "__init__", refuse)
