# When it does not work

Symptoms of a run that is not doing what you meant, and what usually causes them; the contract
these belong to is [AGENTS.md](AGENTS.md).

| Symptom | Usual cause |
|---|---|
| `wait-ready` times out with 0 requests | App wasn't relaunched, or the host isn't in `profile.json` |
| Requests arrive but nothing matches | Path pattern wrong. `/recent` shows the real paths |
| `patchSkipped` in `/recent` | `patch` needs a JSON response from a live upstream; use `replace` if there isn't one |
| `overrun` in `/recent` | A sequence ran past its last step. Add steps, or set `onExhausted` |
| `sequence wait` fails at once | The step already went by — reset, then trigger the action |
| `assert-answered` fails but the screen looked right | The real backend served it. The rule never applied — that is the point of the command |
| `assert-answered` lists paths you did not expect | The app went somewhere else; the matcher is probably fine |
| `assert-answered --run` exits 3 | The run ended under the test — something reset the rule, replaced it, switched scenario, or took the port for another profile. Draw the boundary again and re-run the action; the assertion was never made |
| A rule matches more screens than you meant | `explain-match` on a sibling request — if it selects your rule, it is too broad |
| A rule you wrote in a scenario file is nowhere in `explain-match` | It was dropped at load. `lyrebird validate NAME` names the rule and the field |
| A whole scenario behaves as if it were empty | The file was refused whole — malformed JSON, or a `schemaVersion` this engine does not read. `lyrebird validate NAME` |
| A sequence is one step ahead | Something else called the endpoint. Narrow `match`, or use `advanceOn` |
| `up` fails on CA | No booted iOS simulator (a booted watch is not one). Boot one first |
| `up` or `trust-ca` says N simulators are booted | Name the one you drive: `--simulator <udid>` (it lists them) |
| The app rejects Lyrebird's certificate | The CA is on another device. Re-run `trust-ca --simulator <udid>` for the one under test |
| The app came back without the mock after a relaunch | It was relaunched on another device. `lyrebird relaunch` uses the one `up` recorded |
| 421 / 415 from the API | Missing `Host: 127.0.0.1:8088` or `Content-Type: application/json` — or just use the CLI |
| 409 `profile_mismatch` | Another profile's proxy holds the port. `lyrebird down` first, or pass the `--profile` that is running |
| `path escapes …` when changing scenarios or overrides (API: 400 `invalid_name`) | A scenario file resolves outside `scenarios/` or outside the profile — usually a symlink. Symlink the whole profile instead |
| A scenario file is on disk but the proxy does not have it | Same cause: reads are held to the same rule, so it is skipped at load. `lyrebird validate` names it |
| A scenario file added or moved by hand is not in the list | The engine reads the files once. `lyrebird scenario reload` re-reads them — it resets run evidence, so `reset` after it, not before |
| `scenarios nest one level deep` | `scenarios/a/b/c.json` is too deep to name. Scenarios live in `scenarios/` or one folder below it |
| `directory symlinks under scenarios/ are not read` | A folder in `scenarios/` is a symlink. It would serve one folder's scenarios under two names, so it is refused even when it points inside the profile |
| `names differ only by case` | Two siblings that a case-folding filesystem cannot tell apart. Neither loads; rename one |
| 409 `reload_refused` | A file cannot be read whole, so nothing was published and the proxy still serves what it had. Fix the file it names, or `down && up` — startup is the lenient one |
| `changed on disk since it was loaded` when moving | The file was edited after the engine read it. `lyrebird scenario reload`, then move |

## The proxy is gone but the network still points at it

`down` restores the proxy settings that were there before, and a watchdog process does it on your
behalf if the proxy dies on its own — so killing the proxy alone is normally recovered from. What
is not recovered is losing *both*: a power cut, a `kill -9` that takes the watchdog with it, or a
`networksetup` that failed. Then the Mac is left routing at a port with nothing behind it, and the
disruption is wider than one scenario misbehaving — anything honouring the system proxy can stall
or fail, not only the hosts in your profile.

`down` does not need a live proxy; it restores from what the run recorded. Try it first, from the
checkout if you have no `lyrebird` on `PATH`. If the profile it was using is gone, the installed
PAC still names the control port, which is enough to find the rest:

```bash
networksetup -getautoproxyurl "Wi-Fi"        # → URL: http://127.0.0.1:8088/proxy.pac
LYREBIRD_CONTROL_PORT=8088 lyrebird down     # use the port that URL actually shows
```

Substitute your own network service for `"Wi-Fi"` — `networksetup -listallnetworkservices` lists
them. If `down` itself fails, switch the routing off yourself and check that it took:

```bash
networksetup -setautoproxystate "Wi-Fi" off
networksetup -getautoproxyurl "Wi-Fi"        # must now say: Enabled: No
```

That leaves the stale URL sitting in the field, which is what an ordinary `down` leaves too: macOS
rejects an empty PAC URL, so when you had no PAC to begin with, "restored" means *disabled*.

`lyrebird logs` prints the last 60 lines of the proxy log and writes the path to stderr, so
`tail -f "$(lyrebird logs 2>&1 >/dev/null)"` follows it. When the proxy itself fails to start, `up` prints the last
lines for you; later failures (CA, PAC, relaunch) report their own reason instead.
