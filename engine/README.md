# Lyrebird — engine

Transparent MITM mock proxy for iOS-simulator development. **No app rebuild** — routes the
simulator's traffic through a local [mitmproxy](https://mitmproxy.org) via a host-scoped PAC and a
CA trusted in the simulator, then applies per-endpoint overrides from a saved **session**.

One `mitmdump` process with an embedded control server does the work, plus a small watchdog that
restores your proxy settings if it dies. Rules live in memory, so a change made in the dashboard
or API takes effect instantly. Only the hosts listed in
your profile are routed through the proxy — all other Mac traffic stays DIRECT.

## Install (once)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # mitmproxy, aiohttp, click (pinned)
.venv/bin/pip install -r requirements-dev.txt    # pytest, ruff, mypy
```

## Profiles

A **profile** is a directory holding everything specific to your API:

```
my-app/
├── profile.json     # { "schemaVersion": 1, "hosts": [...], "simBundleId": "com.example.Store" }
├── sessions/*.json
└── presets/
```

Select one with `--profile PATH` (wins) or `LYREBIRD_PROFILE`; the default is
`~/.config/lyrebird` (honouring `XDG_CONFIG_HOME`). `lyrebird init PATH` creates one from
`examples/`.

`hosts` are **exact hostnames** — `api.example.com` does not imply `sub.api.example.com`. An empty
list means *intercept nothing*, and a malformed profile aborts rather than falling back to a
default.

Sessions and presets you save go into the profile — that is what it is for. Everything the tool
writes for its own purposes stays out, in the macOS directory that matches how long the file
deserves to live:

| | |
|---|---|
| `~/Library/Application Support/Lyrebird/` | must survive: the active-session pointer, per-port runtime and lock files, and the CA |
| `~/Library/Caches/com.lyrebird.Lyrebird/` | may be discarded: the generated endpoint catalog |
| `~/Library/Logs/Lyrebird/` | for a person to read: the proxy log — this is where the Console app looks |

Runtime files are keyed by **control port**, not profile, so `lyrebird down` finds the running
instance from any directory. `LYREBIRD_STATE_DIR` collapses all three underneath one directory,
which is how the tests keep their writes in a temp tree and how you get a single thing to delete.

The split is not cosmetic. `tmutil isexcluded` reports Caches and Logs as excluded from Time
Machine and Application Support as included, so a regenerable catalog left in the wrong directory
gets backed up forever to no purpose. A profile, by contrast, *is* configuration — hand-edited and
worth keeping in git — which is why it lives in `~/.config` and none of the above does.

## Use

```bash
../bin/lyrebird init ~/lyrebird-profiles/my-app
../bin/lyrebird --profile ~/lyrebird-profiles/my-app up      # start, trust CA, install PAC
../bin/lyrebird --profile ~/lyrebird-profiles/my-app status  # intercepting? which session? PAC state?
../bin/lyrebird --profile ~/lyrebird-profiles/my-app use orders-outage
../bin/lyrebird --profile ~/lyrebird-profiles/my-app down    # stop and restore previous settings
../bin/lyrebird logs                                          # last 60 lines; path on stderr
```

`up` prints a **🔴 INTERCEPT ACTIVE** banner and starts a watchdog that notices within a couple of
seconds if the proxy dies and makes a best-effort attempt to put your previous proxy settings
back, so a crash is unlikely to strand the Mac pointing at a dead port. There is no idle self-shutdown. After `up`, **relaunch the simulator app** (URLSession caches
the proxy config), or set `simBundleId` in the profile and Lyrebird relaunches it for you.

Dashboard: <http://127.0.0.1:8088/>. Everything the dashboard does is on the admin API below.

> **First run:** `up` generates Lyrebird's CA under
> `~/Library/Application Support/Lyrebird/mitmproxy/` and trusts it in the **booted** simulator.
> Boot the simulator first. Re-run `lyrebird trust-ca` after erasing one.

## Ports

| Port | What | Env override |
|------|------|--------------|
| 8080 | mitmproxy (traffic) — the PAC sends configured hosts here | `LYREBIRD_PROXY_PORT` |
| 8088 | control API + dashboard + `/proxy.pac` | `LYREBIRD_CONTROL_PORT` |

The control API always binds 127.0.0.1; it is unauthenticated, so that is not configurable. Use
`LYREBIRD_PROXY_LISTEN_HOST` / `LYREBIRD_PROXY_ADVERTISED_HOST` to change where the *proxy* binds
and what the PAC advertises — those are deliberately separate settings.

## Admin API (`/__mock__/*`)

- `GET /health` (reports `intercepting` / `proxyUp` / `pacEnabled` / `simBundleId` / `sequences`)
  · `GET /recent` · `GET /catalog`
- `POST /sequences/reset` — rewind every sequence in the active session, or one with `{"id": ...}`
- `GET|POST /overrides`, `DELETE /overrides/{id}` — act on the **active session**
- `DELETE /overrides` — **destructive**: deletes every override in the active session and rewrites
  its file. The only endpoint that does this; the dashboard button asks first.
- `GET /sessions` · `POST /sessions` · `POST /sessions/save-active` · `PUT /sessions/active`
  · `GET /sessions/{name}/export` · `POST /sessions/import` · `DELETE /sessions/{name}`

`POST /sessions/import` refuses (**400**) a payload it cannot keep whole — an override that fails
validation, or two sharing an id — and persists nothing. A session *file* is instead
reported-and-dropped at startup, because the file is in front of you and the proxy must still start;
an import is an API call, and reporting success for a rule that was discarded is worse than refusing.
- `GET /presets/{operationId}` · `GET|POST /presets/{operationId}/{name}`

A `POST`, `PUT`, `PATCH` or `DELETE` carrying a body must send `Content-Type: application/json`,
and the `Host` header must be a loopback name with the control port. Cross-origin requests are
refused. `GET /status` is an alias of `GET /health`.

### Override shape

```json
{ "match": { "method": "GET", "path": "/api/v1/features" },
  "mode": "patch",
  "patch": { "features": [ { "id": "BETA_EXPORT", "enabled": true } ] },
  "patchStrategy": "appendToArray" }
```

- `replace` — return a canned `{status, headers, body}`. Short-circuits before the upstream, so it
  works even when the real API is unreachable and needs no auth.
- `patch` — proxy upstream, then JSON deep-merge into the **real** response. Needs a buffered
  upstream response whose content-type says JSON and whose body parses as JSON; streamed, oversized
  and non-JSON responses are passed through untouched and reported as `patchSkipped`.

Only `mode` is required. `id` is derived from the rule when omitted and `active` defaults to true,
so a hand-written session stays short — but an override with no `match` matches *every* request,
so give it at least a path.

`match` supports `{ method, path (with * wildcards), query?, bodyContains? }`. `delayMs` delays a
matched response (that flow only). Most-specific wins: fewer wildcards first, then a longer path,
then more constraints — so a rule that also pins a query parameter beats a generic rule on the
same path.

**`patchStrategy: "appendToArray"`** changes array handling for the *whole* recursive patch:
wherever the upstream value and the patch value at the same JSON path are both arrays, Lyrebird
returns the upstream items followed by the patch items. It is not scoped to one named array, and it
does not deduplicate. Without it, a patch array *replaces* the upstream array.

`patch` also works with no arrays at all:

```json
{ "match": { "method": "GET", "path": "/api/v1/users/me" },
  "mode": "patch",
  "patch": { "preferences": { "theme": "dark" } } }
```

Server-sent-event streams always pass through un-buffered, and bodies larger than 512 KB stream
rather than buffer. When a patch cannot be applied, the reason appears as `patchSkipped` in
`/recent` instead of the request silently looking unmatched.

## Sequences

A `replace` rule may answer differently as a scenario progresses. It holds a list of **steps** and a
cursor; `advanceOn` decides what moves the cursor.

```json
{ "match": { "method": "GET", "path": "/api/v1/items" },
  "mode": "replace",
  "sequence": {
    "advanceOn": { "method": "DELETE", "path": "/api/v1/items/*" },
    "steps": [ { "body": { "items": ["a", "b", "c"] } },
               { "body": { "items": ["a", "c"] } } ] } }
```

Omit `advanceOn` and it defaults to **`self`** — the cursor moves each time this rule answers, which
is what you want for "the first attempt fails, the second succeeds". Give it a matcher and the rule
becomes **idempotent**: repeated calls all return the current step, and only a request matching that
matcher moves it on. That is what makes delete-then-refresh reliable, because a screen that fetches
its list twice on appear no longer desynchronises the scenario.

`self` is not the same as a matcher copied from `match`. Matching order picks the *most specific*
rule, so a rule whose matcher fits a request may not be the rule that answered it — advancing on a
match it lost would spend a step it never served, and every later request would be off by one.

`advanceOn` takes the same vocabulary as `match` and is validated by the same code, but it must
constrain at least a `method` or a `path`: a matcher with neither matches everything, so every
request would advance the sequence.

**A step is the response half of an override** — `status`, `headers`, `body`, and nothing else.
Fields not set on a step are inherited from the rule, as a shallow overlay: a step's `headers`
*replaces* the rule's rather than merging, `{}` clears them, and an explicit `null` clears any
inherited field. `delayMs` belongs on the rule, not the step, so one delay applies to every step.

| `onExhausted` | after the last step |
|---|---|
| `error` *(default)* | Lyrebird answers `500` naming the rule and the counts |
| `repeatLast` | the last step answers again |
| `passThrough` | the rule stands aside and the real upstream answers |

The default is `error` on purpose: repeating the last step would let a request the scenario never
planned for pass for a successful one.

Cursors live in memory, never in the profile, and reset when the scenario restarts — switching
session, editing the rule, or `lyrebird sequence reset`. `GET /__mock__/health` reports
`sequences[]` with `nextStep`, `stepCount`, `exhausted`, `hasOverrun`, `serves` (how many times
each step has been served this run, keyed by step number) and an opaque `runId` that changes on
every reset. `/recent` records `sequenceId`, `selectedStep` and `advanced` per request —
separately from `matched`, so an exhausted `passThrough` (which answers nothing) is still visible.

Sequences are `replace`-only. A `patch` needs the upstream response, so it could not answer locally
when exhausted, and a patch skipped by a streamed or non-JSON upstream would spend a step the app
never saw.

## Sessions

Named, saveable scenarios under `<profile>/sessions/`. One active session drives the proxy. The
bundled examples are `enable-beta-export`, `orders-outage`, `slow-profile`, `checkout-edge-cases`,
`remove-item-then-refresh` and `retry-then-succeed` — the last two demonstrate the two sequence
triggers.

## Endpoint catalog

```bash
.venv/bin/pip install pyyaml                               # --spec only; not a base dependency
.venv/bin/python catalog.py --spec /path/to/openapi.yaml   # group by OpenAPI tag
.venv/bin/python catalog.py --from-recent                  # derive from observed traffic
```

`--spec` is the one thing here that needs PyYAML, and reading an OpenAPI file is not why most
people install this, so it stays out of `requirements.txt` rather than being pulled in by everyone.
Run `--spec` without it and you are told exactly that. `--from-recent` needs nothing extra.

Written to the cache directory, not the profile — it is a derived copy of your API's structure.
Served at `GET /__mock__/catalog`; the bundled dashboard does not consume it yet.

## Files

`../bin/lyrebird` (launcher) → `cli.py` (supervisor: CA + PAC + watchdog) · `addon.py` (mitmproxy
addon) · `rules.py` (match/patch/validate, unit-tested) · `control.py` (aiohttp API + dashboard) ·
`store.py` · `catalog.py` · `netproxy.py` · `config.py` (paths, ports, host scoping) · `web/`
(dashboard) · `examples/`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Covers rule matching and merging, override validation, path containment, the control API's
Host/Origin/Content-Type guard, and host-scoping agreement between the addon, mitmproxy's
`allow_hosts` and the generated PAC.
