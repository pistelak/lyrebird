"""Choosing what the proxy answers with: the active scenario, its rules, and the traffic it saw.

Every command here changes or reads the running proxy. `offline.py` is the half that inspects
files instead, and never changes anything.
"""

from __future__ import annotations

import json

import click

import api
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
def recent(as_json: bool) -> None:
    """What has come through the proxy, and which overrides answered it."""
    entries = api._control("/__mock__/recent") or []

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


@click.group(name="scenario")
def scenario_group() -> None:
    """Work with the scenario files this profile holds."""


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
