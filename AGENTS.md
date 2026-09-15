# Developing Lyrebird

For changes to this repository, read [CONTRIBUTING.md](CONTRIBUTING.md) first.

- `engine/`: flat Python runtime modules, with tests in `engine/tests/`.
- `menubar/`: AppKit macOS app and unit tests; `project.yml` generates the Xcode project.
- `acceptance/`: synthetic iOS fixture; real interception checks are opt-in.
- `make setup` installs dependencies; `make doctor` checks the toolchain.
- `make format` formats sources; `make check` runs the ordinary local/CI checks.
- `make check-engine`, `make check-app`, and `make test-engine TEST_ARGS="-k reset"`
  support focused work. Python tests need permission to bind local sockets.
- `make acceptance` changes real simulator/network state; read its contribution instructions
  and the operator contract below before running it.

Use generic project names, reserved example domains, portable paths and synthetic payloads in
all tracked content. Never copy unrelated project names, internal paths, real profiles or private
publication-check rules into this repository. Any private hooks a maintainer keeps locally
must remain untouched.

---

# Operating Lyrebird from an agent

Lyrebird's contract for coding agents; assumes a shell and JSON.

## First run

Use your own profile. On a shared one, follow
[the scratch-scenario procedure](#working-on-a-scenario-without-disturbing-anyone) first, and name
that scenario in the loop. On a shared Mac, one agent runs at a time — see
[Several agents on one Mac](#several-agents-on-one-mac).

```bash
lyrebird --profile PATH validate NAME   # offline: non-zero unless that scenario loads whole
lyrebird --profile PATH up --use NAME   # start, activate the scenario, then relaunch the app
RUN=$(lyrebird --profile PATH reset ovr_x --json | jq -r .reset.ovr_x)   # a fresh run, and its id — immediately before the action under test
# …do the work you came to do…
lyrebird --profile PATH assert-answered ovr_x --run "$RUN" --timeout 30   # non-zero unless it answered in it
lyrebird --profile PATH down        # restores the proxy settings that were there before
```

The contract:

- **Profile** — always pass `--profile`.
- **Cleanup** — check `up`'s exit code; on failure stop and run `down`, because setup may have left
  the proxy running — unless `up` refused over a session you did not start: then wait, see
  [Several agents on one Mac](#several-agents-on-one-mac). Run `down` on every exit path of a
  session you started.
- **Exit codes** — 0 means the postcondition was met, never merely that the command ran.
  `assert-answered --run`: 1 = the rule answered nothing in your run; 3 = the assertion could not be
  made — resolve the reported cause, then reset and repeat the action.
- **Simulator** — more than one booted → `up --simulator <udid>`. That picks the device for the CA
  and the relaunch only: interception still covers the profile's hosts across the Mac and every
  simulator.
- **Proof** — only `assert-answered <id> --run "$RUN"` proves the named rule answered in your run,
  so keep the id `reset --json` prints. `--timeout N` waits for it instead of sleeping and hoping.

Do not:

- **Point it at production.** Development and simulators only.
- **Leave interception on.** Run `down` on the session you started.
- **Commit a profile** into this repository, or any public one. Scenarios can hold real payloads.
- **Assume no output means success.** Check exit codes; they are meaningful.

The rest:

- Scenarios → [Making a scenario](#making-a-scenario), [Sequences](#scenarios-that-move-between-states)
- State and proof → [Five things](#five-things-worth-knowing-before-you-start)
- Deleting → [Destructive operations](#the-destructive-operations)
- Failures → [TROUBLESHOOTING.md](TROUBLESHOOTING.md)

---

Reference. Lyrebird is designed to be driven by a coding agent as much as by a person: most of its
use is a machine putting an app into a specific backend state, checking something, and putting the
machine back the way it found it. The block above is the contract for that; read the section you
need.

## The loop

The block at the top of this file is that loop; this section is why it has that shape.

Always pass `--profile` explicitly. `LYREBIRD_PROFILE` works too, but an explicit path is one less
thing to be wrong about when something misbehaves later.

`up --use NAME` is one command because the order inside it matters: the scenario is selected
before the app is relaunched, so the app's *launch* requests — the ones a `use` afterwards is too
late for — are answered by the scenario you asked for. If `NAME` does not exist or did not load
whole, `up` launches nothing, says so, and exits 1 with the proxy still running; run `down`. If a
UI-test runner owns the app instead, pass `--no-relaunch` and launch it only once `up` exited 0.

`use NAME` on its own is still how you switch mid-run. It affects the requests that come *after*
it and relaunches nothing: an app that cached its launch response needs
`lyrebird --profile PATH relaunch`, which relaunches it on the simulator this run is bound to, or
another `up --use NAME`.

### Which simulator

`up` takes `--simulator UDID-OR-NAME`. That device is where the CA is trusted and
where `--relaunch` / `simBundleId` relaunches the app, so a UI-test run should pass the same UDID
it drives:

```bash
lyrebird --profile PATH up --use NAME --simulator <udid>   # `xcrun simctl list devices booted`
```

With exactly one simulator booted you can leave it out. With several booted and no choice
made, `up` **refuses and lists the booted devices** rather than let simctl pick one
for you. `status` reports the device the session recorded (`simulator` in `--json`), and
`lyrebird relaunch [BUNDLEID]` relaunches the app on that same device — use it instead of
`xcrun simctl launch booted <bundleid>` mid-run.

**It does not isolate traffic.** The PAC is installed on a network service and scoped by hostname,
so every simulator on the Mac — and the Mac itself — routes the profile's hosts through the proxy;
`--simulator` only decides where the CA is trusted and which app is relaunched. Do not treat device
selection as a way to run two scenarios side by side.

[engine/README.md — Which simulator](engine/README.md#which-simulator) has the rest: how names and
UDIDs are matched, and what happens on a device that does or
does not trust the CA.

### Several agents on one Mac

There is one session per user, so one agent at a time. The agent that ran `up` runs `down`; nobody
else does. A second agent's `up` is refused, naming the session's port and profile. To wait for it,
poll `LYREBIRD_CONTROL_PORT=<that port> lyrebird status --json` until `proxyUp` is `false` — that is
permission to try `up` again, not proof the session is gone: `up` still refuses over a journal a
failed `down` left behind, and says so. While a session is on, every simulator app on the profile's
hosts is answered from that session's scenario and credits that agent's runs, so quit your app until
it is your turn.

## Five things worth knowing before you start

**1. Exit codes mean the postcondition, not "the command ran."**

`up` exits non-zero if there is no single simulator to work on, if the CA could not be trusted, if
no network service was found, or if the app could not be relaunched — all cases where the proxy is
running but you are *not* mocking anything.
It refuses outright, before starting anything, when the profile lists no hosts.
Do not treat a zero exit from `up` as optional to check.

**2. A green assertion is not proof your mock was in play.**

A negative assertion — "this section is not shown" — passes identically whether your override
applied or never matched, because the real backend usually produces the same screen. A suite like
that can be testing nothing, and it stays green when a rule quietly stops matching.

```bash
RUN=$(lyrebird --profile PATH reset ovr_x --json | jq -r .reset.ovr_x)   # draw the boundary…
# trigger the action under test
lyrebird --profile PATH assert-answered ovr_x --run "$RUN"   # …and assert inside *that* boundary
```

Non-zero unless that rule answered a request in the run you name. `--timeout N` waits instead of
sampling. Put the reset immediately before the action, not once at start-up: the app's launch
fetches land in between, and an answer they produced would satisfy an assertion your test never
earned. A rule that answered is counted where the answer is produced, so the evidence outlives its
entry in `recent` and can never come from a rule that merely matched and lost.

**Keep the run id and pass it.** `reset` issues a fresh run id per rule, and every answer the rule
then gathers is reported under that id. A second reset, a reload, or a scenario switch ends that
run and starts another — and the rule id is
identical on the other side, so without `--run` a count belonging to the new run reads exactly like
the one your test earned. `--run` refuses that substitution instead of reporting it as success. The
run is re-checked on every poll, so a boundary destroyed while `--timeout` is waiting — the rule
vanishing with a scenario switch, say — fails the assertion there rather than being waited out.

With `--run`, 1 means the assertion was made and failed; 3 means it could not be made at all:

| Exit | Meaning |
|---|---|
| 0 | The rule answered, in the run you required |
| 1 | The rule is in that run and answered nothing — including a rule that is inactive and can never answer |
| 3 | The run you named is not the rule's current run, the rule is not in the active scenario at all, or nothing could be read about it: the proxy is unreachable, too old to report runs, or running another profile. **Not** "the mock did not apply" |

Exit 3 is the one a harness handles separately: nothing was learned about your run, so re-draw the
boundary and run the action again (or, for the version-skew cases, `lyrebird down && lyrebird up`
on your own session) rather than going to debug the rule. A `runId` of `null` — the rule has no run at all, its state
dropped by a scenario switch or a replacement — is exit 3 too, never a count of zero. So is another
profile's proxy taking the port mid-test: its counters describe someone else's rules, and the
fingerprint is compared on every poll, so the wait ends there instead of running to its timeout.

Without `--run` the command keeps its older, weaker meaning: "has this rule answered in whichever
run the proxy is in when I look", and every failure is exit 1. That is fine for a one-shot check
by hand. It is not enough for a suite that resets more than once, replaces rules, or switches
scenarios.

[engine/README.md — Proving a rule was in play](engine/README.md#proving-a-rule-was-in-play)
describes what the proxy reports underneath: the `answers[]` entries, the three `runId` states, and
where in the request the count is taken.

**3. `status --json` is the state query.**

```bash
lyrebird --profile PATH status --json
```

```json
{
  "proxyUp": true,
  "intercepting": true,
  "profileMismatch": false,
  "profileFingerprint": "3f0a1c4d9b22",
  "runningProfileFingerprint": "3f0a1c4d9b22",
  "activeScenario": "orders-outage",
  "overrideCount": 1,
  "answers": [ { "id": "ovr_9a99bd", "active": true, "count": 3, "runId": "5c1f9d0a7b3e4d62" } ],
  "scenarios": ["default", "orders-outage"],
  "simBundleId": "com.example.Store",
  "profile": "/path/to/profile",
  "service": "Wi-Fi",
  "pac": { "url": "http://127.0.0.1:8088/proxy.pac", "enabled": true, "ours": true },
  "journalError": null,
  "simulator": { "udid": "…", "name": "iPhone 17 Pro" }
}
```

Exit code is 0 only when the proxy is up **and** intercepting **for the profile you named**, so
`lyrebird status > /dev/null` works as a readiness check on its own. `--json` selects the output
format and nothing else — both forms exit the same way, so `lyrebird status && …` is safe to
write either way round.

`service`, `simulator` and the `pac` reading describe this Mac's one PAC-owning session, taken
from the journal rather than from the proxy, so they answer whether or not anything is running.
`journalError` is non-null when the journal could not be read at all — by this command or by the
proxy, which reads it too — and that means `down` cannot restore from it.

Exit 1 with the reason printed covers every way the postcondition is not met: no session, another
session's, a proxy that is not the one recorded, a PAC that is not routing here, and a default
route that has moved off the service the session took.
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-proxy-is-gone-but-the-network-still-points-at-it) has
what to do with each.

The control port can be held by a proxy started for a *different* profile. That proxy is
intercepting, but not for you, so `status` exits non-zero and says so: `profileMismatch` is `true`
with `intercepting` `false`, the two fingerprints name which proxy answered and which profile you
asked about, and everything that describes a profile's state (`activeScenario`, `overrideCount`,
`scenarios`, `sequences`, `answers`, `simBundleId`) is `null` — it is the other profile's, not
yours. The fix is that session's owner running `lyrebird down` — if that is not you, wait, see
[Several agents on one Mac](#several-agents-on-one-mac); there is one session per user, so `up`
refuses while that one exists, whatever port or profile you ask for. A proxy too old to report
`profileFingerprint` is another profile too — it predates the guard, so nothing can vouch for whose
it is; if you started it, stop it with that version's own `lyrebird down`.

**4. Relaunch the app after `up`, every time.**

`URLSession` caches the proxy configuration it saw at launch. An app that was already running will
ignore Lyrebird completely, with no error anywhere — it will just quietly talk to the real backend.
Set `simBundleId` in the profile and `up` handles it — and name the scenario in the same command,
`up --use NAME`, so the launch that follows meets it. Where something else owns the launch,
`up --no-relaunch` says so and nothing is started for you.

**5. `down` is not optional.**

It restores the proxy settings that were there before. Run it even on your failure paths, for a
session you started. Nothing else does it: a proxy that dies leaves the Mac routed at a dead port
until `lyrebird down` runs.

`down` is also the only recovery command, and it needs nothing to find the session: no `--profile`,
no port, no directory. It reads this user's one session journal, which is what the run recorded the
settings in. Exit 0 means they are back; exit 1 means they are not, and it says what is in the way —
including a PAC somebody set by hand since, which it never writes over: it prints the settings it
recorded at `up` for you to put back yourself.

There is one such session per user, so `up` refuses while one exists — even this profile's. For a
session you started, to put a different scenario in front of the app: `lyrebird use NAME && lyrebird
relaunch`.

## Making a scenario

**Write the file.** That is the whole of it: nothing in Lyrebird writes into a profile, so a
scenario is a file you author and `scenario reload` is how it reaches a running proxy.

Scenarios are JSON in `<profile>/scenarios/`, either there or in one
folder below it — `scenarios/checkout/cart-empty.json` is the scenario `checkout/cart-empty`, and
that qualified name is what `use`, `up --use` and `validate` take. The `name` inside
the file is derived from the path and ignored. `id` and `active` are optional — `id` is derived from
the rule when omitted — so the minimum is:

```json
{
  "name": "orders-outage",
  "overrides": [
    { "match": { "method": "GET", "path": "/api/v1/orders/*" },
      "mode": "replace", "status": 500, "body": { "error": "boom" } }
  ]
}
```

**An image, or any bytes, from a file.** A `replace` rule can answer with a file kept beside the
scenario instead of a `body`: `bodyFile` names it (one file name, same directory as the scenario
file) and a `Content-Type` header says what it is. The proxy is the one https host the simulator
already trusts, so this is how a screen meets an asset the backend does not serve yet — patch the
JSON that carries the URL so it points at a path on an intercepted host, and answer that path with
the file:

```json
{
  "name": "banner-preview",
  "overrides": [
    { "match": { "method": "GET", "path": "/api/v1/home" },
      "mode": "patch",
      "patch": { "banner": { "imageUrl": "https://api.example.com/mock-assets/banner.png" } } },
    { "match": { "method": "GET", "path": "/mock-assets/banner.png" },
      "mode": "replace", "bodyFile": "banner.png", "headers": { "Content-Type": "image/png" } }
  ]
}
```

`banner.png` sits next to `banner-preview.json`. The file is read when the scenario loads — replace
it, then `scenario reload` — and `validate` fails naming the rule and the file when it is missing.

Files are picked up when the proxy starts, so a scenario file written while Lyrebird is running is
not there yet. `lyrebird --profile PATH scenario reload` re-reads them without a restart; it refuses
whole if any file cannot be read, and **it resets run evidence**, so `lyrebird reset` and any
`assert-answered --run` boundary come after it, not before. A restart of your own session
(`down && up --use orders-outage`) is the other way, and is the lenient one: startup keeps the rules
it can where reload refuses.

**Check the file before you start anything.** A file the proxy cannot read whole does not stop it
starting: it keeps the rules it can, reports the rest to its log, and runs. So a scenario can be
live and quietly missing the one rule your test depends on — `up --use NAME` is the one command
that refuses, and only for the scenario you name it. `validate` is where that surfaces first, and
it needs no proxy, changes no network settings and writes nothing:

```bash
lyrebird --profile PATH validate                  # every file in <profile>/scenarios/
lyrebird --profile PATH validate orders-outage    # just this one
```

```
✓ orders-outage            1 rule(s)
✗ partial                  1 rule(s) kept, 2 dropped
    partial.json: override[1]: match: unknown field 'paths' — a matcher may only carry method, path, query, bodyContains
    partial.json: override[2]: duplicate id 'ovr_ok' — ids must be unique within a scenario; this rule was skipped
✗ broken                   not loaded at all
    skipped broken.json: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)
```

**Exit 0 only when every scenario named loads whole.** It refuses a name that does not exist rather
than reporting an empty scenario for it, and it refuses to exit 0 having found no files at all —
the usual cause of that is the wrong `--profile`.

`--json` is the machine-readable form:

```json
{
  "ok": false,
  "problems": [],
  "scenarios": [
    { "name": "partial", "file": "/path/to/profile/scenarios/partial.json",
      "loaded": true, "ok": false, "overrideCount": 1,
      "problems": ["partial.json: override[1]: match: unknown field 'paths' — …"] },
    { "name": "broken", "file": "/path/to/profile/scenarios/broken.json",
      "loaded": false, "ok": false, "overrideCount": null,
      "problems": ["skipped broken.json: Expecting property name enclosed in double quotes: …"] }
  ]
}
```

`loaded` false means the file was refused entirely — malformed JSON, not an object, or a
`schemaVersion` this engine does not read — and `overrideCount` is then `null`, not `0`: a refused
file has no rule count, and zero would read as a scenario that loaded and happens to be empty.
`ok` false with `loaded` true is the dangerous one: that scenario *will* run, without the rules in
`problems`.

The top-level `problems` is about the *request* rather than a file — a name that names nothing, a
name that could not be one, a profile with no scenarios in it — and `scenarios` is then empty. Every
failure comes back in this shape, so `--json` output never has to be parsed as prose.

**Check a rule before you launch anything.** `explain-match` answers, in a second, what otherwise
costs a cold launch and a walk through the app:

```bash
lyrebird explain-match GET '/api/v1/orders/42'
# → ovr_9a99bd is selected  (replace)
#   also matched, ranked lower:
#     ovr_all_orders  {"path": "/api/v1/*"}
#   did not match:
#     ovr_users       path: '/api/v1/users' does not match '/api/v1/orders/42'
```

It reports what would be *selected*, never what would be returned — a `patch` answers only if the
upstream turns out to be JSON, and a sequence's step depends on run state this does not read. The
"also matched" list is the one to read closely: those rules answer the same request whenever yours
is not there, so a rule broad enough to appear in it is a rule quietly mocking its neighbours.

That also splits the two things "nothing matched" runs together. If `explain-match` selects your
rule and it still never fires, the rule is fine and the app did not make the request.

`--scenario NAME` asks the same question of a saved file instead of the running proxy — same
ranking, same reasons, no proxy needed:

```bash
lyrebird --profile PATH explain-match --scenario orders-outage GET '/api/v1/orders/42'
```

Its output and `--json` shape are the live command's, plus a `problems` list — present only with
`--scenario`, because only then is there a file whose load problems were read. It exits non-zero
when the file did not load whole even if a rule was selected. The proxy drops those rules too, so
the winner it names is the one that would be picked; what is *not* true is that the ranking covers
the rules you wrote — yours may be missing rather than out-ranked, which is usually the thing you
ran the command to find out. `validate` is where you read those problems in full.

And to see what happened:

```bash
lyrebird recent --json               # machine-readable; filter it for `matched` yourself
lyrebird recent                      # all of it, human-readable
```

These call the same HTTP control API, so you never need to construct the requests yourself. If you
do call it directly, note two requirements that otherwise surface as a bare 4xx: the `Host` header
must be `127.0.0.1`, `localhost` or `[::1]` with the control port (**421** if not), and any
POST/PUT/PATCH/DELETE with a body must send
`Content-Type: application/json` (**415** if not). Both exist to stop a web page you happen to have
open from driving the proxy. `engine/README.md` has the endpoint list.

One proxy holds the control port, so the CLI also names the profile it means with an
`X-Lyrebird-Profile` header: run it with a `--profile` other than the one that is running and the
call is refused with **409** `profile_mismatch` instead of quietly acting on the running profile.
`/__mock__/health` and `/proxy.pac` are unscoped and answer whoever asks. The check lives in the
running proxy, so its owner restarts it (`lyrebird down && lyrebird up`) after upgrading.
[engine/README.md — Admin API](engine/README.md#admin-api-__mock__) has the header, the error body
and why those two routes are exempt.

## Scenarios that move between states

A rule can hold a list of steps instead of one response — for "delete a row, refresh, it's gone", or
"the first attempt fails and the retry succeeds". Two things about driving them.

**Reset immediately before you trigger the action, not at startup.** Cursors are in memory and
already reset when you `use` a scenario, but anything the app did in between — a launch fetch, a
prefetch — may have moved them.

```bash
lyrebird --profile PATH reset ovr_items_list
# now trigger the UI action
lyrebird --profile PATH sequence wait ovr_items_list --step 2 --timeout 30
```

`reset` with no id rewinds every rule in the active scenario. It is the same boundary
`assert-answered` reads, so one reset serves both.

**`assert-answered` cannot verify a sequence.** It proves the rule answered, not which step it
served, so it cannot express "step 1, then step 2". `sequence wait` waits for a named rule to
serve a named step in the current run, and never burns its timeout on a question live state can
already answer: a step already served in this run **succeeds immediately** (the serve counter is
scoped to the run, so anything in it happened after your reset), and a run already past the step
without ever serving it **fails immediately**, which is the usual symptom of the app making a
request you did not expect.

`status --json` carries `sequences[]` for the rules that have one and `answers[]` for every rule in
the active scenario — [Sequences](engine/README.md#sequences) and
[Proving a rule was in play](engine/README.md#proving-a-rule-was-in-play) list what is in each. The
`runId` in both is the token `reset` returned for that rule, but only `assert-answered --run` takes
one back from you: `sequence wait` baselines itself on whichever run is current when it starts.

Both lists are `null`, not `[]`, when the running proxy did not report them — a proxy started from
an engine older than the field, or one that is not up at all. An empty list means "the engine
answered, and there is nothing to show"; `null` means "it could not tell you", which is a different
thing to act on. If you pipe this into `jq '.answers[]'`, handle the null rather than reading it as
zero answers; `lyrebird down && lyrebird up` on your own session clears the version-skew case.

The same distinction one level down: inside an entry, `"runId": null` means the rule has no run at
all — never reset, never near a request, or its run state dropped by a scenario switch or a
replacement. It is not a count of zero, and reading it as one is how a test comes to believe in a
boundary it never drew. `recent` shows which request took which step, and which request advanced
what, so a scenario that went wrong can be read back rather than guessed at.

If a rule advances when you did not expect it to, the fix is usually a narrower `match`, or an
`advanceOn` so that only the mutating request moves the cursor and repeated reads do not.

## Working on a scenario without disturbing anyone

A profile is shared state. If it belongs to a person or a team, do not edit their scenarios. This is
for a session you started; `reload --use` and `use` switch the running session's scenario, so on
another agent's session wait for its `down` instead.

```bash
lyrebird status --json                                  # note activeScenario before you touch anything
cp PATH/scenarios/orders-outage.json PATH/scenarios/agent-scratch.json   # start from a copy
lyrebird scenario reload --use agent-scratch            # re-read the files, and activate it
# …work…
lyrebird use orders-outage                              # put back what you found
rm PATH/scenarios/agent-scratch.json                    # and take the copy away again
lyrebird scenario reload
```

`scenario reload` refuses on any problem and publishes nothing, so a copy that did not land is a
refusal rather than a scenario that mysteriously does nothing.

`assert-answered` refuses an id it cannot find rather than reporting zero answers for it, for the
same reason: a typo and a rule that never fired need completely different fixes.

## The destructive operations

There are none in Lyrebird: nothing it runs writes into a profile, so every change to a scenario is
one you make to a file yourself, with whatever undo your editor and your version control give you.
`lyrebird down` restores the network settings and touches nothing else. If the profile is under
version control that is your safety net; if not, take a copy before editing someone else's
scenarios.

## When it does not work

The symptom → cause table is in [TROUBLESHOOTING.md](TROUBLESHOOTING.md), together with where the
proxy log is and how to read it.
