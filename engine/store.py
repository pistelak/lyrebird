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


# MARK: - Sequence runtime
#
# Cursors hang off the session under a leading underscore, which `_persistable` already strips and
# `_clone` already copies through. So a cursor is never written to a profile, never survives a clone
# or an import, and is scoped to its session without a second key — all from machinery that was
# already here for `_problems`.

def _runtime(session: dict) -> dict:
    return session.setdefault("_sequenceRuntime", {})


def _new_run_id(previous: str | None = None) -> str:
    """A fresh run token, guaranteed different from the one it replaces.

    Eight bytes rather than the three used for override ids, because this token is what stops a
    retained event from an earlier run satisfying a `sequence wait`. The explicit inequality makes
    "a reset always changes the run" a guarantee instead of a probability.
    """
    while True:
        candidate = secrets.token_hex(8)
        if candidate != previous:
            return candidate


def _rule_runtime(session: dict, override_id: str) -> dict:
    """This rule's cursor and run token, created on first use.

    `runId` is opaque and reissued whenever the cursor is reset, so a caller can tell "this event
    belongs to the run I asked about" from "this is left over from a previous one". Deliberately not
    an ordered counter: those collide across a proxy restart, which looks like continuity.

    `overrunSeen` is sticky. It cannot be derived from the cursor, because a rule with an explicit
    `advanceOn` does not move when it answers — so it can serve an exhausted response any number of
    times while the cursor sits still, and live state would keep reporting no overrun while
    `/recent` recorded one.

    `serves` counts how many times each step (1-based) has been served this run, for the same
    reason `overrunSeen` exists: for an `advanceOn` rule the cursor cannot record that a step was
    served, and `/recent` is a bounded window, so under enough traffic the only durable evidence
    that a serve happened is a counter kept where the serve happens.
    """
    runtime = _runtime(session)
    entry = runtime.get(override_id)
    if entry is None:
        entry = {"cursor": 0, "runId": _new_run_id(), "overrunSeen": False, "serves": {}}
        runtime[override_id] = entry
    return entry


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
                    # persist=False: reading the pointer must not rewrite it.
                    self._activate(state["active"], persist=False)
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

    def _activate(self, name: str, *, persist: bool = True) -> None:
        """The one place `active_name` changes.

        Switching sessions clears the destination's cursors, so a scenario always begins at its
        first step. This is a helper rather than a line repeated at each call site because there are
        four of them — startup, the self-heal in `active_session`, `set_active` and
        `delete_session` — and the last two are easy to miss: deleting the active session falls back
        to `default` without going anywhere near `set_active`.
        """
        self.active_name = name
        session = self.sessions.get(name)
        if session is not None:
            _runtime(session).clear()
        if persist:
            self._persist_state()

    # MARK: - Active session / overrides

    def active_session(self) -> dict:
        if self.active_name not in self.sessions:
            self.sessions.setdefault("default", _empty_session("default"))
            self._activate("default", persist=False)
        return self.sessions[self.active_name]

    def active_overrides(self) -> list[dict]:
        return self.active_session().setdefault("overrides", [])

    def find_override(self, method: str, pathname: str, query: dict[str, str], body_text: str) -> dict | None:
        return rules.find_override(self.active_overrides(), method, pathname, query, body_text)

    def add_override(self, payload: dict) -> dict:
        override = {"active": True, **rules.validate_override(payload)}
        override["id"] = override.get("id") or f"ovr_{secrets.token_hex(3)}"  # after the spread
        session = self.active_session()
        overrides = self.active_overrides()
        existing = next((i for i, o in enumerate(overrides) if o.get("id") == override["id"]), None)
        if existing is not None:
            overrides[existing] = override
        else:
            overrides.append(override)
        # The rule at this id is now a different rule; its old cursor describes steps that may no
        # longer exist. Note this only fires for a caller that supplied an explicit id — an id-less
        # `override add` mints a fresh random one and so replaces nothing.
        _runtime(session).pop(override["id"], None)
        self._persist_session(self.active_name)
        return override

    def remove_override(self, override_id: str) -> bool:
        """False if no such override — reporting a removal that did not happen is a lie."""
        session = self.active_session()
        remaining = [o for o in self.active_overrides() if o.get("id") != override_id]
        if len(remaining) == len(session["overrides"]):
            return False
        session["overrides"] = remaining
        _runtime(session).pop(override_id, None)
        self._persist_session(self.active_name)
        return True

    def clear_overrides(self) -> None:
        session = self.active_session()
        session["overrides"] = []
        _runtime(session).clear()
        self._persist_session(self.active_name)

    # MARK: - Sequences

    def sequenced_overrides(self) -> list[dict]:
        """Active sequenced rules in the active session.

        Excludes `active: false`, matching what `find_override` already does — a disabled rule that
        still advanced on the wire would be a rule doing something while switched off.
        """
        return [
            override
            for override in self.active_overrides()
            if override.get("active", True) is not False
            and rules.sequence_steps(override) is not None
        ]

    def resolve_override(self, override: dict) -> tuple[str, dict | None, dict | None]:
        """What a matched rule should do for this request. Reads the cursor; never moves it.

        Returns (action, effective override or None, progress or None). Progress is None for an
        ordinary rule, so a caller can tell "no sequence here" from "a sequence that selected
        nothing".
        """
        steps = rules.sequence_steps(override)
        if steps is None:
            return rules.APPLY, dict(override), None

        entry = _rule_runtime(self.active_session(), override["id"])
        cursor, count = entry["cursor"], len(steps)
        action, view = rules.resolve_step(override, cursor)
        if cursor >= count:
            entry["overrunSeen"] = True   # sticky: the cursor alone cannot record this
        else:
            # Counted here and not in `bump_selected`, because this is the only path a winning
            # rule takes regardless of trigger — an `advanceOn` rule serves without ever bumping.
            entry["serves"][cursor + 1] = entry["serves"].get(cursor + 1, 0) + 1
        progress = {
            "sequenceId": override["id"],
            "runId": entry["runId"],
            "selectedStep": (cursor + 1) if cursor < count else None,
            "stepCount": count,
            "advanceEvents": cursor,   # for the exhaustion message; not recorded in /recent
            # This request went past the planned steps — an event, distinct from the live
            # `hasOverrun` below, which says one has happened at some point.
            "overrun": cursor >= count,
        }
        return action, view, progress

    def bump_selected(self, override: dict) -> None:
        """Advance a rule whose trigger is `self`: it answered, so it moves.

        A rule with an explicit `advanceOn` is untouched here — it moves only when its own matcher
        sees a request, which is what makes repeated fetches idempotent.
        """
        steps = rules.sequence_steps(override)
        if steps is None or rules.advance_matcher(override) is not None:
            return
        entry = _rule_runtime(self.active_session(), override["id"])
        entry["cursor"] = rules.bumped(entry["cursor"], len(steps))

    def advance_matching(
        self, method: str, pathname: str, query: dict[str, str], body_text: str
    ) -> list[str]:
        """Advance every rule whose explicit `advanceOn` fits this request; return the ids moved."""
        session = self.active_session()
        advanced = []
        for override in self.sequenced_overrides():
            matcher = rules.advance_matcher(override)
            if matcher is None:
                continue
            if not rules.matches_matcher(matcher, method, pathname, query, body_text):
                continue
            steps = rules.sequence_steps(override) or []
            entry = _rule_runtime(session, override["id"])
            entry["cursor"] = rules.bumped(entry["cursor"], len(steps))
            advanced.append(override["id"])
        return advanced

    def sequence_states(self) -> list[dict]:
        """Live state: what the next request would do, not what previous ones did."""
        session = self.active_session()
        states = []
        for override in self.sequenced_overrides():
            count = len(rules.sequence_steps(override) or [])
            entry = _rule_runtime(session, override["id"])
            cursor = entry["cursor"]
            states.append({
                "id": override["id"],
                "runId": entry["runId"],
                "advanceOn": "self" if rules.advance_matcher(override) is None else "match",
                "nextStep": (cursor + 1) if cursor < count else None,
                "stepCount": count,
                "exhausted": cursor >= count,             # no planned step remains
                "hasOverrun": entry["overrunSeen"],       # a request was actually served past it
                # String keys, matching what any JSON round-trip would force anyway — a consumer
                # must not need to know whether the payload came straight from the store.
                "serves": {str(step): n for step, n in entry["serves"].items()},
            })
        return states

    def reset_sequences(self, override_id: str | None = None) -> dict | None:
        """Rewind sequences to their first step. None if `override_id` names no sequenced rule.

        None rather than an empty result: "I reset nothing" and "there is no such rule" are
        different answers, and a caller that cannot tell them apart will believe a typo worked.
        """
        session = self.active_session()
        sequenced = {override["id"]: override for override in self.sequenced_overrides()}
        if override_id is not None:
            if override_id not in sequenced:
                return None
            targets = [override_id]
        else:
            targets = sorted(sequenced)

        runtime = _runtime(session)
        reset = {}
        for target in targets:
            previous = (runtime.pop(target, None) or {}).get("runId")   # dropping it is the rewind
            entry = _rule_runtime(session, target)                      # recreate to report a token
            entry["runId"] = _new_run_id(previous)
            reset[target] = entry["runId"]
        return {"session": self.active_name, "reset": reset}

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
        self._activate(name)
        return previous

    def delete_session(self, name: str) -> bool:
        if name == "default" or name not in self.sessions:
            return False
        path = _session_path(name)
        # Switch away BEFORE removing: set_active reads the outgoing session's override count.
        if self.active_name == name:
            self._activate("default")
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
        """Raises ValidationError if anything in the payload could not be kept, and
        FileExistsError if the name is taken — the same refusal `create_session` makes, because
        silently replacing a session someone else may be using is a delete without a `delete`.

        A file on disk is reported-and-dropped, because the file is in front of you and the proxy
        must still start. An import is an API call at the boundary, and answering 200 to a payload
        whose second override was silently discarded tells the caller their rule is installed when
        it is not.
        """
        session = payload.get("session", payload)
        if not rules.is_plain_object(session):
            return None
        try:
            name = safe_component(session.get("name"), "session name")
        except UnsafeName:
            return None
        if name in self.sessions:
            raise FileExistsError(name)
        normalised = rules.normalise_session({**_empty_session(name), **session}, name)
        problems = normalised.pop("_problems", [])
        if problems:
            raise rules.ValidationError("; ".join(problems))
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
