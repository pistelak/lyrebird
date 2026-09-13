# When it does not work

Symptoms of a run that is not doing what you meant, and what usually causes them; the contract
these belong to is [AGENTS.md](AGENTS.md).

| Symptom | Usual cause |
|---|---|
| `assert-answered` times out with 0 requests | App wasn't relaunched, or the host isn't in `profile.json` |
| Requests arrive but nothing matches | Path pattern wrong. `/recent` shows the real paths |
| `patchSkipped` in `/recent` | `patch` needs a JSON response from a live upstream; use `replace` if there isn't one |
| `overrun` in `/recent` | A sequence ran past its last step. Add steps, or set `onExhausted` |
| `sequence wait` fails at once | The step already went by — reset, then trigger the action |
| `assert-answered` fails but the screen looked right | The real backend served it. The rule never applied — that is the point of the command |
| `assert-answered --run` exits 3 | The run ended under the test — something reset the rule, replaced it, switched scenario, or took the port for another profile. Draw the boundary again and re-run the action; the assertion was never made |
| A rule matches more screens than you meant | `explain-match` on a sibling request — if it selects your rule, it is too broad |
| A rule you wrote in a scenario file is nowhere in `explain-match` | It was dropped at load. `lyrebird validate NAME` names the rule and the field |
| A whole scenario behaves as if it were empty | The file was refused whole — malformed JSON, or a `schemaVersion` this engine does not read. `lyrebird validate NAME` |
| A sequence is one step ahead | Something else called the endpoint. Narrow `match`, or use `advanceOn` |
| `up` fails on CA | No booted simulator, or the one named cannot trust a CA. Boot a usable iOS simulator and name it |
| `up` says N simulators are booted | Name the one you drive: `--simulator <udid>` (it lists them) |
| `up` says the default route moved | The Mac changed network service (Wi-Fi to Ethernet, say) under a live session. `lyrebird down && lyrebird up` — the old service gets its settings back before the new one is taken |
| The app rejects Lyrebird's certificate | The CA is on another device. `lyrebird down && lyrebird up --simulator <udid>` for the one under test |
| The app came back without the mock after a relaunch | It was relaunched on another device. `lyrebird relaunch` uses the one `up` recorded |
| 421 / 415 from the API | Missing `Host: 127.0.0.1:8088` or `Content-Type: application/json` — or just use the CLI |
| 409 `profile_mismatch` | Another profile's proxy holds the port. `lyrebird down` first, or pass the `--profile` that is running |
| `path escapes …` in the load problems | A scenario file resolves outside `scenarios/` or outside the profile — usually a symlink. Symlink the whole profile instead |
| A scenario file is on disk but the proxy does not have it | Same cause: reads are held to the same rule, so it is skipped at load. `lyrebird validate` names it |
| A scenario file added or moved by hand is not in the list | The engine reads the files once. `lyrebird scenario reload` re-reads them — it resets run evidence, so `reset` after it, not before |
| `scenarios nest one level deep` | `scenarios/a/b/c.json` is too deep to name. Scenarios live in `scenarios/` or one folder below it |
| 409 `reload_refused` | A file cannot be read whole, so nothing was published and the proxy still serves what it had. Fix the file it names, or `down && up` — startup is the lenient one |

## Things Lyrebird does not do

| You wanted | Instead |
|---|---|
| `wait-ready` | `assert-answered <id> --run R --timeout N` — it proves your rule answered, not merely that traffic arrived |
| `recent --matched` | `recent --json`, filtered on `matched` |
| `scenario mv` | Move the file, then `scenario reload --use NAME` |
| `scenario new` | Write the file, then `scenario reload --use NAME` |
| `scenario new --clone-from X` | Copy the file, then `scenario reload --use NAME` |
| `scenario rm` | Delete the file, then `scenario reload` (naming a survivor with `--use` if it was the active one) |
| `scenario list` | `status --json` lists the names; the menu-bar app shows them by folder |
| `override add` / `override clear` | Edit the file, then `scenario reload` |
| `trust-ca` on its own | `up --simulator X`; for another device, `down && up --simulator Y` |
| `untrust-ca` | `xcrun simctl keychain <udid> reset` clears the certificates added to that simulator |
| `logs` | The proxy log is `~/Library/Logs/Lyrebird/proxy.log`; `up` prints its last lines when the proxy fails to start |
| `recent --limit` | `recent --json \| jq` |
| A `status` that lists scenarios, sequences, the PAC and the simulator | `status --json` carries all four |
| A migration hint for a profile still keeping its scenarios in `sessions/` | Rename the directory to `scenarios/` |
| Sibling scenario names that differ only by case | On a case-folding filesystem they are one file; rename one before moving the profile to such a machine |
| `onExhausted: passThrough` | `repeatLast`, or add the steps the run actually makes |
| A remembered active scenario across restarts | The proxy starts on `default`; `up --use NAME` names it each time |

## The proxy is gone but the network still points at it

`down` restores the proxy settings that were there before, and nothing else does. There is no
watchdog: a proxy that dies — a crash, a `kill -9`, a machine that went to sleep badly — leaves the
Mac routing at a port with nothing behind it until you run `lyrebird down`. The disruption is wider
than one scenario misbehaving: anything honouring the system proxy can stall or fail, not only the
hosts in your profile.

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

- **`the PAC on 'X' is not this session's any more … — not restored`.** Something else set the PAC
  after `up` took it. Lyrebird never writes over a PAC it did not install, so it stops the proxy,
  prints the settings it recorded at `up` and keeps the journal. Put those back in System Settings ▸
  Network ▸ *service* ▸ Proxies and run `lyrebird down` again: it finds them in place, writes
  nothing and releases the session. To keep the PAC you set instead, delete
  `~/Library/Application Support/Lyrebird/session/session.json`.
- **`session.json cannot be read (…) — nothing was changed`.** The journal is there and this
  version will not guess at it. Move `~/Library/Application Support/Lyrebird/session/session.json`
  away, switch the routing off by hand with the two commands below, then run `lyrebird down` again.
- **`the proxy (pid N) could not be proved to be this session's`.** Lyrebird never signals a
  process it cannot prove is its own. The settings are already back; stop it by hand
  (`kill -9 N`) — see the clock step in the table below for the usual cause.

If `down` cannot run at all, switch the routing off yourself and check that it took:

```bash
networksetup -setautoproxystate "Wi-Fi" off
networksetup -getautoproxyurl "Wi-Fi"        # must now say: Enabled: No
```

Substitute your own network service for `"Wi-Fi"` — `networksetup -listallnetworkservices` lists
them. That leaves the stale URL sitting in the field, which is what an ordinary `down` leaves too:
macOS rejects an empty PAC URL, so when you had no PAC to begin with, "restored" means *disabled*.

The proxy log is `~/Library/Logs/Lyrebird/proxy.log`; `up` prints its last lines when the proxy
fails to start. Later failures (CA, PAC, relaunch) report their own reason instead.

## Run the old `down` before you upgrade

Run the *previous* version's `lyrebird down` before checking out a new one. It is the only version
that understands what it wrote, and the new one will not adopt it: a pre-protocol `runtime-*.json`
left under `~/Library/Application Support/Lyrebird/` is never read. A record written before this
protocol says nothing about whether its PAC is still installed, so restoring from it would put
settings back across a boundary nobody can see. Read it yourself, put those settings back by hand if
they are still wanted, then delete the file.

For the same reason, do not run two versions at once: an older one does not take the session lock,
so the two are not serialised against each other.

## What the session journal does not rule out

One thing is certain: the settings you had are written down *before* the PAC is touched, and one
authority — one session per user — decides who may put them back. These states remain possible
anyway, and knowing them saves guessing. Every one of them ends at the same command.

| Limit | Remedy |
|---|---|
| A foreign write landing inside a single `networksetup` command is overwritten without trace | Check System Settings ▸ Network ▸ *service* ▸ Proxies afterwards |
| A foreign write landing between two commands of one recipe | Same: the window is one command wide and nothing can see into it |
| Another login account's session — the journal and its lock are per user | Run `lyrebird down` as that user |
| A system clock step of a second or more makes a live process unidentifiable | `down` restores the settings and refuses to signal the proxy; `kill -9 <pid>`, then `lyrebird down` |
| A second `up` while a session exists is refused, even for the same profile | `lyrebird use NAME && lyrebird relaunch`, or `lyrebird down` first |
| The Mac's default route moves to another service mid-session (hot migration) | `lyrebird down && lyrebird up` |
| A network service deleted and recreated on the same device is treated as the same service | Nothing to do; the PAC is restored to the device that carries it |
| `kill -9` of the proxy, or a proxy that dies on its own — nothing restores the settings | `lyrebird down` |
| Power loss inside the window between the journal write and the PAC write | `lyrebird down`; if the journal did not survive, the two `networksetup` lines above |
| A crash between a restore's read-back and the journal's removal | `lyrebird down` again: it restores to the same recorded baseline and releases the journal |
| A damaged `session.json` — `down` refuses rather than guess | Move it away, `networksetup -setautoproxystate <service> off`, then `lyrebird down` |
| A pre-protocol `runtime-*.json` from an older Lyrebird | Run that version's `down` first; this one never imports it |
| Two versions running at once — an older one does not take the session lock | Do not; run the old `down` before upgrading |
| An `up` killed in the instant it is spawning the proxy leaves a process no journal names | Stop it by hand (`kill -9 <pid>`) |
| A proxy still running that no journal records at all | Same: stop it by hand |
| A second Lyrebird session during an acceptance run | The acceptance check refuses to start over one; `lyrebird down` first |
