# Lyrebird — menu-bar app

Native SwiftUI `MenuBarExtra` client for the Lyrebird engine — a thin client over the control API
on `:8088` and the `lyrebird` CLI. No proxy logic in Swift; the engine stays the single source of
truth.

## Build

The Xcode project is generated from `project.yml` via
[XcodeGen](https://github.com/yonaskolb/XcodeGen), so it isn't committed:

```bash
brew install xcodegen        # once
xcodegen generate            # writes Lyrebird.xcodeproj
xcodebuild -project Lyrebird.xcodeproj -scheme Lyrebird -configuration Debug build
```

Or open the generated `Lyrebird.xcodeproj` in Xcode.

The project builds **ad-hoc signed** (`CODE_SIGN_IDENTITY: "-"`, hardened runtime off), which is
fine for running it on your own machine. To distribute it, set `DEVELOPMENT_TEAM` and a Developer
ID identity in `project.yml`, enable `ENABLE_HARDENED_RUNTIME`, and notarize the result — a
system-proxy + CA-installing controller cannot run in the App Store sandbox, so Developer ID is the
conventional route.

## What it does

Menu-bar glyph: filled bird with a green dot while intercepting, orange when the proxy is up but
not intercepting, and an outlined bird with no dot when stopped.
Click for a session picker, Start/Stop (`lyrebird up|down`), a Relaunch-app button, recent traffic,
and settings. Dock-less agent (`LSUIElement`).

## Configuration

Settings holds the control URL, the `lyrebird` launcher path, and the **profile directory**. When
that is set the app passes `--profile` explicitly on every CLI call, because an app launched
from Finder inherits no shell environment — relying on `LYREBIRD_PROFILE` would silently select
the wrong profile. Left blank, the engine falls back to its own default.

The simulator bundle id to relaunch comes from the engine (`simBundleId` in your `profile.json`,
surfaced via `GET /__mock__/health`), so the app ships with no app identifier of its own. Relaunch
stays disabled until your profile sets one.

If the `lyrebird` path is unset, the app searches the inherited `PATH`, then `/usr/local/bin`,
`/opt/homebrew/bin`, `~/.local/bin` and `~/bin`. CLI failures are shown in the menu rather than swallowed.
