# Contributing

## Setup

```bash
cd engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

For the menu-bar app: `brew install xcodegen && cd menubar && xcodegen generate`.

## Before opening a pull request

From the repository root. Each line is a subshell, so neither depends on the other's directory:

```bash
(cd engine && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy && .venv/bin/python -m pytest tests/ -q)
(cd menubar && xcodegen generate && xcodebuild -project Lyrebird.xcodeproj -scheme Lyrebird build)
```

CI runs both.

## What lands on `main` and what goes through a pull request

Scale the landing to the change:

- **Directly on `main`:** docs, comments, and small fixes — with both check
  lines above green, and after the independent review below for anything
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

**Never commit a profile.** Profiles hold the hosts you intercept and the payloads you saved, so
they live outside this repository. A session file can contain a real response body in full; see
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
"stopped" with the proxy still running, a session silently created empty when the thing it was
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
by the tests, because the tests covered the happy path. `engine/tests/test_cli.py` is almost
entirely failure paths — an unreadable runtime file, a foreign PAC, nothing to stop — and that is
the model to copy.

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
