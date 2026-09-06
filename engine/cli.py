"""lyrebird — supervisor and macOS integration for the Lyrebird mock proxy.

    lyrebird init [PATH]                  create a profile from the bundled examples
    lyrebird up [--use <session>] [--no-relaunch] [--simulator <udid-or-name>]
                                          start proxy, trust CA, install PAC, select, relaunch
    lyrebird down                         stop proxy and restore the previous proxy settings
    lyrebird use <session>                switch active session (reports what it displaced)
    lyrebird recent [--json] [--matched]  what came through, and which overrides answered
    lyrebird override add <json>          add a rule to the active session, no restart
    lyrebird validate [session]           check saved session files offline; non-zero if any is
                                          not loadable whole
    lyrebird explain-match <method> <path>  which rule would be selected, and why the rest were not
                                          (--session NAME reads a file instead of the proxy)
    lyrebird assert-answered <id> [--run R]  exit non-zero unless that rule answered in that run
    lyrebird session new <name>           create a scratch session (--clone-from X)
    lyrebird reset [id] [--json]          start a fresh run: rewind sequences, clear answer counts
    lyrebird sequence wait <id> --step N  block until a sequence serves a given step
    lyrebird status [--json]              show intercept state (honest about PAC on/off)
    lyrebird wait-ready [--match]         block until traffic arrives, or until a rule matches
    lyrebird relaunch [BUNDLEID]          relaunch the app on the simulator this run is bound to
    lyrebird trust-ca [--simulator UDID]  trust the CA in a booted simulator (untrust-ca explains
                                          how to drop it again)
    lyrebird logs                         print the last 60 lines; path on stderr

Routing uses a *host-scoped PAC* so only the hosts in your profile go through the proxy; everything
else stays DIRECT. Whatever PAC you had before is recorded and put back on `down` — and by the
watchdog if the proxy dies, so a crash is unlikely to strand the Mac pointing at a dead port.
"""

from __future__ import annotations

import click

import config
import evidence
import offline
import scenario
import simulator as sim
import supervisor

# MARK: - CLI

@click.group()
@click.option("--profile", type=click.Path(), default=None,
              help="Profile directory (overrides $LYREBIRD_PROFILE).")
def cli(profile: str | None) -> None:
    if profile:
        # Paths only; `_require_profile` reads the contents. Not exported into os.environ: the two
        # child processes get it from `_child_env`, and a process-wide side effect from an
        # argument parser is what made an in-process test leak its profile into the next one.
        config.configure(profile)


# Every command and group lives in the module named for its concern; this is the only place that
# knows the whole set. `cli.py` stays the entry point `bin/lyrebird` and the watchdog spawn run by
# path, so what it registers is what `lyrebird --help` lists.
cli.add_command(supervisor.init)
cli.add_command(supervisor.up)
cli.add_command(supervisor.down)
cli.add_command(supervisor.status)
cli.add_command(scenario.use)
cli.add_command(scenario.recent)
cli.add_command(scenario.override)
cli.add_command(scenario.session)
cli.add_command(evidence.sequence)
cli.add_command(evidence.reset)
cli.add_command(evidence.assert_answered)
cli.add_command(offline.validate)
cli.add_command(offline.explain_match)
cli.add_command(evidence.wait_ready)
cli.add_command(sim.relaunch_cmd)
cli.add_command(sim.trust_ca_cmd)
cli.add_command(sim.untrust_ca_cmd)
cli.add_command(supervisor.logs)
cli.add_command(supervisor.watchdog)


if __name__ == "__main__":
    cli()
