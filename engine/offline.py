"""Checking a scenario without changing anything: `validate` and `explain-match`.

`validate` and `explain-match --scenario` read scenario files and never touch the proxy;
`explain-match` without `--scenario` reads the running proxy's rule list through `api`, and only
reads it. Nothing here starts a process, writes a file or changes network settings.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import click

import api
import config
import rules
import store
import ui

# MARK: - Offline inspection
#
# The file-reading path. Nothing here starts a process, touches network settings or constructs a
# `Store` — a store creates directories, synthesises a `default` scenario and reads the
# active-scenario pointer, all of which are changes to a profile somebody else may be using.
# `store.load_scenario_file` is the same function startup loads with, so what these commands
# report is what the proxy would do, not a second opinion about it. (`explain_match` without
# `--scenario` is the one call to the control API in this module, and it only reads.)


def _resolve_scenario(name: str) -> tuple[Path | None, list[str]]:
    """The file holding the saved scenario called `name`, or why there is not one.

    A name that names nothing is an error, never an empty scenario: a typo and a scenario with no
    rules in it need completely different fixes, and only one of them is worth a hint listing the
    names that *do* exist. Shared by both commands so a name is looked up, and refused, one way.

    Resolved against the identities discovery found, not by asking whether a path `is_file()`: on a
    case-insensitive filesystem `validate Orders-Outage` used to open `orders-outage.json` and bless
    a name the running proxy would refuse, and a file that discovery *skipped* — one of a pair
    differing only by case — used to validate cleanly under a name nothing serves. See
    test_validate_by_name_refuses_a_colliding_or_miscased_identity.
    """
    try:
        store.scenario_parts(name)
        files, discovery = store.scenario_files()
    except store.UnsafeName as error:
        return None, [str(error)]
    except store.LegacyProfileLayout as error:
        # A problem, not a bare sentence printed on the way out: both callers report through a
        # `--json` envelope, and a profile whose scenarios are all under the old name must not read
        # as one that simply lacks the name asked for — see test_validate_refuses_a_legacy_sessions_layout.
        return None, [str(error)]
    # The non-throwing form: `scenario_files` returns every file worth explaining, including ones
    # whose name could never be an identity (`.hidden.json`). Calling the throwing one here turned
    # an unrelated file in the directory into a traceback out of `validate NAME` — see
    # test_an_unnameable_file_does_not_break_a_lookup_by_name.
    identities = {identity: file for file in files if (identity := store._owner(file)) is not None}
    if name not in identities:
        known = ", ".join(sorted(identities)) or "none"
        # The problems recorded against this identity travel with the refusal: a file discovery
        # skipped is why the name is not here, and reporting only "not found" would send someone
        # looking for a file that is sitting right there.
        blamed = [problem for owner, problem in discovery if owner == name]
        return None, [f"no scenario {name!r} in {config.SCENARIOS_DIR} (found: {known})", *blamed]
    return identities[name], []


def _offline_scenario(name: str) -> tuple[dict | None, list[str]]:
    """Load the saved scenario called `name` from its file: `(scenario, problems)`."""
    path, problems = _resolve_scenario(name)
    if path is None:
        return None, problems
    scenario, file_problems = store.load_scenario_file(path)
    return scenario, [*problems, *file_problems]


def _validation_report(file: Path) -> dict:
    """One file's verdict. `loaded` and `ok` are separate answers on purpose: a scenario can load
    and still be missing the rule you came for, and `problems` says which."""
    scenario, problems = store.load_scenario_file(file)
    return {
        # The identity, not the stem: two groups may each hold a `retry.json`, and a report that
        # called both `retry` would name neither. `_relative_label` never raises, so a file that
        # could not be an identity at all still gets a verdict rather than a traceback — see
        # test_validate_reports_a_dotfile_as_a_verdict_not_a_traceback.
        "name": store._relative_label(file).removesuffix(".json"),
        "file": str(file),
        "loaded": scenario is not None,
        "ok": scenario is not None and not problems,
        # None, not 0: a file that was refused whole has no rule count, and reporting zero would
        # read as a scenario that loaded and happens to be empty.
        "overrideCount": len(scenario.get("overrides", [])) if scenario else None,
        "problems": problems,
    }


@click.command()
@click.argument("name", metavar="[SCENARIO]", required=False)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def validate(name: str | None, as_json: bool) -> None:
    """Check saved scenario files the way the proxy reads them — without starting anything.

    With no SCENARIO, checks every file in `<profile>/scenarios/`.

    Exits non-zero unless every scenario named can be accepted whole. That is the point: startup
    reports a malformed rule and carries on with the rest of the file, so a scenario can be live
    and quietly missing the one rule you are relying on. This is where that shows up, before a
    build and a walk through the app blames the app.

    Nothing here starts a proxy, changes network settings, or writes to the profile.
    """
    # `problems` holds what went wrong with the *request* rather than with a file — a name that
    # names nothing, a name that could not be one, a profile with no scenarios in it. Kept separate
    # from the per-file verdicts, and reported through the same two outputs, so that every way this
    # command can fail is still the shape `--json` promises. Printing a bare sentence on these
    # paths, as this first did, hands a parsing caller something it cannot read on exactly the
    # failures it most needs to act on.
    files: list[Path]
    if name is not None:
        path, problems = _resolve_scenario(name)
        files = [path] if path is not None else []
    else:
        try:
            files, discovery = store.scenario_files()
        except store.LegacyProfileLayout as error:
            files, problems = [], [str(error)]
        else:
            # Discovery problems are the run's problems: a subtree that could not be read, or a pair
            # of files colliding by case, means this command checked fewer scenarios than the profile
            # holds. Dropping them would let `validate` report "ok" for a profile it could not see.
            problems = [problem for _owner, problem in discovery]
            if not files and not problems:
                # Not a green "nothing wrong": a command named for checking scenarios that checked
                # none has not validated anything, and the usual cause is the wrong --profile. Only
                # when discovery itself had nothing to say — a profile whose every file was skipped
                # has already been told why, and adding "no scenario files" would send the reader
                # to --profile over files that are sitting right there.
                problems = [
                    f"no scenario files in {config.SCENARIOS_DIR} — check --profile, or create "
                    f"a profile with `lyrebird init {config.PROFILE_DIR}`"
                ]

    reports = [_validation_report(file) for file in files]
    ok = not problems and all(report["ok"] for report in reports)

    if as_json:
        click.echo(json.dumps({"ok": ok, "problems": problems, "scenarios": reports}, indent=2))
        raise SystemExit(0 if ok else 1)

    for problem in problems:
        click.echo(f"{ui.RED}✗ {problem}{ui.R}")

    for report in reports:
        count = report["overrideCount"]
        # A trailing space as well as the padding: a scenario name may be longer than the column,
        # and without it a long name runs straight into its rule count.
        label = f"{report['name']:<24} "
        if report["ok"]:
            summary = f"{ui.GREEN}✓{ui.R} {label}{ui.DIM}{count} rule(s){ui.R}"
        elif report["loaded"]:
            summary = f"{ui.RED}✗{ui.R} {label}{ui.DIM}{count} rule(s) kept, {len(report['problems'])} dropped{ui.R}"
        else:
            summary = f"{ui.RED}✗{ui.R} {label}{ui.DIM}not loaded at all{ui.R}"
        click.echo(summary)
        for problem in report["problems"]:
            click.echo(f"    {ui.DIM}{problem}{ui.R}")

    if not ok:
        if reports:
            click.echo(
                f"{ui.RED}✗ {sum(1 for r in reports if not r['ok'])} of {len(reports)} "
                f"scenario(s) cannot be accepted whole{ui.R}"
            )
        raise SystemExit(1)
    click.echo(f"{ui.GREEN}✓ {len(reports)} scenario(s) load whole{ui.R}")


@click.command(name="explain-match")
@click.argument("method")
@click.argument("path")
@click.option("--body", default="", help="Request body text, for rules using bodyContains.")
@click.option(
    "--scenario",
    "scenario_name",
    default=None,
    metavar="NAME",
    help="Explain against this saved scenario file instead of the running proxy.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def explain_match(method: str, path: str, body: str, scenario_name: str | None, as_json: bool) -> None:
    """Which rule a request would select, and why each of the others would not.

    Answers at the keyboard what otherwise costs a cold launch and a walk through the app. It also
    separates the two things "no match" collapses together: a rule this says would be selected, that
    then never fires, means the app did not make the request you assumed it did.

    PATH may carry a query string: `lyrebird explain-match GET '/api/items?kind=alpha'`.

    With --scenario it reads a saved scenario file instead of the running proxy: no proxy, no network
    change, and nothing written. The verdict comes from the same matching code the engine runs, and
    the scenario is read by the same loader startup uses, so a rule the proxy would drop is dropped
    here too — and reported, with a non-zero exit, rather than silently absent from the ranking.

    It reports what would be *selected*, never what would be returned. A `patch` answers only if the
    upstream response turns out to be JSON, and which step a sequence would serve depends on run
    state this deliberately neither reads nor touches.
    """
    split = urllib.parse.urlsplit(path)
    # First value wins for a repeated key, as it does on the wire: the addon builds its query from
    # mitmproxy's MultiDict, whose lookup returns the first. `dict(parse_qsl(...))` keeps the last,
    # and for `?kind=alpha&kind=beta` this command then explained a selection the proxy never made.
    query: dict[str, str] = {}
    for key, value in urllib.parse.parse_qsl(split.query, keep_blank_values=True):
        query.setdefault(key, value)

    problems: list[str] = []
    if scenario_name is not None:
        scenario, problems = _offline_scenario(scenario_name)
        if scenario is None:
            # No ranking to show and no honest way to imply one — every rule in the file is absent,
            # not out-ranked, and "nothing would be selected" is the same sentence an empty scenario
            # produces. Say which file could not be read instead.
            if as_json:
                click.echo(json.dumps({"selected": None, "candidates": [], "problems": problems}, indent=2))
            else:
                for problem in problems:
                    click.echo(f"{ui.RED}✗ {problem}{ui.R}")
            raise SystemExit(1)
        overrides = list(scenario.get("overrides", []))
    else:
        overrides = api._control("/__mock__/overrides") or []
    selected = rules.find_override(overrides, method, split.path, query, body)

    report = []
    for override in overrides:
        reason = rules.explain_matcher(override.get("match") or {}, method, split.path, query, body)
        report.append(
            {
                "id": override.get("id"),
                "active": rules.is_active(override),
                "matched": reason is None,
                "reason": reason,
                "selected": override is selected,
                "match": override.get("match") or {},
                "sequenced": rules.sequence_steps(override) is not None,
            }
        )

    # A rule the loader dropped is not in the ranking, so a ranking presented without saying so
    # answers "which rule wins" while hiding that the rule you asked about was never a candidate.
    # Non-zero for the same reason. The proxy would drop them too, so the winner named here is the
    # one it would pick — what is not true is that this ranking covers the rules in the file, and
    # "my rule was never loaded" is the question people run this command to answer.
    whole = not problems

    if as_json:
        payload: dict[str, Any] = {"selected": (selected or {}).get("id"), "candidates": report}
        if scenario_name is not None:
            # Only with --scenario: there is a file whose problems we actually read. Reporting `[]`
            # for the live proxy would claim its scenario loaded whole, which this never checked.
            payload["problems"] = problems
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(0 if selected and whole else 1)

    if selected is None:
        click.echo(f"{ui.RED}✗ no active rule would be selected for {method.upper()} {path}{ui.R}")
    else:
        sequenced = next(c["sequenced"] for c in report if c["selected"])
        click.echo(
            f"→ {ui.BOLD}{selected['id']}{ui.R} is selected  "
            f"{ui.DIM}({'sequence' if sequenced else selected.get('mode')}){ui.R}"
        )
        if selected.get("mode") == "patch":
            click.echo(f"  {ui.DIM}a patch answers only if the upstream response is JSON{ui.R}")
        elif sequenced:
            click.echo(f"  {ui.DIM}which step it serves depends on run state, not read here{ui.R}")

    # The over-match, made visible before it silently answers for a screen nobody is testing.
    ui._section(
        "also matched, ranked lower",
        [(c["id"], json.dumps(c["match"])) for c in report if c["matched"] and c["active"] and not c["selected"]],
    )
    ui._section("did not match", [(c["id"], c["reason"]) for c in report if not c["matched"] and c["active"]])
    ui._section(
        "inactive",
        [(c["id"], "would have matched" if c["matched"] else c["reason"]) for c in report if not c["active"]],
    )
    if problems:
        # Last, and not one of the `_section` blocks above: these rules are not ranked lower or
        # inactive, they are absent — the ranking above was computed without them.
        click.echo(f"  {ui.RED}dropped at load, so not in the ranking above:{ui.R}")
        for problem in problems:
            click.echo(f"    {ui.DIM}{problem}{ui.R}")

    raise SystemExit(0 if selected and whole else 1)
