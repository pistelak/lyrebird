# Contributing

## Setup

Use Python 3.12 (the patch version is in `.tool-versions`) and Xcode 16 or newer, with
Command Line Tools selected via `xcode-select`. Source builds of the formatter require
Swift 6 or newer, which setup checks before downloading and compiling. From the repository root:

```bash
make setup
make doctor
make check
```

`make setup-engine` recreates `engine/.venv` from the hashed development lock, removing
packages no longer required. Keep unrelated Python work in a separate environment.
`make setup-app` installs XcodeGen and swift-format into `.build/tools/bin`, using versions
from `scripts/tool-versions.env`. It verifies and extracts the complete
XcodeGen release archive, including its settings bundle. An installed swift-format is reused
only if its version matches; otherwise setup builds its verified source archive using the
dependency revisions in `scripts/swift-format.resolved`. The first formatter build needs
network access and takes a few minutes.
No global tools or git hooks are installed or replaced.

CI uses the latest Python 3.12 patch available from `actions/setup-python`; security-only
patch releases in `.tool-versions` may not have GitHub-hosted macOS binaries. The runtime and
development dependency locks are the same locally and in CI, but the interpreter patch can differ.

The pinned tools are XcodeGen 2.46.0 and swift-format 602.0.0. Xcode itself is supplied by the
machine; `make doctor` reports its version. The workflow uses the `macos-15` runner image,
so the Xcode build environment is not completely locked. Use the same selected Xcode for
local builds and investigate compiler-version differences when reproducing CI failures.

## Before opening a pull request

```bash
make format         # Ruff (including safe lint fixes) and swift-format
make check          # shell syntax, Python checks, Swift checks, app tests and fixture build
```

CI calls the same `make check-engine`, `make check-shell`, and `make check-app` targets.
Release stamping and launcher smoke checks also run in CI. For a shorter development loop:

```bash
make check-engine
make test-engine TEST_ARGS="-k reset"
make check-app
```

`make help` lists the main targets. Checks do not install dependencies or change network
settings. Tests bind local sockets, so an agent sandbox must allow that. Build output stays in
ignored `.build` directories. `make acceptance` is separate and never part of `make check`.

## Formatting and editor setup

Ruff owns Python formatting and import ordering; swift-format owns Swift formatting and lint.
Their checked-in configurations and `.editorconfig` set four-space source indentation and a
120-column formatter width. Makefile recipes use tabs. Point your editor's Python interpreter
at `engine/.venv/bin/python` and Swift formatter at `.build/tools/bin/swift-format`; enable
format-on-save if desired. Generated projects, virtualenvs and build output are outside the
formatter targets.

Only use Python `# fmt: off` / `# fmt: on` around a statement when table alignment carries
meaning. Swift's `OnlyOneTrailingClosureArgument` rule is disabled to retain idiomatic SwiftUI
calls with an `onDismiss` closure and trailing view content. Other enabled Swift lint rules
fail the check via `--strict`. Keep the complete rule map: swift-format replaces that map
instead of merging it with defaults. Negative naming and indentation probes in `make lint-app`
verify that both style rules and whitespace checks remain enforced.

## Updating dependencies

Edit `engine/requirements.in` or `engine/requirements-dev.in`, then run:

```bash
make lock
make setup-engine
make check-engine
```

The pinned uv resolver generates universal Python 3.12 requirements with transitive versions
and artifact hashes. The development lock is constrained by the runtime lock so both install
the same runtime versions. `make check-locks` verifies freshness stamps offline, so editing
inputs without relocking or manually changing generated contents fails the check unless someone
deliberately re-stamps them. Stamps catch accidental drift; they do not verify a dependency
resolver's work. Existing
resolutions are retained when possible; to upgrade a
transitive dependency deliberately, use `uv pip compile --upgrade-package NAME` with the same
flags as `make lock`, then regenerate both locks and run the checks. Do not edit generated
`.txt` files by hand or add private index URLs or local filesystem dependencies.

Normal installs still use pip; Lyrebird retains its flat modules and is not a Python package.
Update Swift tool versions and archive SHA-256 values together in `scripts/tool-versions.env`
(and refresh `scripts/swift-format.resolved` for formatter dependency changes),
then run `make setup-app`, `make format-app`, and `make check-app`. Review formatter upgrades
as separate mechanical changes.

## Acceptance checks

The ordinary checks are hermetic: they replace the simulator, the network and the proxy
with doubles. They check the orchestration — what is called, in what order, with what result — but
nothing in them can prove that CA trust, the relaunch, PAC routing or teardown work against a real
simulator and a real network service, because each of those lives in the part they replace. The
acceptance checks are where those are exercised, by driving a real app in a real simulator through
a real proxy:

```bash
LYREBIRD_ACCEPTANCE_SIMULATOR=<udid-or-name> make acceptance
```

**Prerequisites.** Xcode with a simulator runtime and the tools and engine venv from *Setup*.
Exactly one simulator may be booted while they run: the harness passes
`up --simulator` so the CA and the relaunch are bound to the device it means, but it still installs
the app and reads its container itself, and a second booted device makes "which one" a question
nothing here can answer. Boot one yourself, or name one in `LYREBIRD_ACCEPTANCE_SIMULATOR` (udid or
device name) and the run boots it and shuts it down again.

With nothing booted and nothing named the run **skips** and says so; a name that matches no device,
two booted simulators, or a Lyrebird PAC already enabled on the network service **fail** — the
machine could have run the check, and the answer would not have meant anything. Once the
prerequisites pass nothing skips.

**What it checks.** It is one test with six phases, not six tests: they share one proxy, one app
and one container of evidence, and each depends on the state the last left, so separate test
functions would only have looked independent. Each phase prints its name, so a failure says how far
the procedure got. The phases: a locally replaced HTTPS response reaching the app's screen; the scenario
being selected *before* the launch that meets it (a decoy answers the same request differently, so
getting the right one cannot be luck); a two-step sequence moving on at the next launch, confirmed
from both ends with `sequence wait`; `up --use <unknown>` refusing, launching nothing and leaving
the proxy for `down`; `down` putting the previous proxy settings back; and the watchdog putting
them back when the proxy is killed outright. They go through the shipped commands wherever there is
one — `up --simulator`, `lyrebird relaunch`, `assert-answered --run` — so the path a user takes is
the path that is checked; `simctl` is called directly only for what Lyrebird has no command for
(install, uninstall, boot, shutdown, reading the app's container).

**What it does to your machine.** It builds and installs the fixture app, adds Lyrebird's CA to
that simulator's keychain, and switches the *active network service's* auto-proxy URL to a PAC on a
loopback port for about a minute. Traffic for `api.example.com` goes to the proxy; everything else
stays `DIRECT`.

**The profile.** Made by the run, not committed: `lyrebird init` writes the bundled example
profile into a temporary directory and the harness points its `simBundleId` at the fixture app.
Keep generated profiles outside the repository; only the synthetic examples in `engine/examples`
belong in version control.

**Cleanup.** It runs `down` however the run ended — a failing check, a Ctrl-C, or a `kill`, all of
which take the same path: SIGTERM and SIGHUP are turned into the `KeyboardInterrupt` pytest already
unwinds, so every finalizer runs and the report is still printed. It then reads the PAC back from
`networksetup` and fails if the settings are not the ones recorded before anything started, so a
restore that did not happen cannot pass unnoticed. (With no PAC URL configured to begin with,
"restored" means *disabled*: macOS rejects an empty PAC URL, so `down` can only switch ours off and
the URL stays in the field — the same thing an ordinary `lyrebird down` leaves behind.) If `down`
did not restore them, the harness kills the proxy this run started — after `ps` confirms the pid is
still that process — gives the watchdog its window, and failing that sets the recorded values back
itself; and it still fails, saying so, because a cleanup that depends on the command it is checking
is not a cleanup. The fixture app is uninstalled and a simulator this run booted is shut down
again, both checked rather than assumed.

Only the *first* signal acts. Everything after it is teardown — restoring the network,
uninstalling the app, shutting the simulator down — and a second signal in the middle of that
leaves the machine worse than the first one found it: three SIGTERMs in quick succession used to
leave the simulator booted and the app installed, with the finalizers dying in interpreter
shutdown. So later signals are recorded and dropped, as is a first one that lands during the
restore itself (by then `down` has stopped the watchdog, and a restore abandoned between two
`networksetup` calls leaves the Mac routed at a port with no proxy behind it and nothing running
that would notice) — it is re-raised the moment the network is back. What remains is bounded by its
own timeouts, and `kill -9` is still `kill -9`.

**What a hard kill leaves.** `kill -9` on the pytest process runs nothing, and the proxy is started
detached, so it and its watchdog survive with the PAC still installed. Nothing can promise
otherwise.

The remedy needs only the control port, and it is the same one users get, in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-proxy-is-gone-but-the-network-still-points-at-it):
read the control port back from the installed PAC and run `down` against it. The temporary profile
is gone by then, but `down` does not need it — it discovers the proxy on that port.

Two things are deliberately left on disk. **The CA stays trusted in that simulator** — `simctl`
offers no way to remove one root certificate; `xcrun simctl keychain <udid> reset` clears added
certificates, or erase the device. And the profile, state directory, proxy log and CA private key
live in pytest's temporary tree (`/private/var/folders/…/pytest-of-$USER/`), which keeps the last
few runs by design; delete that directory if you would rather they were gone. The generated
`FixtureApp.xcodeproj`, its `Info.plist` and `acceptance/FixtureApp/.build` stay in the checkout
and are ignored by git.

**Why they are not in CI.** GitHub's macOS runners have no booted simulator and no business having
their network settings rewritten, and the whole point of these checks is that both are real. They
are the check to run by hand before a release, or after touching `up`, `down`, the watchdog, the
PAC, or CA trust.

`acceptance/README.md` describes the fixture app and the profile it is driven with.

## What lands on `main` and what goes through a pull request

Scale the landing to the change:

- **Directly on `main`:** docs, comments, and small fixes — with `make check`
  green, and after the independent review below for anything
  beyond a trivial fix.
- **Via a branch and pull request:** anything security-relevant (host scoping,
  path containment, the control-API guard, CA handling), changes to command
  exit-code or postcondition semantics, the control-API surface, new features,
  and menu-bar app changes. Do not merge until CI — the engine checks and the
  app build on a clean machine — is green and a human has read the diff. The
  branch is not protected, so this is a rule to follow, not one the platform
  enforces.

Either way, a behavior change is not done without a test pinned to the
failure that motivated it (see *Test the failure path* below).

## Releasing

CI runs on tags matching `v*` as well as on `main` and pull requests, so the
commit a release is cut from is checked as a tag, in the configuration that
ships: the app is built `Release` and stamped with the tag's version, and the
engine is exercised through `bin/lyrebird` in its installed layout.

1. Tag a commit on `main` — `git tag vX.Y.Z && git push origin vX.Y.Z`.
   `vX.Y.Z` with three numeric components; anything else (`v1.2`, `v1.2.3-rc.1`)
   fails the version check on purpose.
2. Wait for the run on the tag to succeed. `release-candidate` needs both
   required checks, so its artifact cannot exist otherwise.
3. Download `lyrebird-vX.Y.Z` from that run. It holds `Lyrebird-vX.Y.Z.zip` —
   the exact bundle the checks passed on, not a rebuild — and
   `CHECKED-COMMIT.txt`.
4. Before publishing, confirm the artifact belongs to the tag as it stands on
   GitHub *now* — fetch the tag first, because a local tag still pointing at the
   checked commit says nothing about one that was moved on the remote:

   ```bash
   git fetch origin "refs/tags/vX.Y.Z" && git rev-parse --verify 'FETCH_HEAD^{commit}'
   # must equal the `commit:` line in CHECKED-COMMIT.txt
   ```

5. Create the GitHub Release by hand — source-only, with no zip attached:

   ```bash
   gh release create vX.Y.Z --verify-tag --notes-file release-notes.md
   ```

   `--verify-tag` refuses a tag that is not on the remote, but it does not
   compare commits, so it replaces nothing in step 4 — do both. Nothing in CI
   has write access to the repository, and publishing stays a human decision.

   The zip is deliberately not published. It is ad-hoc signed, so macOS blocks
   it on first launch, and that warning would land at the worst possible moment
   on a tool that installs a CA and rewrites proxy settings. Its job is to prove
   the checks passed on the tagged commit; anyone who wants the app builds it,
   and a locally built one raises no such prompt.

What the artifact proves and what it does not: it proves the engine and app
checks passed on the commit named in `CHECKED-COMMIT.txt`, and that the zip is
what that build produced. It does not stop anyone moving the tag afterwards —
step 4, done against the freshly fetched remote tag, is what catches that, and
it is worth doing every time.

The app reports `X.Y.Z (N)`, where `N` is `git rev-list --count HEAD`. A
marketing version of `0.0.0` means a build off a tag: a development build, not
a release. The build number only increases along descendants of the previous
release, so cut releases from forward-moving history — a tag on an older commit,
or a retag of one already released, produces a build number that is not larger
than the last published one, and macOS will not treat it as an update. Check
that the new build number is larger than the last one you published; CI cannot,
since it does not know what was published.

The engine is not stamped with a version at all; it is identified by the tag the
release was cut from. The zip is **ad-hoc signed** — fine for running on your own
machine, not for handing to other people. See the distribution paragraph in
[menubar/README.md](menubar/README.md) for what Developer ID signing and
notarization would take.

## Independent review

Before pushing anything beyond a trivial fix, get a review from a capable
model outside the authoring agent's family (any independent model for
human-authored changes). The Codex CLI example below therefore fits changes
authored outside the GPT family; for Codex-authored changes use a non-GPT
equivalent, and if none is available, say plainly that the independent-review
requirement is unmet:

```bash
P=$(mktemp); O=$(mktemp); cat >"$P" <<'EOF'
<goal, exact paths in scope, constraints, non-goals,
 proof expected per claim, output shape>
EOF
codex exec -s read-only -C . \
  -m gpt-5.6-sol -c model_reasoning_effort="xhigh" \
  -o "$O" - <"$P"
```

The prompt contract does the work: state the goal, the exact scope, what is
out of scope, and demand file:line evidence for every claim. Read the output
file and verify every finding against the code before acting; fix what is
real, name what you skip.

## House rules

**No example may reference a real API.** Examples, fixtures and docs use
[RFC 2606](https://www.rfc-editor.org/rfc/rfc2606) reserved domains only — `example.com`,
`example.org`, `example.net`, `*.test`, `*.invalid`, `localhost` — and `com.example.*` bundle
identifiers. A fixture naming a host somebody actually runs is a fixture that breaks when that
host changes, and it points strangers at a server they have no business knowing about.

**Use generic names and portable paths.** Examples, tests, comments, screenshots and shared logs
must not identify unrelated projects, customers, internal services or developer workspaces.
Use names such as `Store`, `orders-outage` and `com.example.Store`, paths such as
`/path/to/profile` or a temporary directory, and synthetic payloads. Do not copy real response
bodies and merely rename their host: identifiers and nested fields can still disclose their origin.
Public dependency links and this repository's own identity are fine.

**Optional private checks stay private.** A maintainer may keep publication checks outside the
tracked tree. A fresh clone does not inherit local hooks. Provision any such checks from your
own trusted private source; do not copy their project-specific rules into this repository.
Before sharing a diff, review new names, paths, fixtures and logs against the generic-example
rules above. `make setup` deliberately leaves existing hooks alone.

**Never commit a profile.** Profiles hold the hosts you intercept and the payloads you saved, so
they live outside this repository. A scenario file can contain a real response body in full; see
[SECURITY.md](SECURITY.md) for what that means before you attach one to an issue.

**`rules.py` stays pure.** No proxy or IO imports — it is the one module that can be unit-tested
with the standard library alone, and that is worth protecting.

**Validate at the boundary.** Overrides are validated when they are stored, not when they are used,
so a malformed rule fails at the API call that created it rather than raising inside a proxy hook on
every matching request. Anything that becomes a filesystem path goes through
`store.safe_component`.

**A function named for an outcome must fail when it does not achieve it.** This is the bug this
codebase attracts most, by a wide margin. Shapes it has taken: `up` exiting 0 when nothing was
being intercepted, `trust-ca` returning 0 after failing to trust anything, `down` printing
"stopped" with the proxy still running, a scenario silently created empty when the thing it was
told to clone did not exist, an empty host list falling back to a built-in default, and a dropped
patch that looked identical to "no rule matched".

They share one shape: **an operation that cannot achieve its postcondition returns the same signal
as one that did.** It happens here more than in most code because nearly everything this tool does
can partially succeed — shelling out to `networksetup` and `simctl`, reading files someone edited
by hand, talking to a proxy that may have died — and warning-and-continuing is always the shorter
path to write.

When you add code, the questions to ask:

- If this fails, does the caller find out? A `⚠` printed to a terminal is not a return value.
- Am I discarding a result? `subprocess.run(...)` without checking `returncode` is the classic.
- Am I substituting a default for something the caller asked for? If they named a thing that does
  not exist, say so — do not hand back an empty one.
- Am I converting an exception into a benign value? If so, is the benign value *true*? Catching
  `OSError` and returning `{}` says "there is nothing here", which is a different claim from "I
  could not read it".
- Would the error message send someone to the right place, or to the next problem it causes?

Warn-and-continue is fine when the caller can still succeed without the thing that failed. It is
not fine when the command is named after the thing that failed.

**A per-rule side effect must be checked against the case where the rule loses.** `find_override`
returns only the *most specific* match, so a rule whose matcher fits a request may not be the rule
that answered it. Any effect keyed to "my matcher matched" rather than "I answered" therefore fires
for rules that were shadowed — and, like everything above, the drift is invisible at the moment it
happens. This is the shape that made sequence cursors advance on requests they never served.

**Test the failure path.** Every instance above was found by running the tool or reviewing it, not
by the tests, because the tests covered the happy path. The CLI suite is almost entirely failure
paths — an unreadable runtime file, a foreign PAC, nothing to stop — and that is the model to copy:
`engine/tests/test_cli_supervisor.py`, `test_cli_evidence.py`, `test_cli_offline.py`,
`test_cli_profile.py`, `test_cli_launch.py` and `test_cli_simulator.py`, over the doubles they
share in `cli_doubles.py`.

**A comment names the failure it prevents, and the test that pins it.** The comments in this
codebase explain *why*, usually by citing the bug a line prevents, and that is worth keeping. What
is not worth keeping is the bug's whole story told at the line: the same paragraph ends up
repeated at every site that depends on it, and none of the copies can be checked. So the shape is
one sentence at the site, naming the failure and the test that would catch its return:

```python
# Raised, not returned as "no PAC": a read that failed used to parse as "not ours", and `down`
# then left the PAC pointing at a dead port — see test_pac_status_raises_when_networksetup_fails.
```

The narrative — what was observed, what it cost, why this fix and not another — lives in that
test's docstring, where running the test verifies it. Design rationale and invariants that no
single test captures (why `config` rebinds module globals, the order `up` does things in) stay at
the site. Apply this as you touch code, not as a sweep: rewriting comments that nobody is reading
is churn.

**Security-relevant changes need a test.** Host scoping, path containment and the control-API guard
all have regression tests in `engine/tests/`; extend them rather than working around them. If you
change host matching, update *all three* mechanisms (`is_intercepted_host`, `allow_hosts_regexes`,
`pac_contents`) — they are generated from one list precisely so they cannot drift.

## Commits

Conventional-commit prefixes (`feat(engine):`, `fix(app):`, `docs:`, `chore:`).

## Python version

Lyrebird needs **Python 3.12 or newer** — mitmproxy 12 requires it. `.tool-versions` pins the
version this project is developed and tested against, so [asdf](https://asdf-vm.com) users get it
automatically:

```bash
asdf install          # reads .tool-versions
python3 -V            # 3.12.x
```
