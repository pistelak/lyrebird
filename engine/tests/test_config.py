"""Host scoping. The three mechanisms below must agree, because each one is a separate chance to
intercept traffic the user never asked us to touch."""

import builtins
import os
import re
import tempfile
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
# Profiles are configuration and belong in ~/.config, as do the sessions and presets you save into
# one. Nothing the tool writes for its own purposes does, and the three places it writes differ in
# what may destroy them: state and the CA must survive, the catalog may be discarded, the log is
# for a person to read.

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


def test_configure_wires_each_path_to_its_own_root(monkeypatch, tmp_path):
    """Naming the right directories is worth nothing if `configure` does not use them."""
    monkeypatch.delenv("LYREBIRD_STATE_DIR", raising=False)
    config.configure(str(tmp_path / "profile"))

    assert config.STATE_FILE.is_relative_to(config._default_state_root())
    assert config.CATALOG_FILE.is_relative_to(config._default_cache_root())
    assert config.LOG_FILE.is_relative_to(config._default_log_root())


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


def test_the_same_profile_gets_one_fingerprint_however_it_is_named(monkeypatch, tmp_path):
    """State is keyed by a hash of the profile path, so two spellings of one directory would mean
    two active-session pointers and two logs for the same profile."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(link))
    monkeypatch.setenv("LYREBIRD_STATE_DIR", str(tmp_path / "state"))
    # conftest sets LYREBIRD_PROFILE for the whole session, which would make the "implicit" call
    # below take the explicit branch and quietly test nothing.
    monkeypatch.delenv("LYREBIRD_PROFILE", raising=False)

    config.configure()                                  # implicit: XDG_CONFIG_HOME/lyrebird
    implicit = config.PROFILE_FINGERPRINT
    config.configure(str(link / "lyrebird"))            # explicit: the same directory, named
    assert config.PROFILE_FINGERPRINT == implicit


# MARK: - Private writes
#
# atomic_write promises, in its first sentence, that what it writes is never world-readable and
# never observed half-written. It used to write first and chmod second, so the payload sat at 0644
# for the width of that gap.

def test_atomic_write_creates_the_file_already_private(tmp_path, monkeypatch):
    """Read the mode off the descriptor while it is open, because the finished file cannot tell you.

    The original implementation chmodded after writing and so also ended at 0600 — an assertion
    about the final mode passes either way and proves nothing about the window in between.
    """
    seen = {}
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        if isinstance(file, int):
            seen["mode"] = os.fstat(file).st_mode & 0o777
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    config.atomic_write(tmp_path / "secret.json", '{"token": "value"}')

    assert seen["mode"] == 0o600, "content was written into a file others could already read"


def test_atomic_write_leaves_the_finished_file_private(tmp_path):
    target = tmp_path / "secret.json"
    config.atomic_write(target, '{"token": "value"}')

    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text(encoding="utf-8") == '{"token": "value"}'


def test_atomic_write_closes_the_descriptor_when_the_wrapper_cannot_be_built(tmp_path, monkeypatch):
    """The failure path must close the descriptor exactly once and surface the original error.

    Two assertions, because neither alone is enough. Counting closes catches a cleanup that never
    happens — unlinking the temporary hides the leaked descriptor, so nothing else would notice.
    The sentinel catches the opposite error, a cleanup that closes a descriptor it no longer owns:
    both `closefd` settings end with the descriptor closed, the LookupError preserved and the
    temporary gone, and the EBADF a double close would raise is suppressed, so what separates them
    is *when* the number is released. Under `closefd=True` the real `open` frees it as it fails,
    the sentinel claimed immediately afterwards is handed that same number, and cleanup closes it.
    """
    owned, sentinel, closes = {}, {}, []
    real_open, real_close = builtins.open, os.close

    def record_close(descriptor):
        closes.append(descriptor)
        real_close(descriptor)

    def unusable_encoding(file, *args, **kwargs):
        if not isinstance(file, int):
            return real_open(file, *args, **kwargs)
        owned["fd"] = file
        kwargs["encoding"] = "definitely-not-a-codec"
        try:
            return real_open(file, *args, **kwargs)
        except LookupError:
            sentinel["fd"] = os.open(os.devnull, os.O_RDONLY)
            raise

    monkeypatch.setattr(builtins, "open", unusable_encoding)
    monkeypatch.setattr(os, "close", record_close)
    with pytest.raises(LookupError):
        config.atomic_write(tmp_path / "secret.json", "payload")
    monkeypatch.undo()

    survived = True
    try:
        os.fstat(sentinel["fd"])
    except OSError:
        survived = False
    else:
        os.close(sentinel["fd"])

    assert closes.count(owned["fd"]) == 1, f"owned descriptor closed {closes.count(owned['fd'])}x"
    assert survived, "cleanup closed a descriptor it no longer owned"
    assert not list(tmp_path.iterdir()), "a temporary file was left behind"

def test_atomic_write_reports_the_write_failure_not_the_cleanup_failure(tmp_path, monkeypatch):
    """If tidying up also fails, the caller still needs to know why the write did."""
    def refuse_replace(self, target):
        raise OSError("replace failed")

    def refuse_unlink(self, missing_ok=False):
        raise OSError("unlink failed")

    monkeypatch.setattr(Path, "replace", refuse_replace)
    monkeypatch.setattr(Path, "unlink", refuse_unlink)

    with pytest.raises(OSError, match="replace failed"):
        config.atomic_write(tmp_path / "out.json", "payload")


def test_atomic_write_refuses_to_write_through_a_planted_symlink(tmp_path):
    """A predictable temporary name is guessable by anything running as you; a unique one is not."""
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    target = tmp_path / "out.json"
    decoy = target.with_suffix(target.suffix + f".tmp{os.getpid()}")
    decoy.symlink_to(victim)

    config.atomic_write(target, "payload")

    assert victim.read_text() == "untouched", "the write followed a symlink out of its directory"
    assert target.read_text() == "payload"


def test_atomic_write_uses_a_unique_temporary_each_time(tmp_path, monkeypatch):
    """The name used to be derived from the pid, which every thread in a process shares, so two
    concurrent writes raced for one file. A unique name also cannot be lain in wait at."""
    target = tmp_path / "out.json"
    names = set()
    real_mkstemp = tempfile.mkstemp

    def record(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        names.add(name)
        return descriptor, name

    monkeypatch.setattr(tempfile, "mkstemp", record)
    for _ in range(3):
        config.atomic_write(target, "payload")

    assert len(names) == 3, "the temporary name was reused"
    assert target.read_text() == "payload"
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"], "a temporary file was left behind"


@pytest.mark.parametrize("mask", [0o022, 0o077, 0o177, 0o777])
def test_atomic_write_mode_does_not_depend_on_the_umask(tmp_path, mask):
    """`mkstemp` requests 0600 and the umask subtracts from it: under 0o777 the file came out 000,
    which is not just a wrong number — nothing could read the state back afterwards."""
    target = tmp_path / "secret.json"
    previous = os.umask(mask)
    try:
        config.atomic_write(target, "payload")
    finally:
        os.umask(previous)

    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text() == "payload"


def test_atomic_write_reports_the_write_failure_not_a_failing_close(tmp_path, monkeypatch):
    """A close that fails while unwinding must not become the error the caller sees."""
    real_open = builtins.open

    closes = []

    class FailsBothWays:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            raise OSError("close failed")

        def close(self):
            closes.append(True)
            raise OSError("close failed")

        def write(self, _text):
            raise OSError("write failed")

    def hand_back_a_broken_wrapper(file, *args, **kwargs):
        if isinstance(file, int):
            return FailsBothWays()
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", hand_back_a_broken_wrapper)
    with pytest.raises(OSError, match="write failed"):
        config.atomic_write(tmp_path / "out.json", "payload")

    assert closes == [True], "the wrapper was not closed while unwinding"


def test_atomic_write_aborts_if_the_descriptor_will_not_close(tmp_path, monkeypatch):
    """The descriptor is closed before the rename, so a close failure must stop the write there.

    That ordering is the point: a close is where buffered data finally reports failure, and the
    destination must still be untouched when it does.
    """
    target = tmp_path / "out.json"
    target.write_text("previous")

    attempts = []
    real_close = os.close

    def close_then_report_failure(descriptor):
        attempts.append(descriptor)
        real_close(descriptor)  # released, as a real close is even when it reports an error
        raise OSError("descriptor close failed")

    monkeypatch.setattr(os, "close", close_then_report_failure)
    with pytest.raises(OSError, match="descriptor close failed"):
        config.atomic_write(target, "payload")
    monkeypatch.undo()

    assert len(attempts) == 1, f"closed {len(attempts)} times; the second may hit someone else's fd"
    assert target.read_text() == "previous", "the destination was replaced despite a failed write"
