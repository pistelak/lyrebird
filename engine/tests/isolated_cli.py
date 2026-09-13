"""Run the real CLI in a real subprocess against a session root the test chose.

    python tests/isolated_cli.py <session-root> <argv...>

The session journal lives at one fixed per-user path, and the production argv and environment name
no root at all — deliberately, because there is one session per user. A test that needs a *real*
process (`status` against a dead control port) therefore cannot isolate itself with `monkeypatch`,
which a fresh interpreter does not inherit: it needs an entry
point that rebinds the root before the CLI is imported. Without this, those tests would read and
write the contributor's own `~/Library/Application Support/Lyrebird/session`.

`sys.path` is extended first because the engine is a flat layout and a script under `tests/`
inherits neither pytest's `pythonpath` setting nor a `PYTHONPATH`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session  # noqa: E402

session.default_root = lambda root=Path(sys.argv[1]): root

import cli  # noqa: E402

cli.cli(sys.argv[2:])
