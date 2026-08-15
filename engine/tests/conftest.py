"""Shared fixtures. Every test runs against a throwaway profile and state directory, so nothing
here can touch a real profile on the developer's machine."""

import os
import tempfile

import pytest

# config resolves paths and reads profile.json at import, and load_profile raises SystemExit on a
# malformed profile. Without this, a contributor whose own ~/.config/lyrebird/profile.json is
# broken gets a suite that dies during collection, blaming their config for our test run.
# Pointed at a directory that does not exist, the import takes the "no profile" branch.
os.environ.setdefault("LYREBIRD_PROFILE", os.path.join(tempfile.gettempdir(), "lyrebird-tests-absent"))
os.environ.setdefault("LYREBIRD_STATE_DIR", os.path.join(tempfile.gettempdir(), "lyrebird-tests-state"))

import config  # must follow the environment defaults above


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A temporary profile + state directory, with config re-resolved to point at them."""
    profile_dir = tmp_path / "profile"
    (profile_dir / "sessions").mkdir(parents=True)
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
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8")
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
