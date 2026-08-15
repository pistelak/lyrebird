"""Host scoping. The three mechanisms below must agree, because each one is a separate chance to
intercept traffic the user never asked us to touch."""

import re
from pathlib import Path

import pytest

import config

# MARK: - Host validation

@pytest.mark.parametrize("host", [
    "https://api.example.com",   # scheme
    "api.example.com/path",      # path
    "api.example.com:443",       # port
    "*.example.com",             # wildcard
    "api example.com",           # whitespace
    "api.example.com\"});alert(1);//",  # PAC JavaScript injection attempt
    "-leading-dash.example.com",
    "",
])
def test_invalid_hosts_are_rejected(host):
    with pytest.raises(ValueError):
        config.validate_host(host)


def test_hosts_are_lowercased():
    assert config.validate_host("API.Example.COM") == "api.example.com"


# MARK: - Fail-closed profile loading

def test_empty_host_list_means_intercept_nothing(profile):
    """An explicitly empty list must not fall back to a built-in default."""
    (profile / "profile.json").write_text('{"hosts": []}')
    config.reload_profile()
    assert config.INTERCEPT_HOSTS == []
    assert config.is_intercepted_host("api.example.com") is False


def test_empty_host_list_yields_a_never_matching_allow_hosts(profile):
    """An empty allow_hosts means 'no restriction' to mitmproxy — the opposite of what we want."""
    (profile / "profile.json").write_text('{"hosts": []}')
    config.reload_profile()
    patterns = config.allow_hosts_regexes()
    assert patterns and not any(re.search(p, "api.example.com:443") for p in patterns)


def test_empty_host_list_yields_a_direct_only_pac(profile):
    (profile / "profile.json").write_text('{"hosts": []}')
    config.reload_profile()
    pac = config.pac_contents()
    assert "PROXY" not in pac and "DIRECT" in pac


def test_malformed_profile_aborts_loudly(profile):
    (profile / "profile.json").write_text("{not json")
    with pytest.raises(SystemExit):
        config.load_profile()


def test_bad_host_in_profile_aborts_loudly(profile):
    (profile / "profile.json").write_text('{"hosts": ["*.example.com"]}')
    with pytest.raises(SystemExit):
        config.load_profile()


def test_missing_profile_is_reported_not_defaulted(profile):
    loaded = config.load_profile()
    assert loaded.exists is False and loaded.hosts == []


# MARK: - The three mechanisms agree

def test_addon_matching_is_exact(hosts):
    assert config.is_intercepted_host("api.example.com") is True
    assert config.is_intercepted_host("api.example.com:443") is True
    assert config.is_intercepted_host("sub.api.example.com") is False
    assert config.is_intercepted_host("api.example.com.attacker.test") is False
    assert config.is_intercepted_host("evil-api.example.com") is False


def test_allow_hosts_is_anchored(hosts):
    patterns = config.allow_hosts_regexes()

    def decrypts(candidate):
        return any(re.search(pattern, candidate) for pattern in patterns)

    assert decrypts("api.example.com:443") is True
    assert decrypts("api.example.com.attacker.test:443") is False
    assert decrypts("evil-api.example.com:443") is False


def test_pac_matches_exactly_and_escapes_hosts(hosts):
    pac = config.pac_contents()
    assert 'lower === "api.example.com"' in pac
    assert "toLowerCase" in pac, "DNS is case-insensitive; the PAC must be too"
    assert "shExpMatch" not in pac  # prefix globbing would be looser than the other two mechanisms


def test_pac_advertises_the_proxy_host_not_the_control_host(hosts):
    assert f"PROXY {config.PROXY_ADVERTISED_HOST}:{config.PROXY_PORT}" in config.pac_contents()


# MARK: - Default locations
#
# Profiles are configuration and belong in ~/.config. Nothing the tool writes for itself does, and
# the three places it writes differ in what is allowed to destroy them: state and the CA must
# survive, the catalog may be discarded, the log is for a person to read.

def test_default_profile_lives_in_config_home(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert config._default_profile() == Path.home() / ".config" / "lyrebird"


def test_default_profile_honours_xdg_config_home(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert config._default_profile() == Path("/tmp/xdg") / "lyrebird"


def test_each_default_root_is_the_macos_directory_for_its_lifetime():
    """Not cosmetic: `tmutil` excludes Caches and Logs from Time Machine and includes Application
    Support, so the directory a file lands in decides whether it is backed up forever."""
    roots = {
        "Application Support/Lyrebird": config._default_state_root(),
        "Caches/com.lyrebird.Lyrebird": config._default_cache_root(),
        "Logs/Lyrebird": config._default_log_root(),
    }
    for expected, root in roots.items():
        assert str(root).endswith(f"Library/{expected}"), f"{root} is not Library/{expected}"
        assert "/.config" not in str(root)
    assert len(set(roots.values())) == 3, "the three roots must be distinct"


def test_state_dir_override_collapses_all_three(monkeypatch, tmp_path):
    """One variable has to relocate everything, or the tests leak into a real Library directory."""
    monkeypatch.setenv("LYREBIRD_STATE_DIR", str(tmp_path / "elsewhere"))
    config.configure()

    for path in (config.STATE_FILE, config.CATALOG_FILE, config.LOG_FILE,
                 config.runtime_file(), config.lock_file(), config.mitmproxy_confdir()):
        assert config.STATE_ROOT in path.parents, f"{path} escaped the override"


def test_state_paths_stay_out_of_the_profile(profile):
    """A profile kept in git must never have runtime files written into it."""
    for path in (config.STATE_FILE, config.CATALOG_FILE, config.LOG_FILE,
                 config.runtime_file(), config.lock_file(), config.mitmproxy_confdir()):
        assert config.PROFILE_DIR not in path.parents, f"{path} is inside the profile"


def test_validate_host_rejects_non_strings():
    """A JSON number used to be stringified into an accepted host."""
    for value in (123, None, ["api.example.com"], True):
        with pytest.raises(ValueError):
            config.validate_host(value)


def test_validate_host_rejects_bare_addresses():
    # 203.0.113.x is the RFC 5737 documentation range. validate_host rejects any all-numeric
    # value, so which address it is makes no difference to the assertion — it is a documentation
    # address so that no fixture here names something somebody actually routes to.
    for value in ("127.0.0.1", "203.0.113.1"):
        with pytest.raises(ValueError):
            config.validate_host(value)




def test_all_three_matchers_agree_on_case(hosts):
    """A client may send any case. The addon check lowercases, so allow_hosts and the PAC must too
    — otherwise a request routes to the proxy and is then silently tunnelled undecrypted."""
    pac = config.pac_contents()
    for host in ("api.example.com", "API.EXAMPLE.COM", "Api.Example.Com"):
        addon = config.is_intercepted_host(host)
        allow = any(re.search(p, f"{host}:443") for p in config.allow_hosts_regexes())
        assert addon is True and allow is True, f"{host}: addon={addon} allow_hosts={allow}"
    assert "toLowerCase" in pac
