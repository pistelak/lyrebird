"""Tool presence is not proof of a usable installation; check its failure paths."""

import shutil
import subprocess
from pathlib import Path


def tool_checkout(tmp_path, *, version_exit=0, settings=True):
    root = tmp_path / "checkout with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "scripts"
    for name in ("check-tools.sh", "tool-versions.env"):
        shutil.copyfile(source / name, scripts / name)
    binaries = root / ".build/tools/bin"
    binaries.mkdir(parents=True)
    for name, version in (("xcodegen", "Version: $XCODEGEN_VERSION"), ("swift-format", "$SWIFT_FORMAT_VERSION")):
        executable = binaries / name
        executable.write_text(
            '#!/usr/bin/env bash\ncd "$(dirname "$0")/../../.."\n'
            f'source scripts/tool-versions.env\necho "{version}"\nexit {version_exit}\n'
        )
        executable.chmod(0o755)
    if settings:
        preset = binaries / "XcodeGen_XcodeGenKit.bundle/Contents/Resources/SettingPresets/base.yml"
        preset.parent.mkdir(parents=True)
        preset.write_text("{}\n")
    return root


def check(root):
    # Invoke from outside the checkout to exercise the script's own path resolution.
    return subprocess.run(
        ["bash", str(root / "scripts/check-tools.sh")], cwd=root.parent, capture_output=True, text=True
    )


def test_tool_check_refuses_a_version_command_that_prints_the_expected_version_but_fails(tmp_path):
    result = check(tool_checkout(tmp_path, version_exit=1))
    assert result.returncode != 0
    assert "run make setup-app" in result.stderr


def test_tool_check_refuses_xcodegen_without_its_settings_bundle(tmp_path):
    result = check(tool_checkout(tmp_path, settings=False))
    assert result.returncode != 0
    assert "missing settings bundle" in result.stderr


def test_tool_check_accepts_complete_tools_from_a_path_with_spaces(tmp_path):
    result = check(tool_checkout(tmp_path))
    assert result.returncode == 0, result.stderr


def test_lock_check_rejects_changed_inputs_and_manual_lock_edits(tmp_path):
    root = tmp_path / "lock checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[2] / "scripts/check-locks.py", scripts / "check-locks.py")
    engine = root / "engine"
    engine.mkdir()
    inputs = engine / "requirements.in"
    inputs.write_text("click==8.4.2\n")
    (engine / "requirements-dev.in").write_text("-r requirements.in\npytest==8.4.2\n")
    (engine / "requirements.txt").write_text("click==8.4.2\n")
    (engine / "requirements-dev.txt").write_text("click==8.4.2\npytest==8.4.2\n")

    import sys

    command = [sys.executable, str(scripts / "check-locks.py")]
    assert subprocess.run(command, capture_output=True).returncode == 1
    subprocess.run([*command, "--stamp"], check=True, capture_output=True)
    assert subprocess.run(command, capture_output=True).returncode == 0
    inputs.write_text("click==8.4.1\n")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "run make lock" in result.stdout
    inputs.write_text("click==8.4.2\n")
    assert subprocess.run(command, capture_output=True).returncode == 0
    lock = engine / "requirements.txt"
    lock.write_text(lock.read_text().replace("8.4.2", "8.4.1"))
    assert subprocess.run(command, capture_output=True).returncode == 1
