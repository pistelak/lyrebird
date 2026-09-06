"""Choosing what the proxy answers with: the active session, its rules, and the traffic it saw.

Every command here changes or reads the running proxy. `offline.py` is the half that inspects
files instead, and never changes anything.
"""

from __future__ import annotations

import json
import sys

import click

import api
import rules
import supervisor
import ui


@click.command()
@click.argument("name")
def use(name: str) -> None:
    """Switch the active session, for the requests that come after it.

    It does not relaunch anything, so an app that cached its launch response goes on showing the
    old scenario: relaunch it yourself, or start the run with `lyrebird up --use NAME`.
    """
    supervisor._activate_session(name)


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--matched", "only_matched", is_flag=True, help="Only requests an override answered.")
@click.option("--limit", default=20, help="How many to show.")
def recent(as_json: bool, only_matched: bool, limit: int) -> None:
    """What has come through the proxy, and which overrides answered it."""
    entries = api._control("/__mock__/recent") or []
    if only_matched:
        entries = [entry for entry in entries if entry.get("matched")]
    entries = entries[:limit]

    if as_json:
        click.echo(json.dumps(entries, indent=2))
        return
    if not entries:
        click.echo(f"{ui.DIM}(nothing yet){ui.R}")
        return
    for entry in entries:
        mark = f" → {entry['matched']}" if entry.get("matched") else ""
        skipped = f"  {ui.YELLOW}patch skipped: {entry['patchSkipped']}{ui.R}" if entry.get("patchSkipped") else ""
        step = ""
        if entry.get("sequenceId"):
            step = (f"  {ui.DIM}[{entry['sequenceId']} step {entry['selectedStep']}/"
                    f"{entry['stepCount']}]{ui.R}" if entry.get("selectedStep")
                    else f"  {ui.YELLOW}[{entry['sequenceId']} overrun]{ui.R}")
        advanced = f"  {ui.DIM}advanced {', '.join(entry['advanced'])}{ui.R}" if entry.get("advanced") else ""
        click.echo(f"  {entry['method']:6} {entry['status']}  {entry['path']}"
                   f"{mark}{step}{advanced}{skipped}")


@click.group()
def override() -> None:
    """Add or clear rules in the active session."""


# Built from the vocabulary itself, so the help cannot claim a different set of fields from the one
# validation accepts. A matcher field nobody can discover is reported as a missing feature — and the
# rule people write instead is a broader one that quietly answers for its neighbours.
_ADD_HELP = """Add a rule to the active session. RULE is JSON, or - to read stdin.

Takes effect immediately — no restart, and the session file is updated.

\b
    lyrebird override add '{"match":{"path":"/api/v1/orders/*"},"mode":"replace","status":500}'

An override accepts these fields, and only these:

\b
""" + "\n".join(
    f"    {field:<13} {description}" for field, description in rules.OVERRIDE_FIELD_HELP.items()
) + """

`match` accepts these fields, and only these:

\b
""" + "\n".join(
    f"    {field:<13} {description}" for field, description in rules.MATCHER_FIELD_HELP.items()
) + """

Constrain a rule as tightly as the thing you are testing. Sibling screens served from one path are
told apart by `query`, and a rule that leaves it out answers for all of them:

\b
    lyrebird override add '{"match":{"method":"GET","path":"/api/items","query":{"kind":"alpha"}},
                            "mode":"replace","status":200,"body":{}}'

`lyrebird explain-match GET '/api/items?kind=alpha'` shows which rule a request would select, and
which others it would also have matched.
"""


@override.command(name="add", help=_ADD_HELP)
@click.argument("rule")
def override_add(rule: str) -> None:
    raw = sys.stdin.read() if rule == "-" else rule
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        click.echo(f"{ui.RED}✗ not valid JSON: {error}{ui.R}")
        raise SystemExit(1) from None
    result = api._control("/__mock__/overrides", "POST", payload)
    click.echo(f"✓ added {result['id']}")


@override.command(name="clear")
@click.option("--force", is_flag=True, help="Required: this deletes rules and rewrites the file.")
def override_clear(force: bool) -> None:
    """Delete EVERY rule in the active session and rewrite its file. There is no undo."""
    if not force:
        click.echo(f"{ui.RED}✗ refusing without --force{ui.R} — this deletes every override in the "
                   f"active session and rewrites the file on disk.")
        raise SystemExit(1)
    result = api._control("/__mock__/overrides", "DELETE")
    click.echo(f"✓ cleared {result['cleared']} override(s) from {result['session']}")


@click.group()
def session() -> None:
    """Create and remove sessions."""


@session.command(name="new")
@click.argument("name")
@click.option("--clone-from", default=None, help="Start from a copy of this session.")
@click.option("--activate/--no-activate", default=True, help="Switch to it once created.")
def session_new(name: str, clone_from: str | None, activate: bool) -> None:
    """Create a session — use this for scratch work instead of editing a shared one."""
    api._control("/__mock__/sessions", "POST", {"name": name, "cloneFrom": clone_from})
    click.echo(f"✓ created {name}" + (f" from {clone_from}" if clone_from else ""))
    if activate:
        api._control("/__mock__/sessions/active", "PUT", {"name": name})
        click.echo(f"✓ active: {name}")


@session.command(name="rm")
@click.argument("name")
def session_rm(name: str) -> None:
    """Delete a session and its file."""
    api._control(f"/__mock__/sessions/{name}", "DELETE")
    click.echo(f"✓ deleted {name}")
