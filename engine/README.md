# Lyrebird — engine

Transparent MITM mock proxy for iOS-simulator development. **No app rebuild** — routes the
simulator's traffic through a local [mitmproxy](https://mitmproxy.org) via a host-scoped PAC and a
CA trusted in the simulator, then applies per-endpoint overrides from a saved **session**.

One `mitmdump` process with an embedded control server does the work, plus a small watchdog that
restores your proxy settings if it dies. Rules live in memory, so a change made through the API
takes effect instantly. Only the hosts listed in your profile are routed through the proxy — all
other Mac traffic stays DIRECT.

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
└── sessions/*.json
```

Select one with `--profile PATH` (wins) or `LYREBIRD_PROFILE`; the default is
`~/.config/lyrebird` (honouring `XDG_CONFIG_HOME`). `lyrebird init PATH` creates one from
`examples/`.

`hosts` are **exact hostnames** — `api.example.com` does not imply `sub.api.example.com`. An empty
list means *intercept nothing*: the engine honours it, and `up` refuses to start on it, because
there would be nothing for `up` to achieve. A malformed profile aborts rather than falling back to
a default — when `up` or the proxy reads it. `down`, `status` and `logs` never parse the profile,
so a broken one cannot stop you restoring the network.

The profile directory may itself be a symlink, or live in a repository you point `--profile` at:
the path is resolved once at startup, and reaching the same directory through a link or by its real
name is the same profile. Inside it, what counts is where a path *resolves*, not whether it is a
link: a session file must resolve under the resolved `sessions/` directory *and* under the resolved
profile. A `sessions/` linked out of the profile therefore fails every write that touches a session
file — including `session new`, `session rm`, `override add`, `override clear`,
`DELETE /overrides/{id}` and `POST /sessions/import` — with `path escapes …`; a single file linked
out, or into a sibling directory inside the profile, fails only the writes to that session.

Reads are held to the same rule, so such a file is not loaded at all: startup skips it with
`path escapes …` in its load problems, and `lyrebird validate` names it. That is the point — a
session the proxy served but could never save was a scenario you could not edit and could not tell
apart from one that had loaded. Move the whole profile, not the sessions.

Sessions you save go into the profile — that is what it is for. Everything the tool
writes for its own purposes stays out, in the macOS directory that matches how long the file
deserves to live:

| | |
|---|---|
| `~/Library/Application Support/Lyrebird/` | must survive: the active-session pointer, per-port runtime and lock files, and the CA |
| `~/Library/Logs/Lyrebird/` | for a person to read: the proxy log — this is where the Console app looks |

Runtime files are keyed by **control port**, not profile, so `lyrebird down` finds the running
instance from any directory. `LYREBIRD_STATE_DIR` collapses both underneath one directory,
which is how the tests keep their writes in a temp tree and how you get a single thing to delete.

The active-session pointer and the log file are keyed by the **profile fingerprint** instead — the
first 12 hex characters of a sha256 of the resolved profile path — so relocating a profile selects
a different pointer and a different log. The state saved for the old path stays where it is, and at
a path with no pointer of its own the active session starts as `default`; a path used before is
remembered again. Nothing in the profile itself changes. The pointer is honoured only if the session
it names is still there, and each `up` that starts the proxy replaces the log at the selected path.

The split is not cosmetic. `tmutil isexcluded` reports Logs as excluded from Time Machine and
Application Support as included, so a log left in the wrong directory gets backed up forever to no
purpose. A profile, by contrast, *is* configuration — hand-edited and worth keeping in git — which
is why it lives in `~/.config` and neither of the above does.

## Use

```bash
../bin/lyrebird init ~/lyrebird-profiles/my-app
../bin/lyrebird --profile ~/lyrebird-profiles/my-app up --use orders-outage   # start, CA, PAC, scenario, app
../bin/lyrebird --profile ~/lyrebird-profiles/my-app status  # intercepting? which session? PAC state?
../bin/lyrebird --profile ~/lyrebird-profiles/my-app use another-scenario   # switch, from here on
../bin/lyrebird --profile ~/lyrebird-profiles/my-app down    # stop and restore previous settings
../bin/lyrebird logs                                          # last 60 lines; path on stderr
```

`up` prints a **🔴 INTERCEPT ACTIVE** banner and starts a watchdog that notices within a couple of
seconds if the proxy dies and makes a best-effort attempt to put your previous proxy settings
back, so a crash is unlikely to strand the Mac pointing at a dead port. There is no idle self-shutdown. After `up`, **relaunch the simulator app** (URLSession caches
the proxy config), or set `simBundleId` in the profile and Lyrebird relaunches it for you.

`up --use NAME` selects the session — and rewinds its sequences — *before* that relaunch, so the
app's launch requests are answered by the scenario you named rather than by whatever was last
active; `use NAME` on its own affects only the requests that follow it. `up` launches nothing at
all if `NAME` does not exist, did not load whole, or the running proxy is older than the CLI and
cannot say which of the two it is: it says so, exits 1, and leaves the proxy running for you to
`down`. That is the same verdict `lyrebird validate NAME` gives offline, arriving later and after
the network has been rewired, which is why validating first is worth the second. `--no-relaunch`
hands the launch to the caller — a UI-test runner that starts the app itself — and suppresses both
the relaunch and the reminder to do one by hand; it cannot be combined with `--relaunch BUNDLE`.

> **First run:** `up` generates Lyrebird's CA under
> `~/Library/Application Support/Lyrebird/mitmproxy/` and trusts it in the booted simulator.
> Boot the simulator first. Re-run `lyrebird trust-ca` after erasing one.

### Which simulator

`up` and `trust-ca` take `--simulator UDID-OR-NAME`, and that device gets the CA and the relaunch.
Without it, the single booted **iOS** simulator is used; with several booted and no choice made,
both commands **refuse and list them** rather than pass simctl's `booted` keyword, which — per
`simctl help` — "will choose one of them" without saying which. A UI-test runner that already
picked a device should pass the same UDID here. `status` reports the device the last `up` used,
in the text output and as `simulator` in `--json`, and `lyrebird relaunch [BUNDLEID]` relaunches
the app on it (`--simulator` overrides; that is the command the menu-bar app's Relaunch runs).

Candidates are booted iOS simulators simctl calls available, so a paired Apple Watch booting
alongside its phone does not make the choice ambiguous, and a lone booted watch is not selected
by default. Names must match in full (`iPhone 17 Pro`, not `iPhone 17`); UDIDs are matched
case-insensitively. A device that is absent, shut down, unavailable, or not an iOS simulator is
reported as such and the command exits non-zero — nothing falls back to another device.

**This is not traffic isolation.** The PAC is installed on a *network service* and scoped by
*hostname*, so every simulator on the Mac, and the Mac itself, routes the profile's hosts through
the proxy. `--simulator` decides where the CA is trusted and which app is relaunched, nothing
more. Devices that already trust the CA — including one an earlier `up` selected, because
switching devices does not untrust anything — go on receiving mocked responses; devices that
never trusted it fail TLS on those hosts instead.

## Ports

| Port | What | Env override |
|------|------|--------------|
| 8080 | mitmproxy (traffic) — the PAC sends configured hosts here | `LYREBIRD_PROXY_PORT` |
| 8088 | control API + `/proxy.pac` | `LYREBIRD_CONTROL_PORT` |

The control API always binds 127.0.0.1; it is unauthenticated, so that is not configurable. Use
`LYREBIRD_PROXY_LISTEN_HOST` / `LYREBIRD_PROXY_ADVERTISED_HOST` to change where the *proxy* binds
and what the PAC advertises — those are deliberately separate settings.

## Admin API (`/__mock__/*`)

- `GET /health` (reports `intercepting` / `proxyUp` / `pacEnabled` / `simBundleId` / `sessions` /
  `sequences` / `answers` / `loadProblems` / `sessionsNotWhole`, and `pacError` when the PAC could
  not be read — `intercepting` is then unproven, not off) · `GET /recent`
  - `loadProblems` is one string **per problem** found while loading the session files — a session
    with two invalid overrides yields two — as `"<file>.json: …"`, or `"skipped <file>.json: …"`
    when the whole file was rejected. `sessions` cannot carry any of it: a session whose invalid
    overrides were dropped, and a malformed `default.json` replaced by an empty in-memory
    `default`, are both listed there looking exactly like a session that loaded.
  - `sessionsNotWhole` is the same problems keyed by the session each belongs to
    (`{"orders-outage": ["orders-outage.json: …"]}`), which is what `up --use NAME` asks. The
    strings cannot answer it: a file named `orders-outage.json: backup.json` produces a
    `"skipped …"` line that begins exactly like a problem with `orders-outage`. A file whose
    *name* was rejected appears in `loadProblems` only — it could never have become a session.
- `POST /reset` — start a fresh run in the active session: rewind sequence cursors and clear answer
  counts, for every rule or one named with `{"id": ...}`. Returns `{"session": …, "reset": {id:
  runId}}` — the run id per rule is what binds a later assertion to this boundary
- `GET|POST /overrides`, `DELETE /overrides/{id}` — act on the **active session**
- `DELETE /overrides` — **destructive**: deletes every override in the active session and rewrites
  its file. The only endpoint that does this.
- `GET /sessions` · `POST /sessions` · `PUT /sessions/active`
  · `GET /sessions/{name}/export` · `POST /sessions/import` · `DELETE /sessions/{name}`

`POST /sessions/import` refuses (**400**) a payload it cannot keep whole — an override that fails
validation, or two sharing an id — and persists nothing. A session *file* is instead
reported-and-dropped at startup, because the file is in front of you and the proxy must still start;
an import is an API call, and reporting success for a rule that was discarded is worse than refusing.

A write whose session file resolves outside `sessions/` or outside the profile is refused with
**400** `{"error": "invalid_name", "detail": "path escapes …"}` before anything live changes —
[Profiles](#profiles) has the layouts that cause it. A name that could not be a path component is a
separate refusal, reported per endpoint: creating with one is the same **400** with a `detail` of
`invalid session name …`, while activating or deleting it is simply a session that is not there.

A `POST`, `PUT`, `PATCH` or `DELETE` carrying a body must send `Content-Type: application/json`,
and the `Host` header must be a loopback name with the control port. Cross-origin requests are
refused.

The CLI sends `X-Lyrebird-Profile: <fingerprint>` on every call, so a command run with `--profile B`
cannot read or change the profile A that actually holds the port: a mismatch is **409**
`{"error": "profile_mismatch", "running": …, "requested": …}`. Direct callers may omit the header
and are not checked. `GET /health` and `/proxy.pac` answer everyone — health is how a caller finds
out which profile is running (`down` needs that across profiles), and macOS fetches the PAC. Because
that reading is unscoped, a command that *interprets* it compares `profileFingerprint` itself: `up`
refuses, and `status` reports no interception and exits non-zero rather than presenting another
profile's proxy as this one's. The
check runs in the proxy, so a proxy started before this change enforces nothing until you restart it
(`lyrebird down && lyrebird up`).

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

Only `mode` is required. `id` is generated when omitted and `active` defaults to true, so a
hand-written session stays short — but an override with no `match` matches *every* request, so give
it at least a path.

An override supports these fields and no others — an unknown one is rejected rather than kept,
because a field the engine ignores is a rule that answers with a default instead of what its author
wrote (`statsu: 503` used to load cleanly and reply 200):

| field | |
|---|---|
| `id` | Stable name for the rule. Generated when omitted; derived from the rule's content for session files. |
| `active` | `false` switches the rule off without deleting it. Default true. |
| `match` | Which requests this rule answers (see the matcher fields). |
| `mode` | `replace` answers locally; `patch` merges into the real response. |
| `delayMs` | Delay the matched response by this many milliseconds. |
| `status` | HTTP status of the answer (replace) or forced onto the real response (patch). |
| `headers` | Response headers (replace). |
| `body` | Response body, JSON or string (replace). |
| `patch` | JSON deep-merged into the real response (patch). |
| `patchStrategy` | `appendToArray` appends to arrays instead of replacing them (patch). |
| `sequence` | Answer differently as a scenario progresses (replace only). |
| `notes` | Free text for the author. Ignored by the engine. |

`notes` is the only free-text field — JSON has no comments, and a rule usually needs a sentence
saying why it exists. At startup an override that carries an unknown field is reported and skipped,
naming the field; the rest of its session still loads. An import or `override add` is refused outright.

`match` supports these fields and no others — an unknown one is rejected rather than ignored,
because a typo'd field is not a stricter matcher but a missing constraint:

| field | |
|---|---|
| `method` | HTTP method, compared case-insensitively. Omit to match any method. |
| `path` | Path without the query string. `*` is the only wildcard; everything else is literal. |
| `query` | Query parameters that must all be present with these exact values. Others are ignored. |
| `bodyContains` | A substring that must appear in the request body. |

Both tables generate `lyrebird override add --help`, so the CLI can never advertise a different
vocabulary from the one validation accepts.

`delayMs` delays a matched response (that flow only). Most-specific wins: fewer wildcards first,
then a longer path, then more constraints — so a rule that also pins a query parameter beats a
generic rule on the same path. `lyrebird explain-match METHOD PATH` shows which rule a given request
would select and why each of the others would not, including the ones that matched and lost — which
is how you find out a rule is answering for sibling screens before it does it silently.

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
session, editing the rule, or `lyrebird reset`. `GET /__mock__/health` reports
`sequences[]` with `nextStep`, `stepCount`, `exhausted`, `hasOverrun`, `serves` (how many times
each step has been served this run, keyed by step number) and an opaque `runId` that changes on
every reset. `/recent` records `sequenceId`, `selectedStep` and `advanced` per request —
separately from `matched`, so an exhausted `passThrough` (which answers nothing) is still visible.

## Proving a rule was in play

`GET /__mock__/health` also reports `answers[]`: one `{id, active, count, runId}` per rule in the
active session, counting the requests it has answered since the last `POST /reset` and naming the
run they were counted in. The `runId` is the token `POST /reset` returned for that rule — the same
one `sequences[]` reports — not a second identity: one boundary, read by both views.

The three states a caller has to keep apart: no `runId` key means this engine cannot say which run
its counts belong to; `"runId": null` means the rule has no run state at all — never reset, never
near a request, or dropped by a session switch or a replacement under the same id; and a `runId`
matching the one you hold, with `count: 0`, means your run happened and nothing answered in it.
Only the last is evidence about the boundary you drew.

The count is taken where the answer is produced — when a `replace` writes its response, and when a
`patch` has actually merged into a JSON upstream — not when the request is recorded. That is what
makes it trustworthy as a test assertion: a rule that matched but lost to a more specific one is
never credited, a patch dropped for a non-JSON upstream is never credited, an exhausted
`passThrough` is never credited, and a `replace` whose flow dies on the way back to the client still
is, because it did answer. The evidence also outlives the request's entry in the bounded `/recent`
buffer.

`lyrebird assert-answered ID` exits non-zero unless that rule has answered since the reset, which is
what lets a UI test fail when its mock never applied — a negative assertion ("this section is not
shown") otherwise passes identically whether the override applied or never matched.

Rule ids are reused, runs are not. `lyrebird reset ID --json` hands back the run id, and
`assert-answered ID --run RUNID` requires the count to belong to it: a second reset, a rule replaced
under the same id, or a session switch between the action and the assertion starts a new run whose
count would otherwise be indistinguishable from the one the test earned. The run is re-checked on
every poll, so a boundary destroyed mid-wait — including the rule vanishing with a session switch —
fails the assertion instead of being waited out. With `--run`, exit 1 means the assertion was made
in your run and failed, and exit 3 that it could not be made: the run moved, the rule is gone, or
nothing could be read about it — an unreachable proxy, one too old to report runs, or another
profile's proxy holding the port, which is re-checked on every poll too. Without `--run` the
older, weaker reading applies — whichever run is current when the command looks — and every failure
is exit 1, as before.

Sequences are `replace`-only. A `patch` needs the upstream response, so it could not answer locally
when exhausted, and a patch skipped by a streamed or non-JSON upstream would spend a step the app
never saw.

## Sessions

Named, saveable scenarios under `<profile>/sessions/`. One active session drives the proxy. The
bundled examples are `enable-beta-export`, `orders-outage`, `slow-profile`, `checkout-edge-cases`,
`remove-item-then-refresh` and `retry-then-succeed` — the last two demonstrate the two sequence
triggers.

A session file carries `schemaVersion: 1`. It may be omitted, but any other value is refused whole:
a file written for a format this engine does not read would otherwise load with the wrong rules,
silently and only in the ways the format changed.

## Checking a session file without starting anything

Startup keeps the rules it can read and reports the rest, so a scenario can be live and quietly
missing the rule a test depends on. Two commands answer that from the file alone — no proxy, no
network change, nothing written to the profile:

```bash
lyrebird validate                     # every file in <profile>/sessions/
lyrebird validate orders-outage       # one of them
lyrebird validate --json              # machine-readable
```

```
✓ orders-outage            1 rule(s)
✗ partial                  1 rule(s) kept, 2 dropped
    partial.json: override[1]: match: unknown field 'paths' — a matcher may only carry method, path, query, bodyContains
    partial.json: override[2]: duplicate id 'ovr_ok' — ids must be unique within a session; this rule was skipped
✗ broken                   not loaded at all
    skipped broken.json: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)
```

Exit code 0 only when every session named loads whole. `--json` gives
`{"ok": bool, "problems": [], "sessions": [{"name", "file", "loaded", "ok", "overrideCount",
"problems": []}]}`, where `loaded` false means the file was refused entirely and `overrideCount` is
then `null` rather than `0` — a refused file has no rule count, and zero reads as a session that
loaded empty. The top-level `problems` holds what went wrong with the *request* rather than with a
file: a name that names nothing, a name that could not be one, a profile with no sessions in it.
Every failure comes back in this shape, so `--json` never has to be parsed as prose.

`explain-match --session NAME` runs the same ranking as the live command against a file:

```bash
lyrebird explain-match --session orders-outage GET '/api/v1/orders/42'
```

Output and `--json` shape match the live command, plus a `problems` list (present only with
`--session`, because only then is there a file whose load problems were actually read). It exits
non-zero if the file did not load whole even when a rule was selected. The proxy drops those rules
too, so the winner named is the one it would pick; what is not true is that the ranking covers the
rules in the file — and "my rule was never loaded" is the question this command gets run to answer.

Both read session files through `store.load_session_file`, the function `Store._load` loops over at
startup, and rank with `rules.find_override` — so the verdict is the engine's, not a second opinion
about it.

## Files

`../bin/lyrebird` (launcher) → `cli.py` (supervisor: CA + PAC + watchdog) · `addon.py` (mitmproxy
addon) · `rules.py` (match/patch/validate, unit-tested) · `control.py` (aiohttp API) ·
`store.py` · `netproxy.py` · `config.py` (paths, ports, host scoping) · `examples/`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Covers rule matching and merging, override validation, path containment, the control API's
Host/Origin/Content-Type guard, and host-scoping agreement between the addon, mitmproxy's
`allow_hosts` and the generated PAC.
