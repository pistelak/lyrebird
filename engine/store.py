"""In-memory scenario/override store, read from JSON under the active profile.

Nothing here writes into a profile: scenario files are written by whoever authors them, and
`reload_scenarios` is the one way an edited file reaches a running proxy. A file that does not load
whole refuses the whole reload, which is why validation lives at load and nowhere else.

Single-loop safety: mitmproxy runs one asyncio loop, and the aiohttp control server runs on that
same loop, so flow-hook reads and control-API calls are serialised — no locking required.

Every name that becomes a path component (a scenario name) is validated and the resolved path is
checked for containment before any read or listing. These names arrive from an unauthenticated
local HTTP API, so they are treated as untrusted input.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import config
import rules

# Deliberately strict: a path component, never a path.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class UnsafeName(ValueError):
    pass


class ReloadRefused(RuntimeError):
    """A reload that published nothing. `problems` is why, one line each."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class LegacyProfileLayout(RuntimeError):
    """A profile still keeping its scenarios in `sessions/`, from before the directory was renamed."""


def refuse_legacy_layout(profile_dir: Path) -> None:
    """Refuse a profile whose scenarios are still under `sessions/`, naming the move that fixes it.

    Raised rather than reported as a directory that could not be read: an unrenamed profile is a
    profile whose scenarios are all there, under the old name, and "cannot read scenarios/" sends
    the operator looking for a permissions problem instead of naming the `mv` that fixes it. See
    test_store_refuses_a_legacy_sessions_layout.

    Takes the profile explicitly rather than reading `config.PROFILE_DIR`, so `init PATH` can check
    the directory it is about to write into.
    """
    legacy, current = profile_dir / "sessions", profile_dir / "scenarios"
    if legacy.is_dir() and not current.exists():
        # `shlex.join`, not an f-string: a profile under a path with a space in it produced a
        # remedy the shell reads as four arguments, so the one line this message exists to hand
        # over was the one thing that did not work — see
        # test_the_legacy_remedy_is_a_command_a_shell_can_run.
        raise LegacyProfileLayout(
            f"{profile_dir} keeps its scenarios in {legacy} — Lyrebird reads {current} now.\n"
            f"  rename it:  {shlex.join(['mv', str(legacy), str(current)])}"
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


def scenario_parts(name: object) -> tuple[str, str]:
    """Split a scenario identity into `(group, leaf)`, validating both as path components.

    Scenarios nest exactly one level: `orders-outage` is `("", "orders-outage")` and
    `checkout/orders-outage` is `("checkout", "orders-outage")`. Every component still goes through
    `safe_component`; the separator is never handed to it, because loosening that pattern to accept
    one would loosen it for every other caller too.

    The depth refusal keeps the words "invalid scenario name": `../../etc/passwd` reaches this
    branch rather than `safe_component`, and the CLI tests assert that wording — see
    test_qualified_names_refuse_traversal_separators_and_depth.
    """
    value = str(name or "")
    group, separator, leaf = value.partition("/")
    if not separator:
        return "", safe_component(value, "scenario name")
    if "/" in leaf:
        raise UnsafeName(f"invalid scenario name {value!r} — scenarios nest one level deep (group/name)")
    return safe_component(group, "scenario group"), safe_component(leaf, "scenario name")


def scenario_group(name: object) -> str:
    """The group a scenario identity names, or `""` for one at the root of `scenarios/`."""
    return scenario_parts(name)[0]


def scenario_path(name: str) -> Path:
    """Where the scenario called `name` lives. Resolves only — it creates nothing."""
    group, leaf = scenario_parts(name)
    if not group:
        return _contained(config.SCENARIOS_DIR, f"{leaf}.json")
    return _contained(config.SCENARIOS_DIR, group, f"{leaf}.json")


def scenario_identity(file: Path) -> str:
    """The identity a discovered file carries: `name`, or `group/name` one level down.

    Raises `UnsafeName` for anything else — a third level, or a component that could not be one.
    """
    parent = file.parent
    if parent == config.SCENARIOS_DIR:
        return scenario_parts(file.stem)[1]
    if parent.parent == config.SCENARIOS_DIR:
        group, leaf = scenario_parts(f"{parent.name}/{file.stem}")
        return f"{group}/{leaf}"
    raise UnsafeName(f"invalid scenario name {file.stem!r} — scenarios nest one level deep (group/name)")


def _relative_label(file: Path) -> str:
    """How a problem line names a file: `broken.json`, or `checkout/broken.json` inside a group.

    Never raises, because it labels the files that could *not* be identified as well as the ones
    that could — a `.hidden.json` has to produce a verdict, not a traceback. Root labels are
    unchanged, so the existing `skipped broken.json:` assertions still hold.
    """
    try:
        return file.relative_to(config.SCENARIOS_DIR).as_posix()
    except ValueError:
        return file.name


def _case_collisions(names: list[str]) -> dict[str, list[str]]:
    """Sibling names that differ only by case, keyed by every member of the collision."""
    folded: dict[str, list[str]] = {}
    for name in names:
        folded.setdefault(name.lower(), []).append(name)
    return {name: members for members in folded.values() if len(members) > 1 for name in members}


def _entries(directory: Path) -> tuple[list[Path], str | None]:
    """What is in `directory`, or the message saying it could not be read.

    A directory that cannot be listed is a reported problem, never an empty one: "there is nothing
    here" is a different claim from "I could not look" — see
    test_an_unreadable_group_is_a_reported_problem_not_an_empty_group.
    """
    try:
        return list(directory.iterdir()), None
    except OSError as error:
        return [], f"cannot read {_relative_label(directory)}/: {error}"


def _classify(entries: list[Path]) -> tuple[list[Path], list[Path], list[tuple[str | None, str]]]:
    """Sort one directory's entries into `(files, directories, problems)`.

    Every entry a person could have meant as a scenario is accounted for. Three shapes are easy to
    drop silently, and each of them was:

    - a `.json` entry that answers False to `is_file()` — a self-referencing or dangling link — is
      still a candidate file, because the loader has a verdict for it and an operator hand-editing
      this directory needs to read it (test_a_scenario_file_that_points_at_itself_does_not_stop_the_proxy_starting);
    - a *directory* named `default.json` is not a group, whatever `safe_component` makes of the
      name: treated as one it would have been scanned for scenarios inside, and an empty one would
      have produced no problem at all while `up --use default` served a synthesised default and
      called it whole;
    - an entry that is neither a file nor a directory is a dangling symlink. It matched no branch
      and disappeared.
    """
    files: list[Path] = []
    directories: list[Path] = []
    problems: list[tuple[str | None, str]] = []
    for entry in sorted(entries):
        if entry.is_dir():
            if entry.suffix == ".json":
                problems.append((_owner(entry), f"skipped {_relative_label(entry)}: a directory, not a scenario file"))
            else:
                directories.append(entry)
        elif entry.suffix == ".json":
            files.append(entry)
        elif entry.is_symlink():
            problems.append((None, f"skipped {_relative_label(entry)}: a symlink pointing at nothing"))
    return files, directories, problems


def _owner(file: Path) -> str | None:
    """The identity a problem about `file` belongs to, or None when it could not have one.

    The non-throwing half of `scenario_identity`, and the only one diagnostics may use: a file that
    could never name a scenario still has to produce a problem line rather than an exception.
    """
    try:
        return scenario_identity(file)
    except UnsafeName:
        return None


def _group_files(directory: Path, colliding: list[str] | None, problems: list[tuple[str | None, str]]) -> list[Path]:
    """The scenario files one group holds, appending whatever stopped one from being read."""
    label = directory.name
    if colliding:
        problems.append((None, f"skipped {'/, '.join(sorted(colliding))}/: names differ only by case"))
        return []
    try:
        safe_component(label, "scenario group")
    except UnsafeName as error:
        problems.append((None, f"skipped {label}/: {error}"))
        return []

    entries, failure = _entries(directory)
    if failure is not None:
        problems.append((None, failure))
        return []

    group_files, nested_directories, classification = _classify(entries)
    problems.extend(classification)
    for nested in nested_directories:
        problems.append((None, f"skipped {label}/{nested.name}/: scenarios nest one level deep (group/name)"))

    collisions = _case_collisions([file.name for file in group_files])
    kept: list[Path] = []
    for file in group_files:
        if members := collisions.get(file.name):
            names = ", ".join(f"{label}/{member}" for member in sorted(members))
            problems.append((_owner(file), f"skipped {names}: names differ only by case"))
            continue
        kept.append(file)
    return kept


def scenario_files() -> tuple[list[Path], list[tuple[str | None, str]]]:
    """Every scenario file in the profile, and what could not be read, in startup's order.

    Returns `(files, problems)`. Each problem carries the identity it belongs to, or None when it
    belongs to no single scenario: `up --use NAME` reads `scenariosNotWhole[NAME]` to decide whether
    the scenario it was asked for loaded whole, so a problem filed under no name would let a
    synthesised `default` pass as whole while its own file was being skipped — see
    test_sibling_names_that_differ_only_by_case_are_refused_and_blamed_on_each.

    Shared with the offline commands so "which files are a profile's scenarios" has one answer:
    a file the loader would read but an inspection command would not is a file whose problems
    only ever surface as a proxy that behaves oddly.

    Raises `LegacyProfileLayout` rather than globbing a directory that is not there: an old profile
    would otherwise report "no scenario files" for a profile that has them all, under the old name —
    see test_validate_refuses_a_legacy_sessions_layout.
    """
    refuse_legacy_layout(config.PROFILE_DIR)
    entries, failure = _entries(config.SCENARIOS_DIR)
    if failure is not None:
        return [], [(None, failure)]

    root_files, directories, problems = _classify(entries)

    files: list[Path] = []
    collisions = _case_collisions([file.name for file in root_files])
    for file in root_files:
        if members := collisions.get(file.name):
            problems.append((_owner(file), f"skipped {', '.join(sorted(members))}: names differ only by case"))
            continue
        files.append(file)

    group_collisions = _case_collisions([directory.name for directory in directories])
    for directory in directories:
        files.extend(_group_files(directory, group_collisions.get(directory.name), problems))
    return files, problems


def load_scenario_file(file: Path) -> tuple[dict | None, list[str]]:
    """Read one scenario file the way startup does: `(scenario, problems)`.

    `scenario` is None when nothing could be kept — an unsafe name, an unreadable or malformed file,
    a payload that is not an object, a `schemaVersion` this engine does not read — and `problems`
    then holds the one `skipped <file>: …` line saying so. Otherwise it is the normalised scenario
    and `problems` lists the rules that were reported-and-dropped, each prefixed with the file name.
    The two are distinguishable on purpose: "this scenario is not loaded" and "this scenario is
    loaded without the rule you are looking for" send an operator to different places.

    A module-level function rather than a `Store` method, so a command can ask "what would the proxy
    make of this file?" without constructing a store — which synthesises a `default` scenario and
    reads the whole profile, neither of which an inspection of one file should do.
    `Store._load` is a loop around this function, so an offline answer cannot drift from startup's.
    """
    label = _relative_label(file)
    try:
        name = scenario_identity(file)
        # Containment on the file about to be read, resolved through `scenario_path` so one identity
        # cannot get two verdicts: a scenario file symlinked out of `scenarios/` loaded into the
        # proxy while `scenario_path` refused that very scenario by name, and the permissive verdict
        # was the one that ran. Checking `file.parent` would only prove the file sits in its own
        # directory, which says nothing about where that directory is — see
        # test_the_loader_holds_a_file_to_the_containment_scenario_path_applies.
        scenario_path(name)
    except UnsafeName as error:
        return None, [f"skipped {label}: {error}"]
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        # UnicodeDecodeError is a ValueError and not a JSONDecodeError — it is raised by the decode,
        # before json sees anything — so without it a file of binary junk escaped all three arms and
        # took the proxy down at startup, from the one directory operators are told to hand-edit.
        return None, [f"skipped {label}: {error}"]
    try:
        scenario = rules.normalise_scenario(raw, name)
    except rules.ValidationError as error:
        return None, [f"skipped {label}: {error}"]
    return scenario, [f"{label}: {problem}" for problem in scenario.pop("_problems", [])]


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


# MARK: - Rule runtime
#
# Per-rule state that must never reach a profile: sequence cursors, and the count of requests each
# rule has answered. It hangs off the scenario under a leading underscore, which `normalise_scenario`
# strips on the way in — so it is scoped to its scenario without a second key, all from machinery
# that was already here for `_problems`.


def _runtime(scenario: dict) -> dict:
    return scenario.setdefault("_ruleRuntime", {})


def _new_run_id(previous: str | None = None) -> str:
    """A fresh run token, guaranteed different from the one it replaces.

    This token is what stops a retained event from an earlier run satisfying a `sequence wait`. The
    explicit inequality makes "a reset always changes the run" a guarantee instead of a probability.
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
    container, `reload_scenarios` builds scenarios that carry none, and `reset_runtime` replaces the
    entry, so a slot captured before any of those is orphaned: crediting it mutates a dict nothing
    can reach. A patch that lands after a scenario switch therefore credits nobody, instead of
    crediting whatever rule in the new scenario happens to share its id.
    """
    slot["answers"] += 1  # `_new_slot` is the only maker of a slot, and it always seeds this


class Store:
    def __init__(self) -> None:
        self.scenarios: dict[str, dict] = {}
        self.active_name: str = "default"
        self.recent: deque[dict] = deque(maxlen=config.RECENT_CAP)
        # Numbers the entries in `recent`, so each one has a name a client can hold on to. Not
        # persisted and deliberately not unique across runs: the list itself lives and dies with the
        # proxy process, and an id that outlived it would be a promise this store cannot keep.
        self._recent_seq = 0
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
        # Which identities actually came from a file. `scenarios` alone cannot say: it holds a
        # synthesised `default` whether or not one was ever written.
        self._disk_names: set[str] = set()
        self._load()

    # MARK: - Loading / persistence

    @staticmethod
    def _read_snapshot() -> tuple[dict[str, dict], list[tuple[str | None, str]], set[str]]:
        """Read every scenario file: `(scenarios, problems, on_disk)`. Publishes nothing.

        `on_disk` is the set of identities that came from a file, which is not the same as the keys
        of `scenarios`: `default` is synthesised when no file provides it, and `reload_scenarios`
        has to tell "the file is gone" from "there never was one" — see
        test_reload_keeps_serving_a_virtual_default_that_was_never_on_disk.

        A static reader rather than part of `_load`, so a reload can build a candidate snapshot and
        throw it away without any of it having been visible.
        """
        scenarios: dict[str, dict] = {}
        files, problems = scenario_files()
        problems = list(problems)
        on_disk: set[str] = set()
        for file in files:
            scenario, file_problems = load_scenario_file(file)
            # Which scenario these problems belong to. `load_scenario_file` reports the *file*,
            # because that is what an offline command is looking at; a running proxy is asked
            # about scenarios instead — `up --use NAME` has to know whether NAME loaded whole.
            #
            # A file that yielded nothing still names one, when its name would have been a legal
            # scenario: the scenario the operator asked for is the one that is missing, and
            # answering "no problems with NAME" because NAME never loaded is the failure this
            # whole map exists to prevent. Only a file that could not have named a scenario at all
            # is recorded against none.
            owner = scenario["name"] if scenario else _owner(file)
            problems.extend((owner, problem) for problem in file_problems)
            if scenario is None:
                continue
            scenarios[scenario["name"]] = scenario
            on_disk.add(scenario["name"])
        return scenarios, problems, on_disk

    def _load(self) -> None:
        # Named as the rename it is, rather than left to `scenario_files` to report as a directory
        # it could not read — see test_store_refuses_a_legacy_sessions_layout. Nothing creates
        # `scenarios/`: `init` writes it, and a profile without one is a reported load problem.
        refuse_legacy_layout(config.PROFILE_DIR)
        self.scenarios, problems, self._disk_names = self._read_snapshot()
        for owner, problem in problems:
            self._problem(problem, owner)

        if "default" not in self.scenarios:
            # In memory only. Writing it would dirty a profile kept in git the first time the
            # proxy started, which is exactly what the profile/state split exists to prevent.
            self.scenarios["default"] = _empty_scenario("default")

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

    def _activate(self, name: str) -> None:
        """The one place `active_name` changes.

        Switching scenarios clears the destination's cursors, so a scenario always begins at its
        first step. This is a helper rather than a line repeated at each call site because there are
        four of them — startup, the self-heal in `active_scenario`, `set_active` and
        `reload_scenarios` — and the last is easy to miss: a reload reselects its target without
        going anywhere near `set_active`.
        """
        self.active_name = name
        scenario = self.scenarios.get(name)
        if scenario is not None:
            _runtime(scenario).clear()

    # MARK: - Active scenario / overrides

    def active_scenario(self) -> dict:
        if self.active_name not in self.scenarios:
            self.scenarios.setdefault("default", _empty_scenario("default"))
            self._activate("default")
        return self.scenarios[self.active_name]

    def active_overrides(self) -> list[dict]:
        return self.active_scenario().setdefault("overrides", [])

    def find_override(self, method: str, pathname: str, query: dict[str, str], body_text: str) -> dict | None:
        return rules.find_override(self.active_overrides(), method, pathname, query, body_text)

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
                    # Derived from the name, never stored: the folder a file sits in *is* the group,
                    # and a second copy in the JSON would be one a hand-moved file could contradict.
                    "group": scenario_group(scenario["name"]),
                    "overrideCount": len(scenario.get("overrides", [])),
                    "verified": scenario.get("verified", False),
                    "notes": scenario.get("notes", ""),
                }
                for scenario in self.scenarios.values()
            ],
        }

    def set_active(self, name: str) -> bool:
        # Validated before the membership test, so a name that could never be one is a refusal the
        # caller can act on rather than "not found" — see
        # test_activating_an_unsafe_qualified_name_is_a_400.
        scenario_parts(name)
        if name not in self.scenarios:
            return False
        self._activate(name)
        return True

    def reload_scenarios(self, use: str | None = None) -> dict:
        """Re-read every scenario file, all or nothing.

        The engine serves a snapshot, so a file added or moved by hand is invisible until this runs.
        Refuses on *any* discovery or load problem and publishes nothing: a reload that silently
        dropped the one scenario somebody had just edited would be indistinguishable from one that
        worked. Startup stays lenient by contrast — it has no earlier state to keep, and refusing
        there would mean a profile with one bad file could not be served at all.

        Prior run evidence does not survive: the new scenarios carry no runtime slots, so
        `answer_states` reports `runId: None` and a slot captured before the reload credits nobody.
        """
        scenarios, problems, on_disk = self._read_snapshot()
        if problems:
            raise ReloadRefused([problem for _owner, problem in problems])

        target = self.active_name if use is None else "/".join(p for p in scenario_parts(use) if p)
        # Checked against what is on disk *before* the virtual `default` is put back, so a
        # `default.json` that was there and is now gone is a refusal rather than a silent fallback
        # to an empty scenario. A `default` that was never on disk is the ordinary case — the
        # bundled examples ship no `default.json` — and stays reloadable.
        if target not in on_disk and not (target == "default" and "default" not in self._disk_names):
            raise ReloadRefused(
                [
                    f"scenario {target!r} is not in this profile any more — "
                    f"`lyrebird scenario reload --use NAME` selects the replacement"
                ]
            )
        if "default" not in scenarios:
            scenarios["default"] = _empty_scenario("default")

        self.scenarios = scenarios
        self._disk_names = on_disk
        self.load_problems = []
        self.scenarios_not_whole = {}
        self._activate(target)
        return {
            "active": self.active_name,
            "reloaded": len(self.scenarios),
            "scenarios": self.list_scenarios()["scenarios"],
        }

    # MARK: - Recent

    def record_recent(self, entry: dict) -> None:
        """Record one request, under an id this store gives it.

        The id is an identity of the engine's own making, because the alternatives are not
        identities: a client polls this list, and a selection keyed by position follows the row that
        slid into it while one keyed by content follows whichever repeat of the same request came
        last. Both quietly select something other than what the user clicked.

        Assigned, never defaulted: a caller's own `evt-2` would collide with the counter and a
        `{"id": None}` would leave a row nothing can select at all — one list with two rows under
        one name is worse than no id — see test_a_recent_id_replaces_whatever_the_caller_supplied.
        Onto a copy, so an id is not written back into a dict the caller still holds.
        """
        self._recent_seq += 1
        self.recent.appendleft({"time": _now_iso(), **entry, "id": f"evt-{self._recent_seq}"})

    def clear_recent(self) -> None:
        self.recent.clear()

    def recent_list(self) -> list[dict]:
        return list(self.recent)
