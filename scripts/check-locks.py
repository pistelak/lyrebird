"""Offline freshness check for the generated dependency locks; `make lock` writes the stamps."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "engine"
PREFIX = "# Lock inputs and contents SHA-256: "


def check_locks(stamp=False):
    ok = True
    for name, inputs in (
        ("requirements.txt", ["requirements.in"]),
        ("requirements-dev.txt", ["requirements.in", "requirements-dev.in", "requirements.txt"]),
    ):
        path = ROOT / name
        text = path.read_text()
        first, _, rest = text.partition("\n")
        body = rest if first.startswith(PREFIX) else text
        payload = {source: (ROOT / source).read_text() for source in inputs}
        payload[name] = body
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        header = PREFIX + digest
        if stamp:
            path.write_text(header + "\n" + body)
        elif first != header:
            print(f"{name}: inputs or generated contents changed; run make lock")
            ok = False
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp", action="store_true", help="record a successful make lock result")
    args = parser.parse_args()
    try:
        success = check_locks(stamp=args.stamp)
    except OSError as error:
        parser.exit(1, f"Dependency lock unavailable: {error}; run make lock\n")
    raise SystemExit(0 if success else 1)
