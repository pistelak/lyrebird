"""Choosing what the proxy answers with: the active scenario, its rules, and the traffic it saw.

Every command here changes or reads the running proxy. `offline.py` is the half that inspects
files instead, and never changes anything.
"""

from __future__ import annotations

import json
import sys
import urllib.parse

import click

import api
import rules
import supervisor
import ui


@click.command()
@click.argument("name")
def use(name: str) -> None:
    """Switch the active scenario, for the requests that come after it.

    It does not relaunch anything, so an app that cached its launch response goes on showing the
    old scenario: relaunch it yourself, or start the run with `lyrebird up --use NAME`.
    """
    supervisor._activate_scenario(name)


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
            step = (
                f"  {ui.DIM}[{entry['sequenceId']} step {entry['selectedStep']}/{entry['stepCount']}]{ui.R}"
                if entry.get("selectedStep")
                else f"  {ui.YELLOW}[{entry['sequenceId']} overrun]{ui.R}"
            )
        advanced = f"  {ui.DIM}advanced {', '.join(entry['advanced'])}{ui.R}" if entry.get("advanced") else ""
        click.echo(f"  {entry['method']:6} {entry['status']}  {entry['path']}{mark}{step}{advanced}{skipped}")


@click.group()
def override() -> None:
    """Add or clear rules in the active scenario."""


# Built from the vocabulary itself, so the help cannot claim a different set of fields from the one
# validation accepts. A matcher field nobody can discover is reported as a missing feature — and the
# rule people write instead is a broader one that quietly answers for its neighbours.
_ADD_HELP = (
    """Add a rule to the active scenario. RULE is JSON, or - to read stdin.

Takes effect immediately — no restart, and the scenario file is updated.

\b
    lyrebird override add '{"match":{"path":"/api/v1/orders/*"},"mode":"replace","status":500}'

An override accepts these fields, and only these:

\b
"""
    + "\n".join(f"    {field:<13} {description}" for field, description in rules.OVERRIDE_FIELD_HELP.items())
    + """

`match` accepts these fields, and only these:

\b
"""
    + "\n".join(f"    {field:<13} {description}" for field, description in rules.MATCHER_FIELD_HELP.items())
    + """

Constrain a rule as tightly as the thing you are testing. Sibling screens served from one path are
told apart by `query`, and a rule that leaves it out answers for all of them:

\b
    lyrebird override add '{"match":{"method":"GET","path":"/api/items","query":{"kind":"alpha"}},
                            "mode":"replace","status":200,"body":{}}'

`lyrebird explain-match GET '/api/items?kind=alpha'` shows which rule a request would select, and
which others it would also have matched.
"""
)


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
    """Delete EVERY rule in the active scenario and rewrite its file. There is no undo."""
    if not force:
        click.echo(
            f"{ui.RED}✗ refusing without --force{ui.R} — this deletes every override in the "
            f"active scenario and rewrites the file on disk."
        )
        raise SystemExit(1)
    result = api._control("/__mock__/overrides", "DELETE")
    click.echo(f"✓ cleared {result['cleared']} override(s) from {result['scenario']}")


@click.group(name="scenario")
def scenario_group() -> None:
    """Create and remove scenarios."""


@scenario_group.command(name="new")
@click.argument("name")
@click.option("--clone-from", default=None, help="Start from a copy of this scenario.")
@click.option("--activate/--no-activate", default=True, help="Switch to it once created.")
def scenario_new(name: str, clone_from: str | None, activate: bool) -> None:
    """Create a scenario — use this for scratch work instead of editing a shared one."""
    api._control("/__mock__/scenarios", "POST", {"name": name, "cloneFrom": clone_from})
    click.echo(f"✓ created {name}" + (f" from {clone_from}" if clone_from else ""))
    if activate:
        api._control("/__mock__/scenarios/active", "PUT", {"name": name})
        click.echo(f"✓ active: {name}")


@scenario_group.command(name="rm")
@click.argument("name")
def scenario_rm(name: str) -> None:
    """Delete a scenario and its file."""
    # Encoded into the query, not interpolated into the path: `checkout/scratch` in the path matches
    # no route, and the CLI would report a scenario that is right there as one that is not — see
    # test_scenario_rm_sends_the_name_in_the_query_encoded.
    api._control(f"/__mock__/scenarios?name={urllib.parse.quote(name, safe='')}", "DELETE")
    click.echo(f"✓ deleted {name}")


@scenario_group.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def scenario_list(as_json: bool) -> None:
    """List the scenarios in this profile, by folder."""
    payload = api._control("/__mock__/scenarios") or {"active": "", "scenarios": []}
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    active = payload.get("active")
    scenarios = payload.get("scenarios") or []
    if not scenarios:
        click.echo(f"{ui.DIM}(no scenarios){ui.R}")
        return

    def row(scenario: dict, indent: str = "") -> None:
        mark = "*" if scenario["name"] == active else " "
        verified = f" {ui.GREEN}✓{ui.R}" if scenario.get("verified") else ""
        count = f"  {ui.DIM}{scenario.get('overrideCount', 0)} rules{ui.R}"
        # The full name, never the leaf: it is what `use`, `mv` and `rm` take, and a listing that
        # printed something else would be a listing nobody can copy from.
        click.echo(f" {mark} {indent}{scenario['name']}{verified}{count}")

    groups: dict[str, list[dict]] = {}
    for scenario in scenarios:
        groups.setdefault(scenario.get("group") or "", []).append(scenario)
    for scenario in groups.pop("", []):
        row(scenario)
    for group in sorted(groups):
        click.echo(f"   {ui.DIM}{group}/{ui.R}")
        for scenario in groups[group]:
            row(scenario, indent="  ")


@scenario_group.command(name="mv")
@click.argument("name")
@click.argument("to")
def scenario_mv(name: str, to: str) -> None:
    """Move a scenario to another name or folder, file and all."""
    api._control("/__mock__/scenarios/move", "POST", {"name": name, "to": to})
    click.echo(f"✓ moved {name} → {to}")


@scenario_group.command(name="reload")
@click.option("--use", "use", default=None, help="Activate this scenario as part of the reload.")
def scenario_reload(use: str | None) -> None:
    """Re-read the scenario files, picking up anything added or moved by hand.

    All or nothing: if any file cannot be read whole the proxy goes on serving what it already has,
    and the refusal names the files. Run evidence does not survive a reload — the scenarios are read
    fresh, so counts and cursors from before it belong to nothing.
    """
    result = api._control("/__mock__/scenarios/reload", "POST", {"use": use} if use else {}) or {}
    click.echo(f"✓ reloaded {result.get('reloaded', 0)} scenario(s), active: {result.get('active')}")
    click.echo(f"{ui.DIM}  run evidence was reset — `lyrebird reset` before asserting{ui.R}")
