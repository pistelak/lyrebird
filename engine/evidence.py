"""Proving what the proxy did: run boundaries, sequence progress and answer counts."""

from __future__ import annotations

import json
import time

import click

import api
import ui


@click.group()
def sequence() -> None:
    """Inspect and rewind response sequences."""


def _find_sequence(health: dict, override_id: str) -> dict | None:
    for state in health.get("sequences", []):
        if state.get("id") == override_id:
            return state
    return None


@sequence.command(name="wait")
@click.argument("override_id")
@click.option("--step", type=int, required=True, help="Which step to wait for (1-based).")
@click.option("--timeout", default=30, help="Seconds to wait.")
def sequence_wait(override_id: str, step: int, timeout: int) -> None:
    """Block until a sequence serves a given step, in the run current when the wait began.

    Both timeout-shaped mistakes are answered up front instead: a step already served this run
    succeeds immediately (reset → trigger → wait is the documented order, and the action may land
    before the wait starts), and a run already past the step without serving it fails immediately —
    an agent that waits 30 seconds for either learns the wrong thing about why.

    The evidence that a step was served is the live serve counter; `/recent` only supplies the
    request details when it still has them, because it is a bounded window a serve can be evicted
    from while the fact that it happened is still true. The counter cannot be evicted and a reset
    drops it with the runtime entry it lives in, which is what makes it evidence about this run —
    see `store._rule_runtime`.
    """
    health = api._health()
    if health is None:
        # "Could not read it" is a different claim from "there is nothing here".
        click.echo(f"{ui.RED}✗ cannot reach the control API — is the proxy up?{ui.R}")
        raise SystemExit(1)
    api._require_same_profile(health)
    state = _find_sequence(health, override_id)
    if state is None:
        click.echo(f"{ui.RED}✗ no sequence '{override_id}' in the active session{ui.R}")
        raise SystemExit(1)
    if not 1 <= step <= state["stepCount"]:
        click.echo(f"{ui.RED}✗ '{override_id}' has {state['stepCount']} step(s); --step {step} is out of range{ui.R}")
        raise SystemExit(1)

    run_id, next_step = state["runId"], state["nextStep"]
    already = (state.get("serves") or {}).get(str(step), 0)
    if already:
        click.echo(
            f"✓ {override_id} served step {step}/{state['stepCount']} this run ({already}×, before the wait began)"
        )
        return

    # Fail now, not at the deadline. Ordered after the serve check: a cursor past the step with
    # no serve recorded means the run advanced over the step without ever serving it.
    if next_step is None or next_step > step:
        position = "exhausted" if next_step is None else f"now at step {next_step}"
        click.echo(
            f"{ui.RED}✗ '{override_id}' is already past step {step} ({position}) and never "
            f"served it this run.{ui.R}\n"
            f"   Run `lyrebird reset {override_id}` before triggering the action."
        )
        raise SystemExit(1)

    deadline = time.time() + timeout
    unreachable = False
    current: dict | None = None
    while True:
        current_health = api._health()
        if current_health is None:
            unreachable = True  # transient until the deadline says otherwise; keep polling
        else:
            unreachable = False
            # Re-checked: this wait's baseline is a run id from whichever store answered first.
            api._require_same_profile(current_health)
            current = _find_sequence(current_health, override_id)
            if current is None or current.get("runId") != run_id:
                what = "was removed" if current is None else "was reset"
                click.echo(
                    f"{ui.RED}✗ '{override_id}' {what} while waiting; this wait's baseline no longer applies.{ui.R}"
                )
                raise SystemExit(1)
            if (current.get("serves") or {}).get(str(step), 0):
                detail = next(
                    (
                        e
                        for e in api._get_json("/__mock__/recent", timeout=2) or []
                        if e.get("sequenceId") == override_id
                        and e.get("runId") == run_id
                        and e.get("selectedStep") == step
                    ),
                    None,
                )
                served = f"✓ {override_id} served step {step}/{current['stepCount']}"
                if detail:
                    click.echo(f"{served} for {detail['method']} {detail['path']} → {detail['status']}")
                else:
                    click.echo(served)  # the serve outlived its /recent entry
                return
            now_next = current.get("nextStep")
            if now_next is None or now_next > step:
                # Checked after the serve: a run that served the step and then advanced is a
                # success, but one that advanced over it without serving can never satisfy this
                # wait — burning the rest of the timeout would blame the wrong thing.
                position = "exhausted" if now_next is None else f"now at step {now_next}"
                click.echo(
                    f"{ui.RED}✗ '{override_id}' advanced past step {step} without serving it "
                    f"({position}).{ui.R}\n"
                    f"   Something advanced the sequence that was not the request you were "
                    f"waiting for; check `lyrebird recent`."
                )
                raise SystemExit(1)
        if time.time() >= deadline:
            break
        time.sleep(1)

    if unreachable:
        click.echo(
            f"{ui.RED}✗ lost the control API while waiting — the proxy may have stopped; "
            f"whether step {step} was served is unknown.{ui.R}"
        )
    else:
        click.echo(
            f"{ui.RED}✗ '{override_id}' did not serve step {step} within {timeout}s "
            f"(next step: {(current or state).get('nextStep')}).{ui.R}\n"
            f"   Check `lyrebird recent` for what did arrive."
        )
    raise SystemExit(1)


@click.command()
@click.argument("override_id", metavar="[ID]", required=False)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def reset(override_id: str | None, as_json: bool) -> None:
    """Start a fresh run: rewind sequences to step 1 and clear answer counts.

    With no ID, resets every rule in the active session. Run this immediately before the action you
    are about to test, not once at start-up — the app's launch fetches land in between, and evidence
    they leave behind would satisfy an `assert-answered` the test itself never earned.

    Every rule reset here gets a fresh run id, printed per rule and machine-readable under `--json`.
    Keep the one for the rule you are about to test and pass it to `assert-answered --run`: that is
    what binds the assertion to *this* boundary instead of to whichever run is current when it looks.

    Run state is in memory, so this is also the only way to replay a scenario without switching
    sessions.
    """
    result = api._control("/__mock__/reset", "POST", {"id": override_id} if override_id else {})
    reset_ids = result.get("reset") or {}
    if as_json:
        # A formatting flag decides how this is printed and nothing else: same call, same exit,
        # same meaning — including the empty case, which is a real answer ("no rules here").
        click.echo(json.dumps({"session": result.get("session"), "reset": reset_ids}, indent=2))
        return
    if not reset_ids:
        click.echo(f"{ui.DIM}no rules in '{result.get('session')}' — nothing to reset{ui.R}")
        return
    for name, run_id in reset_ids.items():
        click.echo(f"✓ reset {name} {ui.DIM}(run {run_id}){ui.R}")


# `assert-answered` distinguishes two failures that a harness must not confuse. Exit 1 is the
# assertion this command exists to make, failing: the rule answered nothing. Exit 3 is the
# assertion never being made at all — the run the caller named is not the one the proxy is
# reporting on, or nothing could be read about it. Evidence from another run is not weaker
# evidence; it is evidence about a different question, and "the mock did not apply" is the wrong
# thing to conclude from it. Click reserves 2 for its own usage errors, so the next code up is 3.
#
# Only `--run` can produce 3: without it the caller has asked the older, weaker question, and every
# failure answers it the way it always did.
_ANSWERED_NONE = 1
_ASSERTION_NOT_MADE = 3


def _require_run(override_id: str, state: dict, required: str) -> None:
    """Exits unless the rule's live run is the one the caller asked about.

    Missing identity is neither a mismatch nor a zero count, and gets the same distinct exit as a
    mismatch rather than the one for "it answered nothing": an engine older than the field cannot
    say which run its counts belong to, and a `runId` of `null` says the rule has no run at all —
    dropped by a session switch or by a replacement under the same id, or never made. Reporting
    either as a rule that answered nothing would send someone to debug a matcher that is fine.
    """
    if "runId" not in state:
        click.echo(
            f"{ui.RED}✗ this proxy does not report which run a count belongs to — restart it "
            f"(`lyrebird down && lyrebird up`) to pick up the current engine.{ui.R}\n"
            f"   Without --run the assertion reads whatever run is current instead."
        )
        raise SystemExit(_ASSERTION_NOT_MADE)
    current = state["runId"]
    if current == required:
        return
    reason = (
        f"is in run {current}"
        if current
        else "has no run at all — a session switch or a rule replaced under the same id dropped its run state"
    )
    click.echo(
        f"{ui.RED}✗ '{override_id}' {reason}, not run {required}.{ui.R}\n"
        f"   Whatever it has answered belongs to a different run than the one you set up, "
        f"so this assertion cannot be made.\n"
        f"   Reset, trigger the action, then assert — with the run id that reset printed."
    )
    raise SystemExit(_ASSERTION_NOT_MADE)


@click.command(name="assert-answered")
@click.argument("override_id")
@click.option(
    "--run",
    "required_run",
    default=None,
    metavar="ID",
    help="Require the answer to belong to this run id (printed by `lyrebird reset`).",
)
@click.option("--timeout", default=0, help="Seconds to wait for the first answer (0 checks now).")
def assert_answered(override_id: str, required_run: str | None, timeout: int) -> None:
    """Exit non-zero unless that rule has answered a request in the run you name.

    The assertion a test can make about its own setup. A negative UI assertion — "this section is
    not shown" — passes identically whether the mock applied or never matched, because the real
    backend usually produces the same screen. So a green suite can be testing nothing, and stays
    green when a rule quietly stops matching. This is the command that fails instead.

    The count is taken where the answer is produced, so it survives eviction from the traffic list
    and can never be satisfied by a rule that merely matched and lost. `lyrebird reset` draws the
    boundary: reset, trigger the action, assert.

    Pass `--run` with the id `reset` printed for the rule and the evidence must come from that
    boundary. Without it the assertion is the weaker one it has always been — "has this rule
    answered in whatever run is current right now" — and a second reset, a rule replaced under the
    same id, or a session switch between the action and the assertion starts a run whose count is
    not the one the test earned. Same-id, different run reads identically otherwise.

    \b
    Exit codes with --run — 1 means the assertion was made and failed, 3 that it could not be made:
      0  the rule answered, in the run you required
      1  the rule is in that run and has answered nothing (including: it is inactive)
      3  the run you named is not the rule's current run, the rule is not in the active session at
         all, or nothing could be read about it — an unreachable proxy, one too old to report
         runs, or another profile's proxy holding the port

    \b
    Without --run, every failure is 1.
    """
    if timeout < 0:
        click.echo(f"{ui.RED}✗ --timeout must not be negative{ui.R}")
        raise SystemExit(1)
    if required_run is not None and not required_run.strip():
        # Checked here rather than compared later: an empty --run would match a rule whose run is
        # unknown only by accident, and "no run id" must never be spelled the same way as one.
        click.echo(f"{ui.RED}✗ --run must name a run id (`lyrebird reset` prints one per rule){ui.R}")
        raise SystemExit(1)

    # Every way of learning nothing about the caller's run shares one code, so a harness has a
    # single branch for "re-establish the boundary" rather than a list of special cases. Without
    # --run there is no boundary to be unable to check, and each of these is 1.
    unproven = _ASSERTION_NOT_MADE if required_run else _ANSWERED_NONE

    deadline = time.time() + timeout
    while True:
        health = api._health()
        if health is None:
            # Distinct from "it answered nothing": we could not ask. Falling through to the
            # zero-answer message would send someone to debug a rule that may be perfectly fine.
            click.echo(f"{ui.RED}✗ cannot reach the control API — is the proxy up?{ui.R}")
            raise SystemExit(unproven)
        api._require_same_profile(health, unproven_exit=unproven)
        if "answers" not in health:
            click.echo(
                f"{ui.RED}✗ this proxy does not report answer counts — restart it "
                f"(`lyrebird down && lyrebird up`) to pick up the current engine.{ui.R}"
            )
            # An engine that cannot count answers certainly cannot say which run they are in, so
            # under --run this joins the other identity failures: a harness branching on 3 must not
            # have to learn that one flavour of version skew arrives as 1.
            raise SystemExit(unproven)

        state = next((s for s in health["answers"] if s.get("id") == override_id), None)
        if state is None:
            # A typo must not read as "it never fired": different bug, different fix.
            active = health.get("activeSession")
            click.echo(f"{ui.RED}✗ no rule '{override_id}' in session '{active}'{ui.R}")
            if required_run:
                # The rule can also vanish *during* the wait — a session switched to one that does
                # not carry this id — and that destroys the boundary rather than answering the
                # question about it. Same code as a run that moved, for the same reason.
                click.echo(f"   Run {required_run} cannot be checked: the rule is not there to have answered in it.")
            raise SystemExit(unproven)
        # Before the count, and re-checked every poll: a run that changes between the action and
        # the assertion — or while the assertion waits — makes the count that follows evidence
        # about a different run, and returning success on it is exactly the substitution this
        # option exists to refuse. The rule cannot come back to the run it left, so this is final.
        if required_run is not None:
            _require_run(override_id, state, required_run)
        count = state.get("count", 0)
        if count:
            where = f"in run {state['runId']}" if state.get("runId") else "this run"
            click.echo(f"✓ {override_id} answered {count} request(s) {where}")
            return
        if not state.get("active", True):
            # Waiting cannot help — matching skips a disabled rule entirely.
            click.echo(f"{ui.RED}✗ '{override_id}' is not active, so it can never answer.{ui.R}")
            raise SystemExit(_ANSWERED_NONE)
        # Without `--run`, a reset landing mid-wait is deliberately not special-cased: a count can
        # only be observed falling if it was seen above zero first, and a count above zero has
        # already returned. The wait simply runs out and says the rule has not answered this run,
        # which is true of the run it ends up looking at — which is the weakness `--run` removes.
        if time.time() >= deadline:
            break
        time.sleep(1)

    # The diagnostic read, and the last chance for the port to change hands: a 409 here must not
    # exit under the code that means "your run was checked and nothing answered".
    entries = api._get_json("/__mock__/recent", timeout=2, unproven_exit=unproven) or []
    waited = f" within {timeout}s" if timeout else ""
    scope = f"in run {required_run}" if required_run else "this run"
    click.echo(f"{ui.RED}✗ '{override_id}' has not answered any request {scope}{waited}.{ui.R}")
    if not entries:
        click.echo(
            "   Nothing has reached the proxy at all — relaunch the app, and check the host is listed in your profile."
        )
    else:
        # The distinct paths, not the count: a count cannot tell "the app went somewhere else"
        # from "the path pattern is wrong", and those have completely different fixes.
        seen = list(dict.fromkeys(f"{entry.get('method', '?'):6} {entry.get('path', '?')}" for entry in entries))
        click.echo(f"   {len(entries)} request(s) in the recent buffer, on these paths:")
        for line in seen[:5]:
            click.echo(f"     {ui.DIM}{line}{ui.R}")
        if len(seen) > 5:
            click.echo(f"     {ui.DIM}… and {len(seen) - 5} more{ui.R}")
        # The buffer is not scoped to the run, so these may predate the reset. Enough to tell
        # "the app is not reaching us" from "it is, on other paths"; not enough to blame the rule.
        click.echo(
            "   `lyrebird explain-match <method> <path>` says which rule one of those "
            "would select, and why yours was not it."
        )
    raise SystemExit(_ANSWERED_NONE)


@click.command(name="wait-ready")
@click.option("--timeout", default=30, help="Seconds to wait.")
@click.option(
    "--match", "want_match", is_flag=True, help="Wait for a request an override actually matched, not just any traffic."
)
def wait_ready(timeout: int, want_match: bool) -> None:
    """Block until the app's traffic reaches the proxy (avoids cold-launch flakiness).

    With --match, wait until an override actually fires. Traffic arriving proves the PAC works;
    it does not prove your rule matched, which is usually the thing you are waiting to confirm.
    """

    def newest(entries: list) -> str:
        return entries[0].get("time", "") if entries else ""

    # Only traffic that arrives from now on counts. Without this the retained buffer could
    # satisfy the wait instantly with a request made before the session was even switched.
    baseline = newest(api._get_json("/__mock__/recent", timeout=2) or [])
    deadline = time.time() + timeout
    while time.time() < deadline:
        recent = [e for e in (api._get_json("/__mock__/recent", timeout=2) or []) if e.get("time", "") > baseline]
        matched = [entry for entry in recent if entry.get("matched")]
        if matched if want_match else recent:
            if want_match:
                hit = matched[0]
                click.echo(f"✓ override {hit['matched']} matched {hit['method']} {hit['path']} → {hit['status']}")
            else:
                click.echo(f"✓ app is live ({len(recent)} proxied request(s) seen)")
            return
        time.sleep(1)
    if want_match:
        seen = len([e for e in (api._get_json("/__mock__/recent", timeout=2) or []) if e.get("time", "") > baseline])
        click.echo(
            f"{ui.RED}✗ no override matched within {timeout}s ({seen} request(s) reached the "
            f"proxy). Check the path in your rule against `lyrebird logs`.{ui.R}"
        )
    else:
        click.echo(
            f"{ui.RED}✗ no proxied requests within {timeout}s — is the app relaunched, and is it "
            f"calling a host listed in your profile?{ui.R}"
        )
    raise SystemExit(1)
