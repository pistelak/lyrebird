"""How `bin/lyrebird` finds its own engine.

The launcher is the only part of Lyrebird that runs before the venv exists, so its failures are
shell errors rather than Python ones — and the documented install puts a *symlink* on PATH. These
tests build a throwaway checkout and invoke the real script through such a link, because that is
the shape that used to send it looking for `~/.local/engine` and die inside `cd`.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the launcher is a bash script")

LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "lyrebird"

# Stands in for the venv interpreter: prints its own path, then every argument, one per line.
FAKE_PYTHON = '#!/bin/sh\nprintf \'%s\\n\' "$0" "$@"\n'


def make_checkout(root: Path, *, with_venv: bool = True) -> Path:
    """A miniature clone: bin/lyrebird, the bin/lb symlink, and an engine directory."""
    checkout = root / "checkout"
    (checkout / "bin").mkdir(parents=True)
    shutil.copy(LAUNCHER, checkout / "bin" / "lyrebird")
    (checkout / "bin" / "lb").symlink_to("lyrebird")

    engine = checkout / "engine"
    engine.mkdir()
    (engine / "cli.py").write_text("")
    if with_venv:
        venv_bin = engine / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.write_text(FAKE_PYTHON)
        python.chmod(0o755)
    return checkout


def install_on_path(root: Path, target: Path) -> Path:
    """The documented install: a relative symlink in a directory that is on PATH."""
    bindir = root / "home" / ".local" / "bin"
    bindir.mkdir(parents=True)
    link = bindir / "lyrebird"
    link.symlink_to(os.path.relpath(target, bindir))
    return link


def test_launcher_resolves_a_two_hop_symlink_chain_from_outside_the_checkout(tmp_path):
    """Regression: `~/.local/bin/lyrebird -> <clone>/bin/lb -> lyrebird` derived the engine from
    the *invoked* path, looked for `~/.local/engine`, and failed inside `cd` before it could say
    anything useful."""
    checkout = make_checkout(tmp_path)
    link = install_on_path(tmp_path, checkout / "bin" / "lb")

    result = subprocess.run([str(link), "status", "--json"],
                            cwd=tmp_path, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert Path(lines[0]).resolve() == (checkout / "engine" / ".venv" / "bin" / "python").resolve()
    assert Path(lines[1]).resolve() == (checkout / "engine" / "cli.py").resolve()
    assert lines[2:] == ["status", "--json"]


def test_launcher_names_the_resolved_engine_when_the_venv_is_missing(tmp_path):
    """The "no virtualenv" message has to point at the engine the launcher actually belongs to,
    not at a directory beside whatever symlink was invoked."""
    checkout = make_checkout(tmp_path, with_venv=False)
    link = install_on_path(tmp_path, checkout / "bin" / "lb")

    result = subprocess.run([str(link), "status"],
                            cwd=tmp_path, capture_output=True, text=True)

    assert result.returncode == 1
    expected = str((checkout / "engine" / ".venv").resolve())
    assert f"no virtualenv at {expected}" in result.stderr
    assert ".local/engine" not in result.stderr


def test_launcher_fails_clearly_when_no_engine_sits_beside_it(tmp_path):
    """A stray copy of the script — not a link — used to die with a bare `cd: … No such file or
    directory`, which names neither the launcher nor what it was looking for."""
    stray = tmp_path / "stray"
    stray.mkdir()
    shutil.copy(LAUNCHER, stray / "lyrebird")

    result = subprocess.run([str(stray / "lyrebird"), "status"],
                            cwd=tmp_path, capture_output=True, text=True)

    assert result.returncode == 1
    assert "no engine directory" in result.stderr
    assert "cd:" not in result.stderr


def test_launcher_fails_clearly_when_it_cannot_resolve_its_own_path(tmp_path):
    """`readlink -f` is the one thing the launcher cannot do without. If it fails, the launcher
    must say so rather than carry on with an empty path and blame the engine directory."""
    checkout = make_checkout(tmp_path)
    link = install_on_path(tmp_path, checkout / "bin" / "lb")

    sabotage = tmp_path / "sabotage"
    sabotage.mkdir()
    fake = sabotage / "readlink"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)

    env = dict(os.environ, PATH=f"{sabotage}{os.pathsep}{os.environ['PATH']}")
    result = subprocess.run([str(link), "status"],
                            cwd=tmp_path, capture_output=True, text=True, env=env)

    assert result.returncode == 1
    assert "cannot resolve the launcher's own path" in result.stderr
    assert result.stdout == ""
