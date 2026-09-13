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
| `up` says the default route moved | The Mac changed network service (Wi-Fi to Ethernet, say) under a live session. `lyrebird down && lyrebird up` — the old service gets its settings back before the new one is taken |
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

The command is `lyrebird down`, and it needs nothing at all to find the session:

```bash
lyrebird down          # from anywhere: no --profile, no port, no directory
```

There is one PAC-owning session per user, recorded in
`~/Library/Application Support/Lyrebird/session/session.json`, and that record — not a control port,
not the profile you started from — is what `down` restores from. A profile that has been deleted, a
shell in another directory, a port you no longer remember: none of them change the answer. Run it
from the checkout as `bin/lyrebird down` if you have no `lyrebird` on `PATH`.

Exit 0 means your settings are back. Exit 1 means they are not, and the reason is printed. The ones
worth recognising:

- **`this session was archived …; its previous settings were never put back`.** The PAC on the
  service was no longer this session's — something else set one, or the network service is gone — so
  `down` refused to overwrite it and preserved the baseline instead, under
  `~/Library/Application Support/Lyrebird/session/archive/`. It prints the file's path; inside, the
  `baseline` object holds the `url` and `enabled` you had. System Settings ▸ Network ▸ *service* ▸
  Proxies is where they go back, and nothing is going to do it for you.
- **`this session's processes could not be checked`**, or **`the watchdog (pid N) could not be
  checked`.** The recorded proxy or watchdog cannot be confirmed to be the process the journal
  means, and Lyrebird never signals a process it cannot prove is its own. Nothing was changed and
  the journal is kept — see the clock step below for the usual cause.
- **`a Lyrebird answers on port N and no session owns it — left alone`.** A proxy from a run whose
  journal is gone. It is answering, so `down` leaves it: stop it yourself if it is yours, then
  `lyrebird down` again.

If `down` cannot run at all, switch the routing off yourself and check that it took:

```bash
networksetup -setautoproxystate "Wi-Fi" off
networksetup -getautoproxyurl "Wi-Fi"        # must now say: Enabled: No
```

Substitute your own network service for `"Wi-Fi"` — `networksetup -listallnetworkservices` lists
them. That leaves the stale URL sitting in the field, which is what an ordinary `down` leaves too:
macOS rejects an empty PAC URL, so when you had no PAC to begin with, "restored" means *disabled*.

`lyrebird logs` prints the last 60 lines of the proxy log and writes the path to stderr, so
`tail -f "$(lyrebird logs 2>&1 >/dev/null)"` follows it. When the proxy itself fails to start, `up` prints the last
lines for you; later failures (CA, PAC, relaunch) report their own reason instead.

## Run the old `down` before you upgrade

Run the *previous* version's `lyrebird down` before checking out a new one. It is the only version
that understands what it wrote, and the new one will not adopt it: a pre-protocol `runtime-*.json`
left under `~/Library/Application Support/Lyrebird/` is **printed** by `down` — the service and the
previous PAC it names — and never imported. A record written before this protocol says nothing
about whether its PAC is still installed, so restoring from it would put settings back across a
boundary nobody can see. Put them back by hand if they are still wanted, then delete the file.

For the same reason, do not run two versions at once: an older one does not take the session lock,
so the two are not serialised against each other.

## What the session journal does not rule out

One thing is now certain: the settings you had are written down durably *before* the PAC is
touched, and one authority decides who may put them back. These states remain possible anyway, and
knowing them saves guessing.

- An enabled PAC at a dead port after a `kill -9` that takes the proxy *and* the watchdog. Nothing
  is left running to notice, so it persists until the next command; `lyrebird down` clears it.
- A PAC set by hand mid-run supersedes the baseline. `down` archives the session and exits 1 rather
  than overwrite what you set.
- A baseline URL set by hand with the other enabled flag is restored to the flag that was recorded:
  it is indistinguishable from a restore that was interrupted.
- Another login account's session is outside the guarantee — the journal and its lock are per user.
- A foreign write landing inside a single `networksetup` command is overwritten without trace. The
  window is one command wide and nothing can see into it.
- A network service deleted and recreated on the same device is treated as the same service.
- An `up` killed in the instant it is spawning the proxy can leave a process no journal names. A
  `down` at exactly that instant cannot see it and exits 1 saying so; the next `up` refuses over it,
  and a `down` after that sweeps it up.
- **A system clock step of a second or more** makes a live process unidentifiable. A process is
  identified by its pid *and* its creation time, read in wall-clock seconds, and a clock step moves
  that reading — so the value the journal recorded and the value read back afterwards disagree for
  the very same process. That is doubt, not proof of death: a pid reused since would look exactly
  the same, and doubt acts on nothing. `down` therefore refuses to signal that process, says so,
  and keeps the journal. Stop it by hand — `session.json`'s `phase` names the pids — and run
  `lyrebird down` again.
