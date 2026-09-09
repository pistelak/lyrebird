"""Embedded control server: admin API + PAC file.

Runs on the mitmproxy asyncio loop (started from addon.running), so it shares the Store instance
with the interception hooks without any cross-process contract.

The API is unauthenticated but loopback-only, which alone is not enough: a web page you visit can
send cross-origin requests to 127.0.0.1, and a DNS-rebinding attack can make a hostile origin
*look* same-origin. Three cheap checks close that (see `_guard`): the Host header must be one of
ours, a cross-origin Origin is refused, and any mutating request with a body must declare JSON —
aiohttp's `request.json()` ignores Content-Type, so without that last check a `text/plain` form
post would reach the API with no CORS preflight.

`_guard` also refuses a request that names a profile other than the running one (409). That is not
a security check — it is scoping, and it stops a CLI pointed at profile B from quietly mutating
profile A, which holds the control port.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Handler

import config
import rules
from store import ReloadRefused, ScenarioRefused, Store, UnsafeName, scenario_parts

_CSP = "default-src 'none'; frame-ancestors 'none'"
_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# The header a caller uses to say which profile it means, and the two routes that must answer
# whoever asks. `/__mock__/health` is the discovery endpoint — it is how `down` finds out that some
# *other* profile's proxy holds the port, and that cross-profile recovery is the whole reason `down`
# works after a crash. `/proxy.pac` is fetched by macOS, which knows nothing about profiles.
_PROFILE_HEADER = "X-Lyrebird-Profile"
_UNSCOPED_PATHS = frozenset({"/__mock__/health", "/proxy.pac"})


def _allowed_hosts() -> set[str]:
    return {
        config.CONTROL_HOST_HEADER,
        f"localhost:{config.CONTROL_PORT}",
        f"[::1]:{config.CONTROL_PORT}",
    }


def _allowed_origins() -> set[str]:
    return {f"http://{host}" for host in _allowed_hosts()}


@web.middleware
async def _guard(request: web.Request, handler: Handler) -> web.StreamResponse:
    if (request.headers.get("Host") or "").lower() not in _allowed_hosts():
        return web.json_response({"error": "bad_host"}, status=421)

    origin = request.headers.get("Origin")
    if origin and origin.lower() not in _allowed_origins():
        return web.json_response({"error": "cross_origin_denied"}, status=403)

    # One proxy holds the control port, so a CLI invoked with `--profile B` while profile A is
    # running would otherwise switch A's scenario, add rules to A and reset A's counters — every
    # call reporting success for work done somewhere the operator was not looking. The header is a
    # scoping declaration, not a credential: it is absent from older CLIs and from curl, and those
    # stay unchecked. Fingerprints only — the profile path belongs to whoever wrote it.
    # `is not None`, not truthiness: an empty header is a caller that said *something* and named
    # nobody, and reading it as absent let `X-Lyrebird-Profile:` sail past the check.
    requested = request.headers.get(_PROFILE_HEADER)
    if requested is not None and request.path not in _UNSCOPED_PATHS and requested != config.PROFILE_FINGERPRINT:
        return web.json_response(
            {"error": "profile_mismatch", "running": config.PROFILE_FINGERPRINT, "requested": requested}, status=409
        )

    if request.method in _BODY_METHODS and request.can_read_body:
        content_type = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return web.json_response({"error": "json_content_type_required"}, status=415)

    try:
        response = await handler(request)
    except UnsafeName as error:
        return web.json_response({"error": "invalid_name", "detail": str(error)}, status=400)
    except ValueError as error:  # rules.ValidationError and friends
        return web.json_response({"error": "invalid_payload", "detail": str(error)}, status=400)
    except OSError as error:
        # The store could not write the profile — a full disk, a read-only profile. It publishes
        # nothing it could not write, so the caller's change simply did not happen and a retry is
        # safe. Named and detailed because aiohttp's own 500 is a bare text/plain body that sends
        # the operator to the proxy log instead of to the disk that is full.
        return web.json_response({"error": "persist_failed", "detail": str(error)}, status=500)

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


async def _safe_json(request: web.Request) -> dict:
    """Raises on a body that is present but unusable — `_guard` turns that into a 400.

    Returning {} instead would report the *next* problem it causes ("name_required") rather than
    the real one, sending the caller to look in the wrong place.
    """
    if not request.can_read_body:
        return {}
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise ValueError("malformed JSON body") from None
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    return payload


MetaProvider = Callable[[], Awaitable[dict[str, Any]]]


def make_app(store: Store, meta_provider: MetaProvider) -> web.Application:
    app = web.Application(middlewares=[_guard])
    routes = web.RouteTableDef()

    # MARK: - PAC

    @routes.get("/proxy.pac")
    async def pac(_request: web.Request) -> web.StreamResponse:
        return web.Response(text=config.pac_contents(), content_type="application/x-ns-proxy-autoconfig")

    # MARK: - Health / status

    @routes.get("/__mock__/health")
    async def health(_request: web.Request) -> web.StreamResponse:
        # Awaited *before* the store is read, not inline below. The provider observes the network
        # off this loop, so it suspends — and a scenario switch landing in that gap would pair
        # counters from the scenario before it with meta from after, a snapshot describing no
        # moment that ever existed. Everything after this line is synchronous.
        meta = await meta_provider()
        return web.json_response(
            {
                "ok": True,
                "activeScenario": store.active_name,
                "overrideCount": len(store.active_overrides()),
                "scenarios": list(store.scenarios.keys()),
                # What did *not* load, so a caller can tell "this scenario is here" from "this scenario
                # is here whole". A scenario whose invalid overrides were dropped, and a malformed
                # default.json replaced by an empty in-memory default, both appear in `scenarios`
                # looking exactly like a scenario that loaded — which is how `up --use NAME` would
                # relaunch the app against a scenario that had quietly lost half its rules.
                #
                # Two fields for one set of facts, because they answer different questions.
                # `loadProblems` is the flat list a person reads, one entry per problem. `up --use`
                # asks a narrower one — did *this* scenario load whole — and cannot answer it from
                # those strings: a file named `orders-outage.json: backup.json` produces a line that
                # reads exactly like a problem with `orders-outage`. So the same problems are also
                # sent keyed by the scenario they belong to.
                "loadProblems": store.load_problems,
                "scenariosNotWhole": store.scenarios_not_whole,
                "sequences": store.sequence_states(),
                # Named for what it holds, not for the objects it describes: `overrides` would read as
                # the rules themselves, which is what GET /overrides returns.
                "answers": store.answer_states(),
                **meta,
            }
        )

    @routes.get("/__mock__/rules")
    async def rules_snapshot(request: web.Request) -> web.StreamResponse:
        """The rules of one scenario, each with the engine's own description of what it answers with
        and the runtime state it has. It exists so a client can show rules without reimplementing
        which rule wins, which step is next or what a patch does.

        `?scenario=NAME` *browses* that scenario instead of the active one, which is what a sidebar
        does when the pointer moves down a list: looking is not switching, and a read that activated
        what it was asked about would repoint the running proxy at every scenario a user glanced at.
        `active` says which of the two a snapshot is, so a window can never present a scenario it is
        merely reading as the one answering requests.

        Nothing is awaited: unlike `health` this needs no meta, and the reads below — the rules,
        their answer counts, their cursors and the problems recorded against the scenario — would
        otherwise be split by a suspension, pairing one scenario's rules with another's counters. The
        handler stays synchronous and every row describes the same moment.
        """
        requested = request.query.get("scenario")
        if requested is not None:
            # Checked before the lookup below, which would report an empty name as an unknown
            # scenario — the wrong problem: `?scenario=` is a client that meant to name one and
            # sent nothing, not a client asking after a scenario called "".
            if not requested:
                return web.json_response({"error": "name_required"}, status=400)
            # Validated here rather than left to the membership test: this handler looks the
            # scenario up itself, so without it a name that could never be one — `../x`, three
            # levels deep — reads as a scenario somebody deleted. Every other route validates
            # inside the store method it calls.
            scenario_parts(requested)
            if requested not in store.scenarios:
                # `detail` as well as the slug, the same shape activating an unknown scenario uses.
                return web.json_response(
                    {
                        "error": "unknown_scenario",
                        "name": requested,
                        "detail": f"no scenario named '{requested}' in this profile",
                    },
                    status=404,
                )

        # The active read is also the parameterless one, and it is not routed through a membership
        # test: a store whose active scenario has gone heals itself inside `active_overrides`, and a
        # lookup here would 404 that recovery instead of performing it.
        if requested is None or requested == store.active_name:
            active = True
            # Sequences first: `sequence_states` mints a rule's runtime slot where `answer_states`
            # only reads one, so reading answers first left the very first snapshot after activation
            # showing a null `answer.runId` beside a live `sequenceState.runId` for one rule — two
            # run ids for one run. See test_a_fresh_snapshot_gives_a_sequenced_rule_one_run_id.
            sequences: dict[str, dict] = {state["id"]: state for state in store.sequence_states()}
            answers: dict[str, dict] = {state["id"]: state for state in store.answer_states()}
            # Read after the healing `active_overrides` may have done, so the name reported is the
            # scenario these rules actually came from.
            overrides = store.active_overrides()
            name = store.active_name
        else:
            # Null runtime, not zeroed. Cursors and answer counts belong to the scenario the proxy
            # is serving; a browsed one has no run, and reporting `count: 0` would say it answered
            # nothing when the truth is that nothing has asked it to.
            active = False
            name, overrides = requested, list(store.scenarios[requested].get("overrides") or [])
            sequences, answers = {}, {}

        def advance_on_rule(matcher: dict | None) -> str | None:
            """The id of the rule whose `match` is this trigger, or None when no rule's is.

            Here because it needs the whole scenario, which `describe_rewrite` does not receive.
            Inactive rules count and the first in scenario order wins: this is trigger identity,
            not which rule would answer.
            """
            if matcher is None:
                return None  # `self`: no request advances it, so there is no rule to name
            return next((o["id"] for o in overrides if rules.same_matcher(o.get("match"), matcher)), None)

        def described(override: dict) -> dict:
            rewrite = rules.describe_rewrite(override)
            if rewrite["sequence"] is not None:
                rewrite["sequence"]["advanceOnRule"] = advance_on_rule(rewrite["sequence"]["advanceOn"])
            return rewrite

        return web.json_response(
            {
                "scenario": name,
                "active": active,
                # From the keyed map, never by filtering `load_problems`: a file named
                # `orders-outage.json: backup.json` leaves a line that begins exactly like a problem
                # with `orders-outage`, and a client would blame a scenario that loaded whole.
                "notWhole": list(store.scenarios_not_whole.get(name, [])),
                "rules": [
                    {
                        **override,
                        "rewrite": described(override),
                        "answer": answers.get(override["id"]),
                        # Not "sequence": that key already holds the rule's steps as written, and
                        # overwriting it with the cursor would hand back a payload that claims to
                        # carry the rule as stored while having dropped half of it.
                        "sequenceState": sequences.get(override["id"]),
                    }
                    for override in overrides
                ],
            }
        )

    @routes.get("/__mock__/recent")
    async def recent(_request: web.Request) -> web.StreamResponse:
        return web.json_response(store.recent_list())

    @routes.delete("/__mock__/recent")
    async def clear_recent(_request: web.Request) -> web.StreamResponse:
        store.clear_recent()
        return web.json_response({"ok": True})

    # MARK: - Overrides (act on the active scenario)

    @routes.get("/__mock__/overrides")
    async def overrides_list(_request: web.Request) -> web.StreamResponse:
        return web.json_response(store.active_overrides())

    @routes.post("/__mock__/overrides")
    async def overrides_add(request: web.Request) -> web.StreamResponse:
        override = store.add_override(await _safe_json(request))
        return web.json_response({"id": override["id"], "active": override["active"]})

    @routes.delete("/__mock__/overrides")
    async def overrides_clear(_request: web.Request) -> web.StreamResponse:
        """Deletes every override in the active scenario and rewrites its file."""
        cleared = len(store.active_overrides())
        store.clear_overrides()
        return web.json_response({"cleared": cleared, "scenario": store.active_name})

    # MARK: - Run state

    @routes.post("/__mock__/reset")
    async def runtime_reset(request: web.Request) -> web.StreamResponse:
        """Start a fresh run in the active scenario: rewind sequence cursors and clear answer counts.

        No body resets every rule; {"id": ...} resets one.
        """
        body = await _safe_json(request)
        override_id = body.get("id")
        if override_id is not None and (not isinstance(override_id, str) or not override_id):
            # Checked here rather than left to the store: a non-string would reach a dict lookup and
            # raise TypeError, which `_guard` does not translate — it catches ValueError — so a
            # caller sending {"id": []} would get a 500 describing nothing.
            return web.json_response({"error": "id_must_be_a_non_empty_string"}, status=400)
        result = store.reset_runtime(override_id)
        if result is None:
            return web.json_response({"error": "unknown_override", "id": override_id}, status=404)
        return web.json_response(result)

    # MARK: - Scenarios

    @routes.get("/__mock__/scenarios")
    async def scenarios_list(_request: web.Request) -> web.StreamResponse:
        return web.json_response(store.list_scenarios())

    @routes.post("/__mock__/scenarios")
    async def scenarios_create(request: web.Request) -> web.StreamResponse:
        body = await _safe_json(request)
        if not body.get("name"):
            return web.json_response({"error": "name_required"}, status=400)
        try:
            store.create_scenario(body["name"], body.get("cloneFrom"))
        except KeyError as error:
            return web.json_response({"error": "unknown_scenario", "cloneFrom": str(error)}, status=404)
        except FileExistsError:
            return web.json_response({"error": "scenario_exists", "name": body["name"]}, status=409)
        return web.json_response({"created": body["name"]})

    @routes.put("/__mock__/scenarios/active")
    async def scenarios_activate(request: web.Request) -> web.StreamResponse:
        body = await _safe_json(request)
        name = body.get("name")
        if not isinstance(name, str) or not name:
            return web.json_response({"error": "name_required"}, status=400)
        previous = store.set_active(name)
        if previous is None:
            # `detail` as well as the slug: the CLI prints `detail` when there is one, and
            # "unknown_scenario" on its own names the category without naming the mistake.
            return web.json_response(
                {"error": "unknown_scenario", "name": name, "detail": f"no scenario named '{name}' in this profile"},
                status=404,
            )
        return web.json_response({"active": store.active_name, "previous": previous})

    def _delete(name: str) -> web.StreamResponse:
        """One implementation for both delete routes, so they cannot answer differently."""
        if not store.delete_scenario(name):
            return web.json_response({"error": "cannot_delete", "name": name}, status=400)
        return web.json_response({"deleted": name})

    @routes.delete("/__mock__/scenarios")
    async def scenarios_delete_by_query(request: web.Request) -> web.StreamResponse:
        # A grouped name contains `/`, which no path parameter can carry: `.../scenarios/checkout/x`
        # matches no route at all, so the CLI would report "not found" for a scenario that is right
        # there. The query carries it percent-encoded instead.
        name = request.query.get("name")
        if not name:
            return web.json_response({"error": "name_required"}, status=400)
        return _delete(name)

    @routes.delete("/__mock__/scenarios/{name}")
    async def scenarios_delete(request: web.Request) -> web.StreamResponse:
        # Kept for clients that predate the query form; a root name still works through it.
        return _delete(request.match_info["name"])

    @routes.post("/__mock__/scenarios/move")
    async def scenarios_move(request: web.Request) -> web.StreamResponse:
        body = await _safe_json(request)
        name, to = body.get("name"), body.get("to")
        if not isinstance(name, str) or not name:
            return web.json_response({"error": "name_required"}, status=400)
        if not isinstance(to, str) or not to:
            return web.json_response({"error": "to_required"}, status=400)
        try:
            store.move_scenario(name, to)
        except KeyError:
            return web.json_response(
                {"error": "unknown_scenario", "name": name, "detail": f"no scenario named '{name}' in this profile"},
                status=404,
            )
        except FileExistsError as error:
            return web.json_response({"error": "scenario_exists", "name": str(error)}, status=409)
        except ScenarioRefused as error:
            return web.json_response({"error": "cannot_move", "detail": str(error)}, status=409)
        return web.json_response({"moved": name, "to": to})

    @routes.post("/__mock__/scenarios/reload")
    async def scenarios_reload(request: web.Request) -> web.StreamResponse:
        body = await _safe_json(request)
        use = body.get("use")
        if use is not None and (not isinstance(use, str) or not use):
            return web.json_response({"error": "name_required"}, status=400)
        try:
            return web.json_response(store.reload_scenarios(use))
        except ReloadRefused as error:
            # 409 with the problems listed: the profile on disk is not one this engine will serve,
            # and the caller has to fix a file. `detail` is what the CLI prints.
            return web.json_response(
                {"error": "reload_refused", "detail": "; ".join(error.problems), "problems": error.problems},
                status=409,
            )

    app.add_routes(routes)
    return app


async def start(store: Store, meta_provider: MetaProvider) -> web.AppRunner:
    app = make_app(store, meta_provider)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.CONTROL_HOST, config.CONTROL_PORT)
    await site.start()
    return runner
