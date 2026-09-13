"""`scripts/check-app-test-count.sh` must fail when the app test run executed nothing.

`xcodebuild -only-testing:` with an identifier that matches nothing exits 0 and reports "0 tests,
passed", so `make test-app TEST=<typo>` was green having run no test. The count is read back from the
result bundle through `xcrun xcresulttool`, which is faked here: the script's contract is the exit
status and the sentence, not xcresulttool's.
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check-app-test-count.sh"

FAKE_XCRUN = """#!/bin/bash
# The only call the script makes: `xcrun xcresulttool get test-results summary --path X --compact`.
printf '{"totalTestCount": %s, "result": "Passed"}\\n' "$(cat "$LYREBIRD_FAKE_COUNT")"
"""


def _run(tmp_path: Path, count: int, *args: str) -> subprocess.CompletedProcess:
    tools = tmp_path / "tools"
    tools.mkdir()
    fake = tools / "xcrun"
    fake.write_text(FAKE_XCRUN)
    fake.chmod(0o755)
    count_file = tmp_path / "count"
    count_file.write_text(str(count))
    env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "LYREBIRD_FAKE_COUNT": str(count_file)}
    return subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "run.xcresult"), *args], capture_output=True, text=True, env=env
    )


def test_a_run_that_executed_tests_passes(tmp_path):
    result = _run(tmp_path, 237, "LyrebirdTests")
    assert result.returncode == 0, result.stderr
    assert "app tests executed: 237" in result.stdout


def test_a_filter_that_matched_nothing_fails_and_names_the_filter(tmp_path):
    """The useful next step is "check the identifier", not "look at your code", so the sentence
    names the filter rather than a bare count."""
    result = _run(tmp_path, 0, "LyrebirdTests/AppTests/ControlTests")
    assert result.returncode != 0
    assert "matched no test" in result.stderr and "LyrebirdTests/AppTests/ControlTests" in result.stderr


def test_an_unfiltered_run_that_executed_nothing_fails_too(tmp_path):
    result = _run(tmp_path, 0)
    assert result.returncode != 0
    assert "no test ran" in result.stderr


def test_the_script_refuses_to_run_without_a_bundle():
    """The `:?` guard: a missing bundle path is a usage error, never a green exit."""
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode != 0 and "usage" in result.stderr
