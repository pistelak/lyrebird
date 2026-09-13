# Security

Lyrebird decrypts TLS traffic and changes your Mac's network configuration. That is its purpose,
so it is worth being explicit about what it does and what it does not defend against.

## Reporting a vulnerability

Please report privately using
[GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository, rather than opening a public issue.

## What Lyrebird does to your machine

- **Installs a CA in one simulator.** `lyrebird up` generates a CA under
  `~/Library/Application Support/Lyrebird/mitmproxy/` and adds it to **one simulator's** keychain
  via `xcrun simctl keychain <udid> add-root-cert`. The device is the booted one when exactly one
  is booted, and otherwise the one you name with `--simulator`; `up` refuses rather than let
  simctl choose among several booted devices, so the CA never lands somewhere you did not pick.
  It is never added to your system keychain. It is Lyrebird's own CA, not the shared
  `~/.mitmproxy` one, so trusting Lyrebird does not widen trust for other mitmproxy tooling.
  `simctl` has no remove-one-certificate command: `xcrun simctl keychain <udid> reset` clears
  every added certificate from that simulator, or erase the device. To rotate the CA, run
  `lyrebird down` first, delete the directory, then `up` — a running proxy is reused, so the CA
  is only regenerated when mitmdump restarts.
  Trusting the CA on one device does not confine interception to it: routing is per network
  service and per hostname — see the PAC below — so every simulator on the Mac, and the Mac
  itself, goes through the proxy for the profile's hosts. What differs per device is only whether
  it trusts the CA, and neither answer leaves that device's traffic alone;
  [engine/README.md — Which simulator](engine/README.md#which-simulator) says what each one gets.
- **Changes your active network service's proxy settings.** It installs a PAC pointing at the local
  proxy. Your previous PAC URL and enabled state go into a per-user session journal under
  `~/Library/Application Support/Lyrebird/session/`, written *before* the PAC is touched, and
  `lyrebird down` restores them from it. One thing is guaranteed by that: there is one PAC-owning
  session per user, taken under a lock, so two runs cannot both snapshot the PAC and both install —
  the failure that left a Mac "restored" to a PAC that was already Lyrebird's. A PAC that is not
  this session's is never overwritten: `down` prints the settings it recorded, keeps the journal and
  exits non-zero, and putting them back is then yours to do.
  What is *not* guaranteed: **nothing restores the settings automatically.** A proxy that dies —
  `kill -9`, a crash, a power cut — leaves the PAC pointing at a dead port until `lyrebird down`
  runs. Nor is a `networksetup` that fails; nor a PAC changed by hand mid-run, which supersedes the
  baseline; a foreign write inside a single `networksetup` command is overwritten without trace; and
  another login account's session is outside all of it. `lyrebird status` reports the true state,
  `lyrebird down` is the recovery command, and System Settings ▸ Network ▸ *service* ▸ Proxies is
  the manual fix.
  [TROUBLESHOOTING.md](TROUBLESHOOTING.md#what-the-session-journal-does-not-rule-out) has the full
  list.
- **Decrypts TLS only for the hosts you list** in `profile.json`, matched exactly. Subdomains are
  not implied. An empty list intercepts nothing; a malformed profile aborts startup rather than
  falling back to a default.

## The control API

The admin API is **unauthenticated** and bound to loopback. Loopback alone is not
sufficient protection, because a web page you visit can issue cross-origin requests to
`127.0.0.1`, and DNS rebinding can make a hostile origin appear same-origin. Three checks close
that:

- the `Host` header must be a known loopback name and our port;
- a cross-origin `Origin` is refused;
- any state-changing request with a body must declare `Content-Type: application/json` — aiohttp's
  `request.json()` ignores Content-Type, so without this a `text/plain` form post would reach the
  API with no CORS preflight.

Scenario names arrive from that API and become filesystem paths, so they are
validated as single path components and the resolved path is confirmed to stay inside the profile
before any read, write, listing or unlink. This constrains *names supplied through the API*. It
does not sandbox the profile directory itself: scenario files found at startup are read from
wherever `--profile` points, symlinks included, exactly as you told it to.

**Not defended against:** other processes running as your user. Any local process can reach the
control API and drive the proxy. If that matters in your environment, do not run Lyrebird there.

## Captured data

- **Response bodies are never recorded.** `GET /__mock__/recent` stores time, method, host, path,
  status, which override matched, and any delay or patch-skip note. Nothing it keeps contains a
  request or response body.
- **Your profile is private data.** Scenarios can contain real payloads captured from a real
  backend, and `/proxy.pac` contains every hostname you intercept. Before attaching
  `/recent`, `/overrides`, `/scenarios` output or a PAC file to a public issue, check what is in
  them. Scenario files written by Lyrebird are `0600`; the examples `lyrebird init` copies keep the
  mode they ship with (`0644`) until something rewrites them.

## Scope

Lyrebird is a development tool for simulators. Do not point it at production traffic, do not run it
on a shared or multi-user machine, and do not leave interception enabled when you are finished —
`lyrebird down` restores your networking.
