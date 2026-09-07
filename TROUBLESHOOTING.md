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

`lyrebird logs` prints the last 60 lines of the proxy log and writes the path to stderr, so
`tail -f "$(lyrebird logs 2>&1 >/dev/null)"` follows it. When the proxy itself fails to start, `up` prints the last
lines for you; later failures (CA, PAC, relaunch) report their own reason instead.
