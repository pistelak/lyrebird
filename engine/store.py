"""In-memory session/override/preset store, persisted as JSON under the active profile.

Single-loop safety: mitmproxy runs one asyncio loop, and the aiohttp control server runs on that
same loop, so flow-hook reads and control-API writes are serialised — no locking required.

Every name that becomes a path component (session name, preset name, operation id) is validated
and the resolved path is checked for containment before any read, write, listing or unlink. These
names arrive from an unauthenticated local HTTP API, so they are treated as untrusted input.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import config
import rules

# Deliberately strict: a path component, never a path.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class UnsafeName(ValueError):
    pass


def safe_component(name: object, kind: str = "name") -> str:
    value = str(name or "")
    if not _SAFE_NAME.match(value):
        raise UnsafeName(
            f"invalid {kind} {value!r} — use letters, digits, dot, dash or underscore "
            f"(max 64 characters, no path separators)"
        )
    return value


def _contained(parent: Path, *parts: str) -> Path:
    """Resolve `parent/parts...` and refuse anything that escapes it, belt-and-braces on top of
    `safe_component`.

    Containment is checked against the **profile root** as well as the immediate parent: proving
    only that a path sits under `sessions/` is not enough if `sessions/` is itself a symlink
    pointing somewhere else.
    """
    candidate = parent.joinpath(*parts)
    try:
        resolved = candidate.resolve()
        roots = [parent.resolve(), config.PROFILE_DIR.resolve()]
    except OSError as error:
        raise UnsafeName(f"cannot resolve path: {error}") from None
    for root in roots:
        if not resolved.is_relative_to(root):
            raise UnsafeName(f"path escapes {root}")
    return candidate


def _session_path(name: str) -> Path:
    return _contained(config.SESSIONS_DIR, f"{safe_component(name, 'session name')}.json")


def _preset_path(operation_id: str, name: str) -> Path:
    return _contained(
        config.PRESETS_DIR,
        safe_component(operation_id, "operation id"),
        f"{safe_component(name, 'preset name')}.json",
    )


def _clone(source: dict, name: str) -> dict:
    session = copy.deepcopy(_persistable(source))
    session["name"] = name
    session["createdAt"] = _now_iso()
    return session


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _empty_session(name: str) -> dict:
    return {
        "schemaVersion": 1,
        "name": name,
        "notes": "",
        "verified": False,
        "overrides": [],
        "createdAt": _now_iso(),
    }


def _persistable(session: dict) -> dict:
    return {key: value for key, value in session.items() if not key.startswith("_")}


class Store:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.active_name: str = "default"
        self.recent: deque[dict] = deque(maxlen=config.RECENT_CAP)
        self.load_problems: list[str] = []
        self._load()

    # MARK: - Loading / persistence

    def _load(self) -> None:
        config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        for file in sorted(config.SESSIONS_DIR.glob("*.json")):
            try:
                name = safe_component(file.stem, "session name")
            except UnsafeName as error:
                self._problem(f"skipped {file.name}: {error}")
                continue
            try:
                raw = json.loads(file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as error:
                self._problem(f"skipped {file.name}: {error}")
                continue
            try:
                session = rules.normalise_session(raw, name)
            except rules.ValidationError as error:
                self._problem(f"skipped {file.name}: {error}")
                continue
            for problem in session.pop("_problems", []):
                self._problem(f"{file.name}: {problem}")
            self.sessions[name] = session

        if "default" not in self.sessions:
            # In memory only. Writing it would dirty a profile kept in git the first time the
            # proxy started, which is exactly what the profile/state split exists to prevent.
            self.sessions["default"] = _empty_session("default")

        if config.STATE_FILE.exists():
            try:
                state = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(state, dict) and state.get("active") in self.sessions:
                    self.active_name = state["active"]
            except (json.JSONDecodeError, OSError):
                pass
        # Sessions are NOT rewritten on startup: a profile kept in git must stay clean until
        # something actually changes.

    def _problem(self, message: str) -> None:
        # Recorded only; addon.running re-emits these through the logger once it is up, and the
        # control API exposes them. Printing here would double-report into the same log file.
        self.load_problems.append(message)

    def _persist_session(self, name: str) -> None:
        session = self.sessions.get(name)
        if session:
            config.atomic_write(_session_path(name), json.dumps(_persistable(session), indent=2))

    def _persist_state(self) -> None:
        config.atomic_write(config.STATE_FILE, json.dumps({"active": self.active_name}, indent=2))

    # MARK: - Active session / overrides

    def active_session(self) -> dict:
        if self.active_name not in self.sessions:
            self.active_name = "default"
            self.sessions.setdefault("default", _empty_session("default"))
        return self.sessions[self.active_name]

    def active_overrides(self) -> list[dict]:
        return self.active_session().setdefault("overrides", [])

    def find_override(self, method: str, pathname: str, query: dict[str, str], body_text: str) -> dict | None:
        return rules.find_override(self.active_overrides(), method, pathname, query, body_text)

    def add_override(self, payload: dict) -> dict:
        override = {"active": True, **rules.validate_override(payload)}
        override["id"] = override.get("id") or f"ovr_{secrets.token_hex(3)}"  # after the spread
        overrides = self.active_overrides()
        existing = next((i for i, o in enumerate(overrides) if o.get("id") == override["id"]), None)
        if existing is not None:
            overrides[existing] = override
        else:
            overrides.append(override)
        self._persist_session(self.active_name)
        return override

    def remove_override(self, override_id: str) -> bool:
        """False if no such override — reporting a removal that did not happen is a lie."""
        session = self.active_session()
        remaining = [o for o in self.active_overrides() if o.get("id") != override_id]
        if len(remaining) == len(session["overrides"]):
            return False
        session["overrides"] = remaining
        self._persist_session(self.active_name)
        return True

    def clear_overrides(self) -> None:
        self.active_session()["overrides"] = []
        self._persist_session(self.active_name)

    # MARK: - Sessions

    def list_sessions(self) -> dict:
        return {
            "active": self.active_name,
            "sessions": [
                {
                    "name": session["name"],
                    "overrideCount": len(session.get("overrides", [])),
                    "verified": session.get("verified", False),
                    "notes": session.get("notes", ""),
                }
                for session in self.sessions.values()
            ],
        }

    def create_session(self, name: str, clone_from: str | None = None) -> None:
        """Raises KeyError if clone_from names a session that does not exist — silently handing
        back an empty session instead is a false success the caller cannot see."""
        name = safe_component(name, "session name")
        if name in self.sessions:
            raise FileExistsError(name)
        if clone_from:
            if clone_from not in self.sessions:
                raise KeyError(clone_from)
            base = _clone(self.sessions[clone_from], name)
        else:
            base = _empty_session(name)
        self.sessions[name] = base
        self._persist_session(name)

    def set_active(self, name: str) -> dict | None:
        if name not in self.sessions:
            return None
        previous = {"name": self.active_name, "overrideCount": len(self.active_overrides())}
        self.active_name = name
        self._persist_state()
        return previous

    def delete_session(self, name: str) -> bool:
        if name == "default" or name not in self.sessions:
            return False
        path = _session_path(name)
        # Switch away BEFORE removing: set_active reads the outgoing session's override count.
        if self.active_name == name:
            self.active_name = "default"
            self._persist_state()
        del self.sessions[name]
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            self._problem(f"could not delete {path.name}: {error}")
            return False
        return True

    def export_session(self, name: str) -> dict | None:
        session = self.sessions.get(name)
        return _persistable(session) if session else None

    def import_session(self, payload: dict) -> str | None:
        session = payload.get("session", payload)
        if not rules.is_plain_object(session):
            return None
        try:
            name = safe_component(session.get("name"), "session name")
        except UnsafeName:
            return None
        normalised = rules.normalise_session({**_empty_session(name), **session}, name)
        normalised.pop("_problems", None)
        self.sessions[name] = normalised
        self._persist_session(name)
        return name

    def save_active_as(self, name: str, notes: str = "", verified: bool = False) -> None:
        name = safe_component(name, "session name")
        snapshot = _clone(self.active_session(), name)
        snapshot["notes"] = notes
        snapshot["verified"] = verified
        self.sessions[name] = snapshot
        self._persist_session(name)

    # MARK: - Presets

    def list_presets(self, operation_id: str) -> list[str]:
        directory = _contained(config.PRESETS_DIR, safe_component(operation_id, "operation id"))
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))

    def get_preset(self, operation_id: str, name: str) -> Any | None:
        path = _preset_path(operation_id, name)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def save_preset(self, operation_id: str, name: str, body: Any) -> None:
        path = _preset_path(operation_id, name)
        config.atomic_write(path, json.dumps(body, indent=2))

    # MARK: - Recent

    def record_recent(self, entry: dict) -> None:
        entry.setdefault("time", _now_iso())
        self.recent.appendleft(entry)

    def recent_list(self) -> list[dict]:
        return list(self.recent)
