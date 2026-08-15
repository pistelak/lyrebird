# Security

Lyrebird decrypts TLS traffic and changes your Mac's network configuration. That is its purpose,
so it is worth being explicit about what it does and what it does not defend against.

## Reporting a vulnerability

Please report privately using
[GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository, rather than opening a public issue.

## What Lyrebird does to your machine

- **Installs a CA in the simulator.** `lyrebird up` generates a CA under
  `~/Library/Application Support/Lyrebird/mitmproxy/` and adds it to the **booted simulator's**
  keychain via `xcrun simctl keychain booted add-root-cert`. It is never added to your system
  keychain. It is Lyrebird's own CA, not the shared `~/.mitmproxy` one, so trusting Lyrebird does
  not widen trust for other mitmproxy tooling.
  `simctl` has no remove-one-certificate command: `xcrun simctl keychain booted reset` clears
  every added certificate from the booted simulator, or erase the device. To rotate the CA, run
  `lyrebird down` first, delete the directory, then `up` — a running proxy is reused, so the CA
  is only regenerated when mitmdump restarts.
- **Changes your active network service's proxy settings.** It installs a PAC pointing at the local
  proxy. Your previous PAC URL and enabled state are recorded and restored by `lyrebird down`, and
  by the watchdog if the proxy dies. A PAC that isn't Lyrebird's is left untouched.
  This is best-effort, not a guarantee: if the watchdog is itself killed, the machine loses power,
  or `networksetup` fails, the PAC can be left pointing at a dead port. `lyrebird status` reports
  the true state, and System Settings ▸ Network ▸ *service* ▸ Proxies is the manual fix.
- **Decrypts TLS only for the hosts you list** in `profile.json`, matched exactly. Subdomains are
  not implied. An empty list intercepts nothing; a malformed profile aborts startup rather than
  falling back to a default.

## The control API

The admin API and dashboard are **unauthenticated** and bound to loopback. Loopback alone is not
sufficient protection, because a web page you visit can issue cross-origin requests to
`127.0.0.1`, and DNS rebinding can make a hostile origin appear same-origin. Three checks close
that:

- the `Host` header must be a known loopback name and our port;
- a cross-origin `Origin` is refused;
- any state-changing request with a body must declare `Content-Type: application/json` — aiohttp's
  `request.json()` ignores Content-Type, so without this a `text/plain` form post would reach the
  API with no CORS preflight.

Session, preset and operation names arrive from that API and become filesystem paths, so they are
validated as single path components and the resolved path is confirmed to stay inside the profile
before any read, write, listing or unlink. This constrains *names supplied through the API*. It
does not sandbox the profile directory itself: session files found at startup are read from
wherever `--profile` points, symlinks included, exactly as you told it to.

**Not defended against:** other processes running as your user. Any local process can reach the
control API and drive the proxy. If that matters in your environment, do not run Lyrebird there.

## Captured data

- **Response bodies are never recorded.** `GET /__mock__/recent` stores time, method, host, path,
  status, which override matched, and any delay or patch-skip note. Nothing it keeps contains a
  request or response body.
- **Your profile is private data.** Sessions can contain real payloads captured from a real backend,
  and `/proxy.pac` contains every hostname you intercept. Before attaching dashboard screenshots,
  `/recent`, `/overrides`, `/sessions` output or a PAC file to a public issue, check what is in
  them. Session files written by Lyrebird are `0600`; the examples `lyrebird init` copies keep the
  mode they ship with (`0644`) until something rewrites them.

## Scope

Lyrebird is a development tool for simulators. Do not point it at production traffic, do not run it
on a shared or multi-user machine, and do not leave interception enabled when you are finished —
`lyrebird down` restores your networking.
