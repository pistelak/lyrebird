"""Terminal presentation — the colour constants, the intercept banner and the log tail.

Imports nothing of Lyrebird's beyond `config`, so every other module can call it.
"""

from __future__ import annotations

import click

import config

# MARK: - Presentation

R = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"


def _section(title: str, rows: list[tuple[str, str]]) -> None:
    """One labelled block of `explain-match` output, or nothing if it has no rows."""
    if not rows:
        return
    click.echo(f"  {title}:")
    for name, note in rows:
        click.echo(f"    {name:<16}{DIM}{note}{R}")


def _tail_log(lines: int) -> str:
    if not config.LOG_FILE.is_file():
        return "(no log)"
    return "\n".join(config.LOG_FILE.read_text(errors="replace").splitlines()[-lines:])


def _banner(health: dict | None, service: str | None, intercepting: bool) -> None:
    """Takes the PAC verdict rather than reading it, so the caller's one observation is the one
    shown."""
    if health is None:
        click.echo(f"{DIM}⚪ proxy not reachable{R}")
        return
    if intercepting:
        click.echo(
            f"{BOLD}{RED}🔴 INTERCEPT ACTIVE{R}  "
            f"session {BOLD}{health['activeSession']}{R} · "
            f"{health['overrideCount']} override(s) · proxy :{health['proxyPort']} · PAC on {service}"
        )
    else:
        click.echo(
            f"{BOLD}{YELLOW}🟠 PROXY UP BUT NOT INTERCEPTING{R} — PAC is disabled/not ours. "
            f"Run {BOLD}lyrebird up{R} to (re)install it."
        )
