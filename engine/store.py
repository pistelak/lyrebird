"""In-memory scenario/override store, persisted as JSON under the active profile.

Single-loop safety: mitmproxy runs one asyncio loop, and the aiohttp control server runs on that
same loop, so flow-hook reads and control-API writes are serialised — no locking required.

Every name that becomes a path component (a scenario name) is validated
and the resolved path is checked for containment before any read, write, listing or unlink. These
names arrive from an unauthenticated local HTTP API, so they are treated as untrusted input.

Write then publish: a mutator writes the file that records a change *before* the change becomes
visible in memory, so a write that fails (full disk, read-only profile) leaves live state, runtime
slots and the file exactly as they were and the OSError reaches the caller. Otherwise the proxy
would answer with a rule no profile contains — a divergence nothing later reports. This covers
scenario content and the active-scenario pointer; `delete_scenario` is the deliberate exception, as
it drops the live scenario first and reports a failed unlink through `_problem` and False.
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


class LegacyProfileLayout(RuntimeError):
    """A profile still keeping its scenarios in `sessions/`, from before the directory was renamed."""


def refuse_legacy_layout(profile_dir: Path) -> None:
    """Refuse a profile whose scenarios are still under `sessions/`, naming the move that fixes it.

    Raised rather than fixed up or ignored: `Store._load` creates `scenarios/` when it is missing, so
    without this the proxy would start on an empty profile — every saved scenario silently absent and
    only `default` in the list — which is the "created empty instead of saying so" failure the house
    rules name. See test_store_refuses_a_legacy_sessions_layout.

    Takes the profile explicitly rather than reading `config.PROFILE_DIR`, so `init PATH` can check
    the directory it is about to write into.
    """
    legacy, current = profile_dir / "sessions", profile_dir / "scenarios"
    if legacy.is_dir() and not current.exists():
        raise LegacyProfileLayout(
            f"{profile_dir} keeps its scenarios in {legacy} — Lyrebird reads {current} now.\n"
            f"  rename it:  mv {legacy} {current}"
        )


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
    only that a path sits under `scenarios/` is not enough if `scenarios/` is itself a symlink
    pointing somewhere else.
    """
    candidate = parent.joinpath(*parts)
    try:
        resolved = candidate.resolve()
        roots = [parent.resolve(), config.PROFILE_DIR.resolve()]
    except (OSError, RuntimeError) as error:
        # RuntimeError as well as OSError: `Path.resolve()` raises `RuntimeError("Symlink loop
        # from …")` for a link that points at itself, and it is not an OSError. Startup reads every
        # scenario file through here, so an uncaught one is a self-referencing file in `scenarios/`
        # stopping the proxy from starting at all — and an offline command dying on a traceback
        # instead of the JSON diagnostics it promises. "I could not resolve it" is the honest
        # verdict for both, and it is a refusal, not a pass.
        raise UnsafeName(f"cannot resolve path: {error}") from None
    for root in roots:
        if not resolved.is_relative_to(root):
            raise UnsafeName(f"path escapes {root}")
    return candidate


def scenario_path(name: str) -> Path:
    """Where the scenario called `name` lives. Resolves only — it creates nothing."""
    return _contained(config.SCENARIOS_DIR, f"{safe_component(name, 'scenario name')}.json")


def scenario_files() -> list[Path]:
    """Every scenario file in the profile, in the order startup reads them.

    Shared with the offline commands so "which files are a profile's scenarios" has one answer:
    a file the loader would read but an inspection command would not is a file whose problems
    only ever surface as a proxy that behaves oddly.

    Raises `LegacyProfileLayout` rather than globbing a directory that is not there: an old profile
    would otherwise report "no scenario files" for a profile that has them all, under the old name —
    see test_validate_refuses_a_legacy_sessions_layout.
    """
    refuse_legacy_layout(config.PROFILE_DIR)
    return sorted(config.SCENARIOS_DIR.glob("*.json"))


def _intended_scenario_name(file: Path) -> str | None:
    """The scenario `file` would have become, or None if its name could never have been one.

    Kept beside `load_scenario_file` rather than returned by it: the offline commands report on
    files and have no use for this, and widening that function's contract to carry a name it does
    not use in its own verdict would put the two callers' needs in one return value.
    """
    try:
        return safe_component(file.stem, "scenario name")
    except UnsafeName:
        return None


def load_scenario_file(file: Path) -> tuple[dict | None, list[str]]:
    """Read one scenario file the way startup does: `(scenario, problems)`.

    `scenario` is None when nothing could be kept — an unsafe name, an unreadable or malformed file,
    a payload that is not an object, a `schemaVersion` this engine does not read — and `problems`
    then holds the one `skipped <file>: …` line saying so. Otherwise it is the normalised scenario
    and `problems` lists the rules that were reported-and-dropped, each prefixed with the file name.
    The two are distinguishable on purpose: "this scenario is not loaded" and "this scenario is
    loaded without the rule you are looking for" send an operator to different places.

    A module-level function rather than a `Store` method, so a command can ask "what would the proxy
    make of this file?" without constructing a store — which creates directories, synthesises a
    `default` scenario and reads the active-scenario pointer, none of which an inspection may do.
    `Store._load` is a loop around this function, so an offline answer cannot drift from startup's.
    """
    try:
        name = safe_component(file.stem, "scenario name")
        # Containment on the file about to be *read*, not only on the ones written and unlinked.
        # Startup reaches its files through a glob, so nothing used to check them: a scenario file
        # symlinked out of `scenarios/` loaded into the proxy while `scenario_path` refused that very
        # scenario by name — one file, two verdicts, and the permissive one was the one that ran. It
        # is the same rule either way now, so a scenario the proxy serves is one it can also save.
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
        scenario = rules.normalise_scenario(raw, name)
    except rules.ValidationError as error:
        return None, [f"skipped {file.name}: {error}"]
    return scenario, [f"{file.name}: {problem}" for problem in scenario.pop("_problems", [])]


def _clone(source: dict, name: str) -> dict:
    scenario = copy.deepcopy(_persistable(source))
    scenario["name"] = name
    scenario["createdAt"] = _now_iso()
    return scenario


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _empty_scenario(name: str) -> dict:
    return {
        "schemaVersion": 1,
        "name": name,
        "notes": "",
        "verified": False,
        "overrides": [],
        "createdAt": _now_iso(),
    }


def _persistable(scenario: dict) -> dict:
    return {key: value for key, value in scenario.items() if not key.startswith("_")}


# MARK: - Rule runtime
#
# Per-rule state that must never reach a profile: sequence cursors, and the count of requests each
# rule has answered. It hangs off the scenario under a leading underscore, which `_persistable`
# already strips — so it is never written to a profile, never survives a clone, and is
# scoped to its scenario without a second key, all from machinery that was already here for
# `_problems`.


def _runtime(scenario: dict) -> dict:
    return scenario.setdefault("_ruleRuntime", {})


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


def _new_slot(previous: str | None = None) -> dict:
    """A rule's runtime state at the start of a run, under a token that is not `previous`.

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
    return {"cursor": 0, "runId": _new_run_id(previous), "overrunSeen": False, "serves": {}, "answers": 0}


def _rule_runtime(scenario: dict, override_id: str) -> dict:
    """This rule's slot, created on first use."""
    runtime = _runtime(scenario)
    entry = runtime.get(override_id)
    if entry is None:
        entry = _new_slot()
        runtime[override_id] = entry
    return entry


def credit(slot: dict) -> None:
    """Record that the rule owning `slot` answered a request.

    A free function taking the slot, not a `Store` method taking an id, because the two answer paths
    learn the truth at different moments. A `replace` answers inside the request hook and can credit
    at once; a `patch` is only an answer once it has merged into a real upstream response, one hook
    and a network round trip later — and by then the scenario may have been switched or the rule
    replaced.

    Holding the slot rather than the id is what makes that safe. `_activate` clears the runtime
    container, `add_override` pops from it and `reset_runtime` replaces the entry, so a slot captured
    before any of those is orphaned: crediting it mutates a dict nothing can reach. A patch that lands after a scenario
    switch therefore credits nobody, instead of crediting whatever rule in the new scenario happens to
    share its id.
    """
    slot["answers"] += 1  # `_new_slot` is the only maker of a slot, and it always seeds this


class Store:
    def __init__(self) -> None:
        self.scenarios: dict[str, dict] = {}
        self.active_name: str = "default"
        self.recent: deque[dict] = deque(maxlen=config.RECENT_CAP)
        self.load_problems: list[str] = []
        # The same problems, keyed by the scenario each belongs to. A diagnostic string cannot be
        # matched back to its scenario: a file may be named `orders-outage.json: backup.json`, and
        # the `skipped <file>: <error>` line it produces then reads exactly like one about
        # `orders-outage`. A caller deciding whether to run against a scenario — `up --use` does —
        # needs the answer, not a resemblance to it.
        #
        # A file whose *name* was rejected is deliberately absent: it could never have become a
        # scenario, so there is no scenario for it to be reported against.
        self.scenarios_not_whole: dict[str, list[str]] = {}
        self._load()

    # MARK: - Loading / persistence

    def _load(self) -> None:
        # Before the mkdir, which would otherwise create an empty `scenarios/` beside the profile's
        # real `sessions/` and start the proxy carrying only `default` — see
        # test_store_refuses_a_legacy_sessions_layout.
        refuse_legacy_layout(config.PROFILE_DIR)
        config.SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        for file in scenario_files():
            scenario, problems = load_scenario_file(file)
            # Which scenario these problems belong to. `load_scenario_file` reports the *file*,
            # because that is what an offline command is looking at; a running proxy is asked
            # about scenarios instead — `up --use NAME` has to know whether NAME loaded whole.
            #
            # A file that yielded nothing still names one, when its name would have been a legal
            # scenario: the scenario the operator asked for is the one that is missing, and
            # answering "no problems with NAME" because NAME never loaded is the failure this
            # whole map exists to prevent. Only a file that could not have named a scenario at all
            # is recorded against none.
            owner = scenario["name"] if scenario else _intended_scenario_name(file)
            for problem in problems:
                self._problem(problem, owner)
            if scenario is None:
                continue
            self.scenarios[scenario["name"]] = scenario

        if "default" not in self.scenarios:
            # In memory only. Writing it would dirty a profile kept in git the first time the
            # proxy started, which is exactly what the profile/state split exists to prevent.
            self.scenarios["default"] = _empty_scenario("default")

        if config.STATE_FILE.exists():
            try:
                state = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(state, dict) and state.get("active") in self.scenarios:
                    # persist=False: reading the pointer must not rewrite it.
                    self._activate(state["active"], persist=False)
            except (json.JSONDecodeError, OSError):
                pass
        # Scenarios are NOT rewritten on startup: a profile kept in git must stay clean until
        # something actually changes.

    def _problem(self, message: str, scenario: str | None = None) -> None:
        """Record a problem, and — when it belongs to one — the scenario it stopped loading whole.

        Recorded only; addon.running re-emits these through the logger once it is up, and the
        control API exposes them. Printing here would double-report into the same log file.
        """
        self.load_problems.append(message)
        if scenario is not None:
            self.scenarios_not_whole.setdefault(scenario, []).append(message)

    def _forget_load_problems(self, name: str) -> None:
        """Drop what was recorded against `name`: the scenario it described is no longer there.

        `scenarios_not_whole` is a claim about the scenario under a name *now*, not a history, and
        replacing a scenario is how an operator recovers from a file that would not load — a
        malformed `orders-outage.json`, then `scenario new orders-outage`. Keeping the entry would make
        that recovery invisible: every rule installed, and `up --use orders-outage` still refusing
        to launch the app over a file that no longer decides anything.

        `load_problems` is untouched, because it is the other thing: a record of what startup found,
        which stays true however the store is edited afterwards.
        """
        self.scenarios_not_whole.pop(name, None)

    def _write_scenario(self, name: str, scenario: dict) -> None:
        """Write the scenario someone *proposes* to store under `name`, not the one already there.

        Taking the dict rather than looking it up is what lets a mutator write its candidate before
        publishing it, so a failed write leaves nothing half-applied.
        """
        config.atomic_write(scenario_path(name), json.dumps(_persistable(scenario), indent=2))

    def _persist_state(self, name: str) -> None:
        """Record `name` as active. Takes the name for the same reason `_write_scenario` takes the
        scenario: the pointer is written before `active_name` moves."""
        config.atomic_write(config.STATE_FILE, json.dumps({"active": name}, indent=2))

    def _activate(self, name: str, *, persist: bool = True) -> None:
        """The one place `active_name` changes.

        Switching scenarios clears the destination's cursors, so a scenario always begins at its
        first step. This is a helper rather than a line repeated at each call site because there are
        four of them — startup, the self-heal in `active_scenario`, `set_active` and
        `delete_scenario` — and the last two are easy to miss: deleting the active scenario falls back
        to `default` without going anywhere near `set_active`.

        The pointer is written first: a switch that cannot be recorded must not happen at all, or
        the destination's cursors are rewound for a scenario that reverts on the next restart.
        """
        if persist:
            self._persist_state(name)
        self.active_name = name
        scenario = self.scenarios.get(name)
        if scenario is not None:
            _runtime(scenario).clear()

    # MARK: - Active scenario / overrides

    def active_scenario(self) -> dict:
        if self.active_name not in self.scenarios:
            self.scenarios.setdefault("default", _empty_scenario("default"))
            self._activate("default", persist=False)
        return self.scenarios[self.active_name]

    def active_overrides(self) -> list[dict]:
        return self.active_scenario().setdefault("overrides", [])

    def find_override(self, method: str, pathname: str, query: dict[str, str], body_text: str) -> dict | None:
        return rules.find_override(self.active_overrides(), method, pathname, query, body_text)

    def add_override(self, payload: dict) -> dict:
        override = {"active": True, **rules.validate_override(payload)}
        override["id"] = override.get("id") or f"ovr_{secrets.token_hex(3)}"  # after the spread
        scenario = self.active_scenario()
        overrides = self.active_overrides()
        existing = next((i for i, o in enumerate(overrides) if o.get("id") == override["id"]), None)
        if existing is not None:
            candidate = [*overrides[:existing], override, *overrides[existing + 1 :]]
        else:
            candidate = [*overrides, override]
        self._write_scenario(self.active_name, {**scenario, "overrides": candidate})
        scenario["overrides"] = candidate
        # The rule at this id is now a different rule; its old cursor describes steps that may no
        # longer exist. Note this only fires for a caller that supplied an explicit id — an id-less
        # `override add` mints a fresh random one and so replaces nothing. It happens after the
        # write for the same reason the list does: if the write failed the old rule is still live,
        # and its cursor and answer count still describe it.
        _runtime(scenario).pop(override["id"], None)
        return override

    def clear_overrides(self) -> None:
        scenario = self.active_scenario()
        self._write_scenario(self.active_name, {**scenario, "overrides": []})
        scenario["overrides"] = []
        _runtime(scenario).clear()

    # MARK: - Sequences

    def sequenced_overrides(self) -> list[dict]:
        """Active sequenced rules in the active scenario.

        Excludes `active: false`, matching what `find_override` already does — a disabled rule that
        still advanced on the wire would be a rule doing something while switched off.
        """
        return [
            override
            for override in self.active_overrides()
            if rules.is_active(override) and rules.sequence_steps(override) is not None
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

        entry = _rule_runtime(self.active_scenario(), override["id"])
        cursor, count = entry["cursor"], len(steps)
        action, view = rules.resolve_step(override, cursor)
        if cursor >= count:
            entry["overrunSeen"] = True  # sticky: the cursor alone cannot record this
        else:
            # Counted here and not in `bump_selected`, because this is the only path a winning
            # rule takes regardless of trigger — an `advanceOn` rule serves without ever bumping.
            entry["serves"][cursor + 1] = entry["serves"].get(cursor + 1, 0) + 1
        progress = {
            "sequenceId": override["id"],
            "runId": entry["runId"],
            "selectedStep": (cursor + 1) if cursor < count else None,
            "stepCount": count,
            "advanceEvents": cursor,  # for the exhaustion message; not recorded in /recent
            # This request went past the planned steps — an event, distinct from the live
            # `hasOverrun` below, which says one has happened at some point.
            "overrun": cursor >= count,
        }
        return action, view, progress

    def answer_slot(self, override_id: str) -> dict:
        """The runtime entry to credit when this rule answers. See `credit`."""
        return _rule_runtime(self.active_scenario(), override_id)

    def answer_states(self) -> list[dict]:
        """How many requests each rule in the active scenario has answered, and in which run.

        `runId` is the same token `reset` hands back and `sequence_states` reports, so a caller can
        demand that the count belong to the boundary it drew rather than to whatever run happens to
        be current when it reads. Without it a count is only ever "some run's", and a reset, a rule
        replaced under the same id, or a scenario switch silently substitutes another run's evidence.

        `None` means this rule has no run at all — never reset, never near a request, or its runtime
        dropped by a scenario switch or a replacement. That is a different fact from a count of zero
        ("this run happened and the rule answered nothing") and must not be read as one.
        """
        scenario = self.active_scenario()
        runtime = _runtime(scenario)
        states = []
        for override in self.active_overrides():
            # Read, never create: asking how many answers a rule has must not mint runtime state
            # for a rule that has never been near a request. One lookup feeds both fields, so a
            # count can never be reported under a run id it was not counted in.
            entry = runtime.get(override["id"]) or {}
            states.append(
                {
                    "id": override["id"],
                    "active": rules.is_active(override),
                    "count": entry.get("answers", 0),
                    "runId": entry.get("runId"),
                }
            )
        return states

    def bump_selected(self, override: dict) -> None:
        """Advance a rule whose trigger is `self`: it answered, so it moves.

        A rule with an explicit `advanceOn` is untouched here — it moves only when its own matcher
        sees a request, which is what makes repeated fetches idempotent.
        """
        steps = rules.sequence_steps(override)
        if steps is None or rules.advance_matcher(override) is not None:
            return
        entry = _rule_runtime(self.active_scenario(), override["id"])
        entry["cursor"] = rules.bumped(entry["cursor"], len(steps))

    def advance_matching(self, method: str, pathname: str, query: dict[str, str], body_text: str) -> list[str]:
        """Advance every rule whose explicit `advanceOn` fits this request; return the ids moved."""
        scenario = self.active_scenario()
        advanced = []
        for override in self.sequenced_overrides():
            matcher = rules.advance_matcher(override)
            if matcher is None:
                continue
            if not rules.matches_matcher(matcher, method, pathname, query, body_text):
                continue
            steps = rules.sequence_steps(override) or []
            entry = _rule_runtime(scenario, override["id"])
            entry["cursor"] = rules.bumped(entry["cursor"], len(steps))
            advanced.append(override["id"])
        return advanced

    def sequence_states(self) -> list[dict]:
        """Live state: what the next request would do, not what previous ones did."""
        scenario = self.active_scenario()
        states = []
        for override in self.sequenced_overrides():
            count = len(rules.sequence_steps(override) or [])
            entry = _rule_runtime(scenario, override["id"])
            cursor = entry["cursor"]
            states.append(
                {
                    "id": override["id"],
                    "runId": entry["runId"],
                    "advanceOn": "self" if rules.advance_matcher(override) is None else "match",
                    "nextStep": (cursor + 1) if cursor < count else None,
                    "stepCount": count,
                    "exhausted": cursor >= count,  # no planned step remains
                    "hasOverrun": entry["overrunSeen"],  # a request was actually served past it
                    # String keys, matching what any JSON round-trip would force anyway — a consumer
                    # must not need to know whether the payload came straight from the store.
                    "serves": {str(step): n for step, n in entry["serves"].items()},
                }
            )
        return states

    def reset_runtime(self, override_id: str | None = None) -> dict | None:
        """Start a fresh run: rewind sequence cursors and clear answer counts. None if `override_id`
        names no rule in the active scenario.

        None rather than an empty result: "I reset nothing" and "there is no such rule" are
        different answers, and a caller that cannot tell them apart will believe a typo worked.

        Scoped to every rule, not just sequenced ones, because every rule now carries runtime state.
        Refusing to reset a plain rule would leave the one boundary a test can draw — reset, trigger,
        assert — unavailable to exactly the rules that need it most.
        """
        scenario = self.active_scenario()
        known = {override["id"] for override in self.active_overrides()}
        if override_id is not None:
            if override_id not in known:
                return None
            targets = [override_id]
        else:
            targets = sorted(known)

        runtime = _runtime(scenario)
        reset = {}
        for target in targets:
            previous = (runtime.get(target) or {}).get("runId")
            runtime[target] = _new_slot(previous)  # replaced, not cleared: a slot captured before
            reset[target] = runtime[target]["runId"]  # the reset credits nobody — see `credit`
        return {"scenario": self.active_name, "reset": reset}

    # MARK: - Scenarios

    def list_scenarios(self) -> dict:
        return {
            "active": self.active_name,
            "scenarios": [
                {
                    "name": scenario["name"],
                    "overrideCount": len(scenario.get("overrides", [])),
                    "verified": scenario.get("verified", False),
                    "notes": scenario.get("notes", ""),
                }
                for scenario in self.scenarios.values()
            ],
        }

    def create_scenario(self, name: str, clone_from: str | None = None) -> None:
        """Raises KeyError if clone_from names a scenario that does not exist — silently handing
        back an empty scenario instead is a false success the caller cannot see."""
        name = safe_component(name, "scenario name")
        if name in self.scenarios:
            raise FileExistsError(name)
        if clone_from:
            if clone_from not in self.scenarios:
                raise KeyError(clone_from)
            base = _clone(self.scenarios[clone_from], name)
        else:
            base = _empty_scenario(name)
        self._write_scenario(name, base)
        self.scenarios[name] = base
        self._forget_load_problems(name)

    def set_active(self, name: str) -> dict | None:
        if name not in self.scenarios:
            return None
        previous = {"name": self.active_name, "overrideCount": len(self.active_overrides())}
        self._activate(name)
        return previous

    def delete_scenario(self, name: str) -> bool:
        if name == "default" or name not in self.scenarios:
            return False
        path = scenario_path(name)
        # Switch away BEFORE removing: set_active reads the outgoing scenario's override count.
        if self.active_name == name:
            self._activate("default")
        del self.scenarios[name]
        # Before the unlink, which may fail: the scenario is already out of memory either way, and
        # an entry left behind would be inherited by the next scenario created under this name.
        self._forget_load_problems(name)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError) as error:
            self._problem(f"could not delete {path.name}: {error}")
            return False
        return True

    # MARK: - Recent

    def record_recent(self, entry: dict) -> None:
        entry.setdefault("time", _now_iso())
        self.recent.appendleft(entry)

    def recent_list(self) -> list[dict]:
        return list(self.recent)
