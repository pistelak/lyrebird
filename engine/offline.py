"""Checking a scenario without changing anything: `validate` and `explain-match`.

`validate` and `explain-match --session` read session files and never touch the proxy;
`explain-match` without `--session` reads the running proxy's rule list through `api`, and only
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
# `Store` — a store creates directories, synthesises a `default` session and reads the
# active-session pointer, all of which are changes to a profile somebody else may be using.
# `store.load_session_file` is the same function startup loads with, so what these commands
# report is what the proxy would do, not a second opinion about it. (`explain_match` without
# `--session` is the one call to the control API in this module, and it only reads.)


def _resolve_session(name: str) -> tuple[Path | None, list[str]]:
    """The file holding the saved session called `name`, or why there is not one.

    A name that names nothing is an error, never an empty session: a typo and a scenario with no
    rules in it need completely different fixes, and only one of them is worth a hint listing the
    names that *do* exist. Shared by both commands so a name is looked up, and refused, one way.
    """
    try:
        path = store.session_path(name)
    except store.UnsafeName as error:
        return None, [str(error)]
    if not path.is_file():
        known = ", ".join(file.stem for file in store.session_files()) or "none"
        return None, [f"no session {name!r} in {config.SESSIONS_DIR} (found: {known})"]
    return path, []


def _offline_session(name: str) -> tuple[dict | None, list[str]]:
    """Load the saved session called `name` from its file: `(session, problems)`."""
    path, problems = _resolve_session(name)
    return store.load_session_file(path) if path is not None else (None, problems)


def _validation_report(file: Path) -> dict:
    """One file's verdict. `loaded` and `ok` are separate answers on purpose: a session can load
    and still be missing the rule you came for, and `problems` says which."""
    session, problems = store.load_session_file(file)
    return {
        "name": file.stem,
        "file": str(file),
        "loaded": session is not None,
        "ok": session is not None and not problems,
        # None, not 0: a file that was refused whole has no rule count, and reporting zero would
        # read as a session that loaded and happens to be empty.
        "overrideCount": len(session.get("overrides", [])) if session else None,
        "problems": problems,
    }


@click.command()
@click.argument("name", metavar="[SESSION]", required=False)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def validate(name: str | None, as_json: bool) -> None:
    """Check saved session files the way the proxy reads them — without starting anything.

    With no SESSION, checks every file in `<profile>/sessions/`.

    Exits non-zero unless every session named can be accepted whole. That is the point: startup
    reports a malformed rule and carries on with the rest of the file, so a scenario can be live
    and quietly missing the one rule you are relying on. This is where that shows up, before a
    build and a walk through the app blames the app.

    Nothing here starts a proxy, changes network settings, or writes to the profile.
    """
    # `problems` holds what went wrong with the *request* rather than with a file — a name that
    # names nothing, a name that could not be one, a profile with no sessions in it. Kept separate
    # from the per-file verdicts, and reported through the same two outputs, so that every way this
    # command can fail is still the shape `--json` promises. Printing a bare sentence on these
    # paths, as this first did, hands a parsing caller something it cannot read on exactly the
    # failures it most needs to act on.
    files: list[Path]
    if name is not None:
        path, problems = _resolve_session(name)
        files = [path] if path is not None else []
    else:
        files, problems = store.session_files(), []
        if not files:
            # Not a green "nothing wrong": a command named for checking sessions that checked none
            # has not validated anything, and the usual cause is the wrong --profile.
            problems = [
                f"no session files in {config.SESSIONS_DIR} — check --profile, or create "
                f"a profile with `lyrebird init {config.PROFILE_DIR}`"
            ]

    reports = [_validation_report(file) for file in files]
    ok = not problems and all(report["ok"] for report in reports)

    if as_json:
        click.echo(json.dumps({"ok": ok, "problems": problems, "sessions": reports}, indent=2))
        raise SystemExit(0 if ok else 1)

    for problem in problems:
        click.echo(f"{ui.RED}✗ {problem}{ui.R}")

    for report in reports:
        count = report["overrideCount"]
        # A trailing space as well as the padding: a session name may be longer than the column,
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
                f"session(s) cannot be accepted whole{ui.R}"
            )
        raise SystemExit(1)
    click.echo(f"{ui.GREEN}✓ {len(reports)} session(s) load whole{ui.R}")


@click.command(name="explain-match")
@click.argument("method")
@click.argument("path")
@click.option("--body", default="", help="Request body text, for rules using bodyContains.")
@click.option(
    "--session",
    "session_name",
    default=None,
    metavar="NAME",
    help="Explain against this saved session file instead of the running proxy.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def explain_match(method: str, path: str, body: str, session_name: str | None, as_json: bool) -> None:
    """Which rule a request would select, and why each of the others would not.

    Answers at the keyboard what otherwise costs a cold launch and a walk through the app. It also
    separates the two things "no match" collapses together: a rule this says would be selected, that
    then never fires, means the app did not make the request you assumed it did.

    PATH may carry a query string: `lyrebird explain-match GET '/api/items?kind=alpha'`.

    With --session it reads a saved session file instead of the running proxy: no proxy, no network
    change, and nothing written. The verdict comes from the same matching code the engine runs, and
    the session is read by the same loader startup uses, so a rule the proxy would drop is dropped
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
    if session_name is not None:
        session, problems = _offline_session(session_name)
        if session is None:
            # No ranking to show and no honest way to imply one — every rule in the file is absent,
            # not out-ranked, and "nothing would be selected" is the same sentence an empty session
            # produces. Say which file could not be read instead.
            if as_json:
                click.echo(json.dumps({"selected": None, "candidates": [], "problems": problems}, indent=2))
            else:
                for problem in problems:
                    click.echo(f"{ui.RED}✗ {problem}{ui.R}")
            raise SystemExit(1)
        overrides = list(session.get("overrides", []))
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
        if session_name is not None:
            # Only with --session: there is a file whose problems we actually read. Reporting `[]`
            # for the live proxy would claim its session loaded whole, which this never checked.
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
