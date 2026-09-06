"""In-memory session/override store, persisted as JSON under the active profile.

Single-loop safety: mitmproxy runs one asyncio loop, and the aiohttp control server runs on that
same loop, so flow-hook reads and control-API writes are serialised — no locking required.

Every name that becomes a path component (a session name) is validated
and the resolved path is checked for containment before any read, write, listing or unlink. These
names arrive from an unauthenticated local HTTP API, so they are treated as untrusted input.

Write then publish: a mutator writes the file that records a change *before* the change becomes
visible in memory, so a write that fails (full disk, read-only profile) leaves live state, runtime
slots and the file exactly as they were and the OSError reaches the caller. Otherwise the proxy
would answer with a rule no profile contains — a divergence nothing later reports. This covers
session content and the active-session pointer; `delete_session` is the deliberate exception, as
it drops the live session first and reports a failed unlink through `_problem` and False.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

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
    except (OSError, RuntimeError) as error:
        # RuntimeError as well as OSError: `Path.resolve()` raises `RuntimeError("Symlink loop
        # from …")` for a link that points at itself, and it is not an OSError. Startup reads every
        # session file through here, so an uncaught one is a self-referencing file in `sessions/`
        # stopping the proxy from starting at all — and an offline command dying on a traceback
        # instead of the JSON diagnostics it promises. "I could not resolve it" is the honest
        # verdict for both, and it is a refusal, not a pass.
        raise UnsafeName(f"cannot resolve path: {error}") from None
    for root in roots:
        if not resolved.is_relative_to(root):
            raise UnsafeName(f"path escapes {root}")
    return candidate


def session_path(name: str) -> Path:
    """Where the session called `name` lives. Resolves only — it creates nothing."""
    return _contained(config.SESSIONS_DIR, f"{safe_component(name, 'session name')}.json")


def session_files() -> list[Path]:
    """Every session file in the profile, in the order startup reads them.

    Shared with the offline commands so "which files are a profile's sessions" has one answer:
    a file the loader would read but an inspection command would not is a file whose problems
    only ever surface as a proxy that behaves oddly.
    """
    return sorted(config.SESSIONS_DIR.glob("*.json"))


def _intended_session_name(file: Path) -> str | None:
    """The session `file` would have become, or None if its name could never have been one.

    Kept beside `load_session_file` rather than returned by it: the offline commands report on
    files and have no use for this, and widening that function's contract to carry a name it does
    not use in its own verdict would put the two callers' needs in one return value.
    """
    try:
        return safe_component(file.stem, "session name")
    except UnsafeName:
        return None


def load_session_file(file: Path) -> tuple[dict | None, list[str]]:
    """Read one session file the way startup does: `(session, problems)`.

    `session` is None when nothing could be kept — an unsafe name, an unreadable or malformed file,
    a payload that is not an object, a `schemaVersion` this engine does not read — and `problems`
    then holds the one `skipped <file>: …` line saying so. Otherwise it is the normalised session
    and `problems` lists the rules that were reported-and-dropped, each prefixed with the file name.
    The two are distinguishable on purpose: "this scenario is not loaded" and "this scenario is
    loaded without the rule you are looking for" send an operator to different places.

    A module-level function rather than a `Store` method, so a command can ask "what would the proxy
    make of this file?" without constructing a store — which creates directories, synthesises a
    `default` session and reads the active-session pointer, none of which an inspection may do.
    `Store._load` is a loop around this function, so an offline answer cannot drift from startup's.
    """
    try:
        name = safe_component(file.stem, "session name")
        # Containment on the file about to be *read*, not only on the ones written and unlinked.
        # Startup reaches its files through a glob, so nothing used to check them: a session file
        # symlinked out of `sessions/` loaded into the proxy while `session_path` refused that very
        # session by name — one file, two verdicts, and the permissive one was the one that ran. It
        # is the same rule either way now, so a session the proxy serves is one it can also save.
        _contained(file.parent, file.name)
    except UnsafeName as error:
        return None, [f"skipped {file.name}: {error}"]
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        # UnicodeDecodeError is a ValueError and not a JSONDecodeError — it is raised by the decode,
        # before json sees anything — so without it a file of binary junk escaped all three arms and
        # took the proxy down at startup, from the one directory operators are told to hand-edit.
        return None, [f"skipped {file.name}: {error}"]
    try:
        session = rules.normalise_session(raw, name)
    except rules.ValidationError as error:
        return None, [f"skipped {file.name}: {error}"]
    return session, [f"{file.name}: {problem}" for problem in session.pop("_problems", [])]


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


# MARK: - Rule runtime
#
# Per-rule state that must never reach a profile: sequence cursors, and the count of requests each
# rule has answered. It hangs off the session under a leading underscore, which `_persistable`
# already strips — so it is never written to a profile, never survives a clone or an import, and is
# scoped to its session without a second key, all from machinery that was already here for
# `_problems`.

def _runtime(session: dict) -> dict:
    return session.setdefault("_ruleRuntime", {})


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

    `answers` is that same argument for rules of every kind, sequenced or not: how many requests this
    rule has answered since the run began. It is what lets a test assert that its mock was actually
    in play, rather than inferring it from a screen the un-mocked backend would have produced too.
    """
    runtime = _runtime(session)
    entry = runtime.get(override_id)
    if entry is None:
        entry = {"cursor": 0, "runId": _new_run_id(), "overrunSeen": False, "serves": {}, "answers": 0}
        runtime[override_id] = entry
    return entry


def credit(slot: dict) -> None:
    """Record that the rule owning `slot` answered a request.

    A free function taking the slot, not a `Store` method taking an id, because the two answer paths
    learn the truth at different moments. A `replace` answers inside the request hook and can credit
    at once; a `patch` is only an answer once it has merged into a real upstream response, one hook
    and a network round trip later — and by then the session may have been switched or the rule
    replaced.

    Holding the slot rather than the id is what makes that safe. `_activate` clears the runtime
    container and `add_override`/`reset_runtime` pop from it, so a slot captured before any of those
    is orphaned: crediting it mutates a dict nothing can reach. A patch that lands after a session
    switch therefore credits nobody, instead of crediting whatever rule in the new session happens to
    share its id.
    """
    slot["answers"] += 1   # `_rule_runtime` is the only maker of a slot, and it always seeds this


class Store:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.active_name: str = "default"
        self.recent: deque[dict] = deque(maxlen=config.RECENT_CAP)
        self.load_problems: list[str] = []
        # The same problems, keyed by the session each belongs to. A diagnostic string cannot be
        # matched back to its session: a file may be named `orders-outage.json: backup.json`, and
        # the `skipped <file>: <error>` line it produces then reads exactly like one about
        # `orders-outage`. A caller deciding whether to run against a session — `up --use` does —
        # needs the answer, not a resemblance to it.
        #
        # A file whose *name* was rejected is deliberately absent: it could never have become a
        # session, so there is no session for it to be reported against.
        self.sessions_not_whole: dict[str, list[str]] = {}
        self._load()

    # MARK: - Loading / persistence

    def _load(self) -> None:
        config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        for file in session_files():
            session, problems = load_session_file(file)
            # Which session these problems belong to. `load_session_file` reports the *file*,
            # because that is what an offline command is looking at; a running proxy is asked
            # about sessions instead — `up --use NAME` has to know whether NAME loaded whole.
            #
            # A file that yielded nothing still names one, when its name would have been a legal
            # session: the scenario the operator asked for is the one that is missing, and
            # answering "no problems with NAME" because NAME never loaded is the failure this
            # whole map exists to prevent. Only a file that could not have named a session at all
            # is recorded against none.
            owner = session["name"] if session else _intended_session_name(file)
            for problem in problems:
                self._problem(problem, owner)
            if session is None:
                continue
            self.sessions[session["name"]] = session

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

    def _problem(self, message: str, session: str | None = None) -> None:
        """Record a problem, and — when it belongs to one — the session it stopped loading whole.

        Recorded only; addon.running re-emits these through the logger once it is up, and the
        control API exposes them. Printing here would double-report into the same log file.
        """
        self.load_problems.append(message)
        if session is not None:
            self.sessions_not_whole.setdefault(session, []).append(message)

    def _forget_load_problems(self, name: str) -> None:
        """Drop what was recorded against `name`: the session it described is no longer there.

        `sessions_not_whole` is a claim about the session under a name *now*, not a history, and
        replacing a session is how an operator recovers from a file that would not load — a
        malformed `orders-outage.json`, then an import of a good one. Keeping the entry would make
        that recovery invisible: every rule installed, and `up --use orders-outage` still refusing
        to launch the app over a file that no longer decides anything.

        `load_problems` is untouched, because it is the other thing: a record of what startup found,
        which stays true however the store is edited afterwards.
        """
        self.sessions_not_whole.pop(name, None)

    def _write_session(self, name: str, session: dict) -> None:
        """Write the session someone *proposes* to store under `name`, not the one already there.

        Taking the dict rather than looking it up is what lets a mutator write its candidate before
        publishing it, so a failed write leaves nothing half-applied.
        """
        config.atomic_write(session_path(name), json.dumps(_persistable(session), indent=2))

    def _persist_state(self, name: str) -> None:
        """Record `name` as active. Takes the name for the same reason `_write_session` takes the
        session: the pointer is written before `active_name` moves."""
        config.atomic_write(config.STATE_FILE, json.dumps({"active": name}, indent=2))

    def _activate(self, name: str, *, persist: bool = True) -> None:
        """The one place `active_name` changes.

        Switching sessions clears the destination's cursors, so a scenario always begins at its
        first step. This is a helper rather than a line repeated at each call site because there are
        four of them — startup, the self-heal in `active_session`, `set_active` and
        `delete_session` — and the last two are easy to miss: deleting the active session falls back
        to `default` without going anywhere near `set_active`.

        The pointer is written first: a switch that cannot be recorded must not happen at all, or
        the destination's cursors are rewound for a scenario that reverts on the next restart.
        """
        if persist:
            self._persist_state(name)
        self.active_name = name
        session = self.sessions.get(name)
        if session is not None:
            _runtime(session).clear()

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
            candidate = [*overrides[:existing], override, *overrides[existing + 1:]]
        else:
            candidate = [*overrides, override]
        self._write_session(self.active_name, {**session, "overrides": candidate})
        session["overrides"] = candidate
        # The rule at this id is now a different rule; its old cursor describes steps that may no
        # longer exist. Note this only fires for a caller that supplied an explicit id — an id-less
        # `override add` mints a fresh random one and so replaces nothing. It happens after the
        # write for the same reason the list does: if the write failed the old rule is still live,
        # and its cursor and answer count still describe it.
        _runtime(session).pop(override["id"], None)
        return override

    def remove_override(self, override_id: str) -> bool:
        """False if no such override — reporting a removal that did not happen is a lie."""
        session = self.active_session()
        remaining = [o for o in self.active_overrides() if o.get("id") != override_id]
        if len(remaining) == len(session["overrides"]):
            return False
        self._write_session(self.active_name, {**session, "overrides": remaining})
        session["overrides"] = remaining
        _runtime(session).pop(override_id, None)
        return True

    def clear_overrides(self) -> None:
        session = self.active_session()
        self._write_session(self.active_name, {**session, "overrides": []})
        session["overrides"] = []
        _runtime(session).clear()

    # MARK: - Sequences

    def sequenced_overrides(self) -> list[dict]:
        """Active sequenced rules in the active session.

        Excludes `active: false`, matching what `find_override` already does — a disabled rule that
        still advanced on the wire would be a rule doing something while switched off.
        """
        return [
            override
            for override in self.active_overrides()
            if rules.is_active(override)
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

    def answer_slot(self, override_id: str) -> dict:
        """The runtime entry to credit when this rule answers. See `credit`."""
        return _rule_runtime(self.active_session(), override_id)

    def answer_states(self) -> list[dict]:
        """How many requests each rule in the active session has answered this run."""
        session = self.active_session()
        runtime = _runtime(session)
        return [
            {
                "id": override["id"],
                "active": rules.is_active(override),
                # Read, never create: asking how many answers a rule has must not mint runtime state
                # for a rule that has never been near a request.
                "count": (runtime.get(override["id"]) or {}).get("answers", 0),
            }
            for override in self.active_overrides()
        ]

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

    def reset_runtime(self, override_id: str | None = None) -> dict | None:
        """Start a fresh run: rewind sequence cursors and clear answer counts. None if `override_id`
        names no rule in the active session.

        None rather than an empty result: "I reset nothing" and "there is no such rule" are
        different answers, and a caller that cannot tell them apart will believe a typo worked.

        Scoped to every rule, not just sequenced ones, because every rule now carries runtime state.
        Refusing to reset a plain rule would leave the one boundary a test can draw — reset, trigger,
        assert — unavailable to exactly the rules that need it most.
        """
        session = self.active_session()
        known = {override["id"] for override in self.active_overrides()}
        if override_id is not None:
            if override_id not in known:
                return None
            targets = [override_id]
        else:
            targets = sorted(known)

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
        self._write_session(name, base)
        self.sessions[name] = base
        self._forget_load_problems(name)

    def set_active(self, name: str) -> dict | None:
        if name not in self.sessions:
            return None
        previous = {"name": self.active_name, "overrideCount": len(self.active_overrides())}
        self._activate(name)
        return previous

    def delete_session(self, name: str) -> bool:
        if name == "default" or name not in self.sessions:
            return False
        path = session_path(name)
        # Switch away BEFORE removing: set_active reads the outgoing session's override count.
        if self.active_name == name:
            self._activate("default")
        del self.sessions[name]
        # Before the unlink, which may fail: the session is already out of memory either way, and
        # an entry left behind would be inherited by the next session created under this name.
        self._forget_load_problems(name)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError) as error:
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
        self._write_session(name, normalised)
        self.sessions[name] = normalised
        self._forget_load_problems(name)
        return name

    # MARK: - Recent

    def record_recent(self, entry: dict) -> None:
        entry.setdefault("time", _now_iso())
        self.recent.appendleft(entry)

    def recent_list(self) -> list[dict]:
        return list(self.recent)
