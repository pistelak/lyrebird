"""The acceptance harness's own PAC read-back, checked hermetically.

The harness verifies a restore by reading macOS itself, not through `netproxy`, so its parser is a
second implementation of the same reading — and one that invented `("", False)` from an answer with
no `URL:` line passed a restore over an empty baseline that nobody observed. `conftest.py` under
`acceptance/` marks everything it collects as `acceptance`, so this check lives beside the ordinary
tests and loads the harness module by path.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import ownership

_ACCEPTANCE = Path(__file__).resolve().parent / "acceptance"


def _harness_module():
    """The acceptance conftest, registered under its own name — never as `conftest`, which pytest
    already holds for the root `tests/conftest.py`."""
    name = "acceptance_harness"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ACCEPTANCE / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _answering(module, monkeypatch, stdout: str):
    def try_run(args, timeout=600):
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(module, "_try_run", try_run)
    monkeypatch.setattr(module, "_service_table", lambda: [("Wi-Fi", "en0")])
    scratch = Path("/tmp/lyrebird-tests")
    harness = module.Harness(
        "SIM-1", scratch / "docs", scratch / "profile", scratch / "state", {"LYREBIRD_CONTROL_PORT": "8088"}
    )
    harness.service = ownership.ServiceRef("Wi-Fi", "en0")
    return harness


@pytest.mark.parametrize("stdout", ["", "URL: http://proxy.example.com/corp.pac\n", "Enabled: No\n", "garbage\n"])
def test_a_networksetup_answer_without_both_lines_is_not_a_pac(monkeypatch, stdout):
    """`None` is "could not read", which the cleanup reports as a failure to verify. `("", False)`
    was "no PAC", which matched an empty baseline and called the restore verified."""
    module = _harness_module()
    harness = _answering(module, monkeypatch, stdout)
    assert harness._read_pac() is None


def test_a_whole_networksetup_answer_is_read_verbatim(monkeypatch):
    module = _harness_module()
    harness = _answering(module, monkeypatch, "URL: http://proxy.example.com/corp.pac\nEnabled: Yes\n")
    assert harness._read_pac() == ownership.Pac("http://proxy.example.com/corp.pac", True)
    harness = _answering(module, monkeypatch, "URL: (null)\nEnabled: No\n")
    assert harness._read_pac() == ownership.Pac("", False)
