# Operating Lyrebird from an agent

Lyrebird is designed to be driven by a coding agent as much as by a person. Most of its use is a
machine putting an app into a specific backend state, checking something, and putting the machine
back the way it found it.

This file is the contract for that. It assumes you can run shell commands and read JSON.

## The loop

```bash
lyrebird --profile PATH up          # start; non-zero if it did not achieve interception
lyrebird --profile PATH use NAME    # activate a saved scenario
# relaunch the app under test
lyrebird --profile PATH wait-ready --match --timeout 30
# …do the work you came to do…
lyrebird --profile PATH down        # restores the proxy settings that were there before
```

Always pass `--profile` explicitly. `LYREBIRD_PROFILE` works too, but an explicit path is one less
thing to be wrong about when something misbehaves later.

## Five things worth knowing before you start

**1. Exit codes mean the postcondition, not "the command ran."**

`up` exits non-zero if the CA could not be trusted, if no network service was found, or if the app
could not be relaunched — all cases where the proxy is running but you are *not* mocking anything.
Do not treat a zero exit from `up` as optional to check.

**2. `wait-ready` alone does not prove your rule works.**

Without `--match` it returns as soon as *any* request reaches the proxy. That proves routing works.
It does not prove your override matched — which is usually the thing you actually want to know.

```bash
lyrebird --profile PATH wait-ready --match --timeout 30
# ✓ override ovr_9a99bd matched GET /api/v1/orders/42 → 500
```

If it times out, it tells you how many requests *did* arrive, which distinguishes "the app isn't
talking to us" from "your path pattern is wrong".

**3. `status --json` is the state query.**

```bash
lyrebird --profile PATH status --json
```

```json
{
  "proxyUp": true,
  "intercepting": true,
  "activeSession": "orders-outage",
  "overrideCount": 1,
  "sessions": ["default", "orders-outage"],
  "simBundleId": "com.example.Store",
  "profile": "/path/to/profile",
  "service": "Wi-Fi",
  "pac": { "url": "http://127.0.0.1:8088/proxy.pac", "enabled": true, "ours": true },
  "dashboard": "http://127.0.0.1:8088"
}
```

Exit code is 0 only when the proxy is up **and** intercepting, so
`lyrebird status > /dev/null` works as a readiness check on its own. `--json` selects the output
format and nothing else — both forms exit the same way, so `lyrebird status && …` is safe to
write either way round.

**4. Relaunch the app after `up`, every time.**

`URLSession` caches the proxy configuration it saw at launch. An app that was already running will
ignore Lyrebird completely, with no error anywhere — it will just quietly talk to the real backend.
Set `simBundleId` in the profile and `up` handles it.

**5. `down` is not optional.**

It restores the proxy settings that were there before. Run it even on your failure paths. If your
process is killed before it can, a watchdog restores them within a couple of seconds — but do not
rely on that as the normal path.

## Making a scenario

Two options, and the second is usually the right one for an agent.

**Edit a session file.** Sessions are JSON in `<profile>/sessions/`. `id` and `active` are optional
— `id` is derived from the rule when omitted — so the minimum is:

```json
{
  "name": "orders-outage",
  "overrides": [
    { "match": { "method": "GET", "path": "/api/v1/orders/*" },
      "mode": "replace", "status": 500, "body": { "error": "boom" } }
  ]
}
```

Then `lyrebird use orders-outage`. Files are picked up when the proxy starts.

**Or add one from the CLI,** which takes effect immediately with no restart:

```bash
lyrebird override add '{"match":{"method":"GET","path":"/api/v1/orders/*"},"mode":"replace","status":500}'
lyrebird override add -   # or read the JSON from stdin
```

And to see what happened:

```bash
lyrebird recent --json --matched     # only requests an override answered
lyrebird recent                      # all of it, human-readable
```

These call the same HTTP control API, so you never need to construct the requests yourself. If you
do call it directly, note two requirements that otherwise surface as a bare 4xx: the `Host` header
must be `127.0.0.1`, `localhost` or `[::1]` with the control port (**421** if not), and any
POST/PUT/PATCH/DELETE with a body must send
`Content-Type: application/json` (**415** if not). Both exist to stop a web page you happen to have
open from driving the proxy. `engine/README.md` has the endpoint list.

## Working on a scenario without disturbing anyone

A profile is shared state. If it belongs to a person or a team, do not edit their sessions.

```bash
lyrebird status --json                                  # note activeSession before you touch anything
lyrebird session new agent-scratch --clone-from orders-outage   # creates and activates
# …work…
lyrebird use orders-outage                              # put back what you found
lyrebird session rm agent-scratch
```

`--clone-from` fails if the source does not exist rather than quietly giving you an empty session,
so a typo surfaces immediately instead of as a scenario that mysteriously does nothing.

## The destructive operations

`lyrebird override clear --force` (and `DELETE /__mock__/overrides`) deletes every override in the
**active session** and rewrites the file on disk. There is no undo.

It is not the only thing that writes: `session rm` deletes a file, and `override add` replaces a
rule with the same id. But it is the only one that discards everything at once, which is why it
is the only one behind a flag — `clear` is easy to reach for while meaning "clear the traffic
list", which is not what it does. Creating or importing a session that already exists is refused
rather than silently replacing it. If the profile is under version control that is your safety
net; if not, take a copy before touching someone else's sessions.

## When it does not work

| Symptom | Usual cause |
|---|---|
| `wait-ready` times out with 0 requests | App wasn't relaunched, or the host isn't in `profile.json` |
| Requests arrive but nothing matches | Path pattern wrong. `/recent` shows the real paths |
| `patchSkipped` in `/recent` | `patch` needs a JSON response from a live upstream; use `replace` if there isn't one |
| `up` fails on CA | No booted simulator. Boot one first |
| 421 / 415 from the API | Missing `Host: 127.0.0.1:8088` or `Content-Type: application/json` — or just use the CLI |

`lyrebird logs` prints the last 60 lines of the proxy log and writes the path to stderr, so
`tail -f "$(lyrebird logs 2>&1 >/dev/null)"` follows it. When the proxy itself fails to start, `up` prints the last
lines for you; later failures (CA, PAC, relaunch) report their own reason instead.

## Do not

- **Point it at production.** Development and simulators only.
- **Leave interception on.** Run `down`.
- **Commit a profile** into this repository, or any public one. Sessions can hold real payloads.
- **Assume no output means success.** Check exit codes; they are meaningful.
