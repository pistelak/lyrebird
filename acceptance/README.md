# The acceptance fixture

A synthetic iOS app and the profile it is driven with. Together with
`engine/tests/acceptance/` they are the only check in this repository that exercises the whole
interception path — an app in a simulator, macOS PAC routing, TLS terminated by a CA trusted in
that simulator, and an override answering locally.

Everything else in `engine/tests/` replaces one of those with a double, which is why a regression
in CA trust, relaunch ordering, routing or teardown can leave the rule engine and the app build
perfectly green.

```
acceptance/
  FixtureApp/                     the app (xcodegen; project.yml is the source of truth)
    project.yml
    FixtureApp/FixtureApp.swift
  fixture-sessions/               copied into the temporary profile's sessions/ before `up`
    fixture-decoy.json            the scenario that must NOT answer once another is selected
    fixture-replaced.json         one replaced HTTPS response, status 503
    fixture-sequence.json         two steps, one per launch
```

## The app

`com.example.lyrebird-fixture`. On every launch it makes exactly one HTTPS `GET` to
`https://api.example.com/api/v1/fixture` and shows the status and body it got back in a label
(accessibility identifier `fixtureResult`). A "Fetch again" button (`fixtureFetchAgain`) repeats the
request for anyone driving a sequence by hand; the harness relaunches the app instead, which is what
Lyrebird's users do anyway and needs no UI-test target.

Nothing has to exist at that host. The PAC routes it to the proxy, and a `replace` override answers
without an upstream — mitmproxy runs with `connection_strategy: lazy` precisely so a mocked host
never has to resolve. The app therefore needs no network at all beyond the proxy on loopback.

There is no UI-test target, so the app reports through three JSON arrays in its own container,
which the harness reads with `xcrun simctl get_app_container <udid> com.example.lyrebird-fixture
data`:

| File | What it holds |
|---|---|
| `Documents/launches.json` | one record per launch, written before any networking |
| `Documents/results.json` | one record per launch: `status`, `body`, `error`, `attempts` |
| `Documents/displayed.json` | one record per rendered label, written from the value the label is bound to |

All three are read, because each alone can be true while the path is broken. A result appears only
once a request has *finished*, so an app that was launched and then hung would leave none — which
is why "`up --use nosuch` launched nothing" is checked against `launches.json` and not against
results. And a response that arrived is not a response that reached the screen: `displayed.json` is
evidence at the label rather than at the socket, which is as close as this gets without a UI test.
The proxy's own side of it — `assert-answered`, `sequence wait`, `status --json` — is checked
separately.

`attempts` exists because a launch retries a *transport* error twice, a second apart: the PAC can
take a moment to be picked up right after `up` installed it. A status is never retried — a 503 from
an override is an answer. All attempts fold into the one record for that launch, so the array's
length is the number of launches, which is what the "nothing was launched" check counts.

## Building it by hand

```bash
# From the repository root:
make setup-app build-fixture
xcrun simctl install booted acceptance/FixtureApp/.build/Build/Products/Debug-iphonesimulator/FixtureApp.app
```

`FixtureApp.xcodeproj` and the generated `Info.plist` are ignored by git, exactly as `menubar/`'s
are: `project.yml` is the source of truth. The app is unsigned — it is installed with `simctl` and
never distributed.

The harness does all of the above for you, into a freshly reinstalled container.

## The profile

There isn't one here, deliberately. Nothing profile-shaped — a `profile.json`, or a `sessions/`
directory — lives in this repository outside `engine/examples`, because committed profiles can disclose the hosts you intercept and the payloads you saved.
Use generic names and synthetic data throughout the fixture.

So the harness makes its own. `lyrebird init` writes the bundled example profile into a temporary
directory, the harness asserts that it intercepts `api.example.com` — the fixture app's URL is
compiled in, so a bundled profile that stopped intercepting it would make every check fail as "the
app never reached the proxy" — and sets `simBundleId` to the fixture's bundle id, which is exactly
what `init`'s own output tells a new user to do. The three sessions below are copied in beside it.
Naming the bundle id is what lets `up` relaunch the app itself, which is the ordering the checks
are about.

The three sessions answer the same request, and differ so that answering with the wrong one is
visible rather than plausible:

- **fixture-decoy** — 200, marker `LYREBIRD-FIXTURE-DECOY`. Activated first, so that
  `up --use fixture-replaced` has something to displace.
- **fixture-replaced** — 503, marker `LYREBIRD-FIXTURE-REPLACED`. A status the request could not
  have got any other way.
- **fixture-sequence** — two steps, `…STEP-ONE` then `…STEP-TWO`, advanced by the rule's own calls.
  `onExhausted` is left at the default `error`, so an unplanned third request answers 500 and shows
  up in the app's own record instead of passing for a repeat of step 2.

## Running the checks

See **Acceptance checks** in [CONTRIBUTING.md](../CONTRIBUTING.md) — prerequisites, the exact
command, what it does to the machine, and what it cleans up.
