# Lyrebird — engine

Transparent MITM mock proxy for iOS-simulator development. **No app rebuild** — routes the
simulator's traffic through a local [mitmproxy](https://mitmproxy.org) via a host-scoped PAC and a
CA trusted in the simulator, then applies per-endpoint overrides from a saved **scenario**.

One `mitmdump` process with an embedded control server does the work, plus a small watchdog that
restores your proxy settings if it dies. Rules live in memory, so a change made through the API
takes effect instantly. Only the hosts listed in your profile are routed through the proxy — all
other Mac traffic stays DIRECT.

## Install (once)

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt  # locked runtime dependencies
```

For development, run `make setup` and `make check` from the repository root; see
[CONTRIBUTING.md](../CONTRIBUTING.md) for formatting, tests and dependency updates.

## Profiles

A **profile** is a directory holding everything specific to your API:

```
my-app/
├── profile.json     # { "schemaVersion": 1, "hosts": [...], "simBundleId": "com.example.Store" }
└── scenarios/*.json
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
link: a scenario file must resolve under the resolved `scenarios/` directory *and* under the
resolved profile. A `scenarios/` linked out of the profile therefore fails every write that touches
a scenario file — including `scenario new`, `scenario rm`, `override add` and `override clear` —
with `path escapes …`; a single file linked out, or into a sibling directory inside the profile,
fails only the writes to that scenario.

Reads are held to the same rule, so such a file is not loaded at all: startup skips it with
`path escapes …` in its load problems, and `lyrebird validate` names it. That is the point — a
scenario the proxy served but could never save was a scenario you could not edit and could not tell
apart from one that had loaded. Move the whole profile, not the scenarios.

Scenarios you save go into the profile — that is what it is for. Everything the tool
writes for its own purposes stays out, in the macOS directory that matches how long the file
deserves to live:

| | |
|---|---|
| `~/Library/Application Support/Lyrebird/` | must survive: the active-scenario pointer, per-port runtime and lock files, and the CA |
| `~/Library/Logs/Lyrebird/` | for a person to read: the proxy log — this is where the Console app looks |

Runtime files are keyed by **control port**, not profile, so `lyrebird down` finds the running
instance from any directory. `LYREBIRD_STATE_DIR` collapses both underneath one directory,
which is how the tests keep their writes in a temp tree and how you get a single thing to delete.

The active-scenario pointer and the log file are keyed by the **profile fingerprint** instead — the
first 12 hex characters of a sha256 of the resolved profile path — so relocating a profile selects
a different pointer and a different log. The state saved for the old path stays where it is, and at
a path with no pointer of its own the active scenario starts as `default`; a path used before is
remembered again. Nothing in the profile itself changes. The pointer is honoured only if the
scenario it names is still there, and each `up` that starts the proxy replaces the log at the
selected path.

The split is not cosmetic. `tmutil isexcluded` reports Logs as excluded from Time Machine and
Application Support as included, so a log left in the wrong directory gets backed up forever to no
purpose. A profile, by contrast, *is* configuration — hand-edited and worth keeping in git — which
is why it lives in `~/.config` and neither of the above does.

## Use

```bash
../bin/lyrebird init ~/lyrebird-profiles/my-app
../bin/lyrebird --profile ~/lyrebird-profiles/my-app up --use orders-outage   # start, CA, PAC, scenario, app
../bin/lyrebird --profile ~/lyrebird-profiles/my-app status  # intercepting? which scenario? PAC state?
../bin/lyrebird --profile ~/lyrebird-profiles/my-app use another-scenario   # switch, from here on
../bin/lyrebird --profile ~/lyrebird-profiles/my-app down    # stop and restore previous settings
../bin/lyrebird logs                                          # last 60 lines; path on stderr
```

`up` prints a **🔴 INTERCEPT ACTIVE** banner and starts a watchdog that notices within a couple of
seconds if the proxy dies and makes a best-effort attempt to put your previous proxy settings
back, so a crash is unlikely to strand the Mac pointing at a dead port. There is no idle
self-shutdown.

After `up`, **relaunch the simulator app** — URLSession caches the proxy config it saw at launch —
or set `simBundleId` in the profile and Lyrebird relaunches it for you. `up --use NAME` selects the
scenario and rewinds its sequences *before* that relaunch, and launches nothing at all if `NAME`
does not exist, did not load whole, or the running proxy is too old to say which of the two it is:
it says so, exits 1, and leaves the proxy running for you to `down`. `--no-relaunch` hands the
launch to the caller — a UI-test runner that starts the app itself — suppressing both the relaunch
and the reminder to do one by hand; it cannot be combined with `--relaunch BUNDLE`.

[The loop](../AGENTS.md#the-loop) in AGENTS.md has the reasoning: why selecting the scenario and
relaunching the app are one command, and what to do when something else owns the launch.

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

- `GET /health` (reports `intercepting` / `proxyUp` / `pacEnabled` / `simBundleId` / `scenarios` /
  `sequences` / `answers` / `loadProblems` / `scenariosNotWhole`, and `pacError` when the PAC could
  not be read — `intercepting` is then unproven, not off) · `GET /recent`
  - The PAC is read in a worker thread, one observation at a time, and health answers within a
    second whether or not it has finished, so a hung `networksetup` shows up as a `pacError`
    instead of stalling health and the proxy's traffic together.
  - `loadProblems` is one string **per problem** found while loading the scenario files — a scenario
    with two invalid overrides yields two — as `"<file>.json: …"`, or `"skipped <file>.json: …"`
    when the whole file was rejected. `scenarios` cannot carry any of it: a scenario whose invalid
    overrides were dropped, and a malformed `default.json` replaced by an empty in-memory
    `default`, are both listed there looking exactly like a scenario that loaded.
  - `scenariosNotWhole` is the same problems keyed by the scenario each belongs to
    (`{"orders-outage": ["orders-outage.json: …"]}`), which is what `up --use NAME` asks. The
    strings cannot answer it: a file named `orders-outage.json: backup.json` produces a
    `"skipped …"` line that begins exactly like a problem with `orders-outage`. A file whose
    *name* was rejected appears in `loadProblems` only — it could never have become a scenario.
- `GET /rules` — the active scenario's rules, each carrying `rewrite`, `answer` (its row from
  `answers`) and `sequenceState` (its row from `sequences`, or `null`). It exists so a client can
  show what a scenario rewrites without reimplementing rule semantics: `rewrite` is the *engine's*
  description of what that rule answers with — `mode`, the `status` that will actually be sent (200
  for a `replace` naming none; `null` for a patch forcing none, which keeps the real response's),
  `bodyKind`/`bodyBytes` sized as the wire encodes them and reported as none for a bodyless 204/304,
  `patchKeys`/`patchStrategy`, `delayMs`, and for a sequenced rule its `advanceOn`, the
  `onExhausted` that will actually apply, and each step described *after* it inherits from the
  parent. A sequenced rule leaves the top-level `status` and body fields empty,
  because its answers are its steps, and a patch reports no body of its own, because it answers with
  the upstream's. `notWhole` is this scenario's entries from `scenariosNotWhole`,
  so a window can say a rule was dropped instead of quietly showing a shorter list.
- `POST /reset` — start a fresh run in the active scenario: rewind sequence cursors and clear answer
  counts, for every rule or one named with `{"id": ...}`. Returns `{"scenario": …, "reset": {id:
  runId}}` — the run id per rule is what binds a later assertion to this boundary
- `GET|POST /overrides` — act on the **active scenario**
- `DELETE /overrides` — **destructive**: deletes every override in the active scenario and rewrites
  its file. The only endpoint that does this.
- `GET /scenarios` · `POST /scenarios` · `PUT /scenarios/active` · `DELETE /scenarios/{name}`

`POST /overrides` refuses (**400**) a rule that fails validation and installs nothing. A scenario
*file* is instead reported-and-dropped at startup, because the file is in front of you and the
proxy must still start; an API call answering 200 for a rule it discarded is worse than refusing.

A write whose scenario file resolves outside `scenarios/` or outside the profile is refused with
**400** `{"error": "invalid_name", "detail": "path escapes …"}` before anything live changes —
[Profiles](#profiles) has the layouts that cause it. A name that could not be a path component is a
separate refusal, reported per endpoint: creating with one is the same **400** with a `detail` of
`invalid scenario name …`, while activating or deleting it is simply a scenario that is not there.

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
profile's proxy as this one's. The check runs in the proxy, so restart it
(`lyrebird down && lyrebird up`) after upgrading.

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
hand-written scenario stays short — but an override with no `match` matches *every* request, so give
it at least a path.

An override supports these fields and no others — an unknown one is rejected rather than kept,
because a field the engine ignores is a rule that answers with a default instead of what its author
wrote — a rule carrying `statsu: 503` would otherwise load cleanly and reply 200:

| field | |
|---|---|
| `id` | Stable name for the rule. Generated when omitted; derived from the rule's content for scenario files. |
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
naming the field; the rest of its scenario still loads. An `override add` is refused outright.

`match` supports these fields and no others — an unknown one is rejected rather than ignored,
because a typo'd field is not a stricter matcher but a missing constraint:

| field | |
|---|---|
| `method` | HTTP method, compared case-insensitively. Omit to match any method. |
| `path` | Path without the query string. `*` is the only wildcard; everything else is literal. |
| `query` | Query parameters that must all be present with these exact values. Others are ignored. |
| `bodyContains` | A substring that must appear in the request body. |

The help text of `lyrebird override add` and the validation that accepts or rejects a rule share
one vocabulary, `OVERRIDE_FIELD_HELP` and `MATCHER_FIELD_HELP` in `rules.py`, so the CLI cannot
advertise a field validation would refuse. The two tables above mirror that vocabulary; they are
maintained by hand, so keep them in step with it.

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
matcher moves it on. That is what makes delete-then-refresh reliable: a screen that fetches
its list twice on appear does not desynchronise the scenario.

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
scenario, editing the rule, or `lyrebird reset`. `GET /__mock__/health` reports
`sequences[]` with `nextStep`, `stepCount`, `exhausted`, `hasOverrun`, `serves` (how many times
each step has been served this run, keyed by step number) and an opaque `runId` that changes on
every reset. `/recent` records `sequenceId`, `selectedStep` and `advanced` per request —
separately from `matched`, so an exhausted `passThrough` (which answers nothing) is still visible.

## Proving a rule was in play

`GET /__mock__/health` also reports `answers[]`: one `{id, active, count, runId}` per rule in the
active scenario, counting the requests it has answered since the last `POST /reset` and naming the
run they were counted in. The `runId` is the token `POST /reset` returned for that rule — the same
one `sequences[]` reports — not a second identity: one boundary, read by both views.

The three states a caller has to keep apart: no `runId` key means this engine cannot say which run
its counts belong to; `"runId": null` means the rule has no run state at all — never reset, never
near a request, or dropped by a scenario switch or a replacement under the same id; and a `runId`
matching the one you hold, with `count: 0`, means your run happened and nothing answered in it.
Only the last is evidence about the boundary you drew.

The count is taken where the answer is produced — when a `replace` writes its response, and when a
`patch` has actually merged into a JSON upstream — not when the request is recorded. That is what
makes it trustworthy as a test assertion: a rule that matched but lost to a more specific one is
never credited, a patch dropped for a non-JSON upstream is never credited, an exhausted
`passThrough` is never credited, and a `replace` whose flow dies on the way back to the client still
is, because it did answer. The evidence also outlives the request's entry in the bounded `/recent`
buffer.

`lyrebird assert-answered ID` reads `answers[]` and exits non-zero unless the rule has answered
since the reset; `--run RUNID`, the token `lyrebird reset ID --json` hands back, additionally
requires the count to belong to that run, so a boundary that moved under the test fails rather than
passing on someone else's count.
[Six things worth knowing before you start](../AGENTS.md#six-things-worth-knowing-before-you-start),
item 3, has the exit-code table — 1 for an assertion made and failed, 3 for one that could not be
made — and how a harness should act on each.

Sequences are `replace`-only. A `patch` needs the upstream response, so it could not answer locally
when exhausted, and a patch skipped by a streamed or non-JSON upstream would spend a step the app
never saw.

## Scenarios

Named, saveable scenarios under `<profile>/scenarios/`. One active scenario drives the proxy. The
bundled examples are `enable-beta-export`, `orders-outage`, `slow-profile`, `checkout-edge-cases`,
`remove-item-then-refresh` and `retry-then-succeed` — the last two demonstrate the two sequence
triggers.

A scenario file carries `schemaVersion: 1`. It may be omitted, but any other value is refused whole:
a file written for a format this engine does not read would otherwise load with the wrong rules,
silently and only in the ways the format changed.

## Checking a scenario file without starting anything

Startup keeps the rules it can read and reports the rest, so a scenario can be live and quietly
missing the rule a test depends on. Two commands answer that from the file alone — no proxy, no
network change, nothing written to the profile:

```bash
lyrebird validate orders-outage                                           # does it load whole?
lyrebird explain-match --scenario orders-outage GET '/api/v1/orders/42'   # which rule would win?
```

`validate` exits 0 only when every scenario named loads whole, and `explain-match --scenario` exits
non-zero when the file did not load whole even if it selected a rule. Under `--json` the first
gives `{"ok", "problems", "scenarios": [{"name", "file", "loaded", "ok", "overrideCount",
"problems"}]}` and the second gives the live command's shape plus a `problems` list, so neither
has to be parsed as prose.

Both read scenario files through `store.load_scenario_file`, the function `Store._load` loops over
at startup, and rank with `rules.find_override` — so the verdict is the engine's, not a second
opinion about it.

[Making a scenario](../AGENTS.md#making-a-scenario) has the transcripts and how to read them.

## Files

`../bin/lyrebird` (launcher) → `cli.py`, which holds the `lyrebird` group and registers the
commands its siblings define: `supervisor.py` (start, stop, PAC, watchdog) · `simulator.py` (which
device, CA trust, relaunch) · `scenario.py` (scenarios and rules on the running proxy) ·
`evidence.py` (runs, sequences, answer counts) · `offline.py` (`validate`, `explain-match` — reads
files; the live `explain-match` only reads the proxy) · `api.py` (control-API calls and their profile scoping) · `ui.py`
(colours, banner, log tail). Behind them: `addon.py` (mitmproxy addon) · `rules.py`
(match/patch/validate, unit-tested) · `control.py` (aiohttp API) · `store.py` · `netproxy.py` ·
`config.py` (paths, ports, host scoping) · `examples/`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

`test_rules.py` covers matching order, patch merging, sequences and override validation;
`test_store.py` and `test_control.py` cover scenario persistence, path containment and the control
API's Host/Origin/Content-Type guard; `test_config.py` pins the host-scoping agreement between the
addon, mitmproxy's `allow_hosts` and the generated PAC, with `test_addon.py`, `test_netproxy.py`
and `test_launcher.py` covering the mitmproxy options, the `networksetup` parsers and how
`bin/lyrebird` finds its engine.

The largest of them is the CLI suite, split by concern the way the commands are:
`test_cli_supervisor.py` (`up`, `down`, the watchdog, `status`, the lock) · `test_cli_evidence.py`
(`sequence`, `reset`, `assert-answered`) · `test_cli_offline.py` (`explain-match` and `validate`) ·
`test_cli_profile.py` (which profile a control call means) · `test_cli_launch.py` (selecting the
scenario before the app is launched) · `test_cli_simulator.py` (which device the CA and the
relaunch land on), over the doubles they share in `cli_doubles.py`. It is almost entirely failure
paths — an unreadable runtime file, a foreign PAC, nothing to stop, a proxy running someone else's
profile.

All of it is hermetic — the simulator, the network and the proxy are replaced with doubles — so it
checks the orchestration but cannot prove that CA trust, relaunch, PAC routing or teardown work
against a real simulator and a real network service; [CONTRIBUTING.md](../CONTRIBUTING.md) has the
acceptance checks that do, and when to run them.
