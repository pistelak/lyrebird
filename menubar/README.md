# Lyrebird — menu-bar app

Native AppKit client for the Lyrebird engine — a thin client over the control API
on `:8088` and the `lyrebird` CLI. No proxy logic in Swift; the engine stays the single source of
truth.

Use it to browse scenarios prepared by a coding agent, inspect configured responses and sequences,
and choose which scenario to activate while testing your iOS app.

## Build

The Xcode project is generated from `project.yml` via
[XcodeGen](https://github.com/yonaskolb/XcodeGen), so it isn't committed:

```bash
# From the repository root:
make setup-app
make check-app              # formatting/lint, build, unit tests, fixture build
```

To run it day to day, install one copy and launch that:

```bash
make install-app            # Release build into /Applications, replacing the old one, then open
```

It quits a running Lyrebird, replaces the installed bundle with the one it just built, and launches
it. It exists because every Xcode build is a launchable `.app` that Spotlight also lists as
"Lyrebird", with nothing to say which is current; this is the one that is.

Or open the generated `menubar/Lyrebird.xcodeproj` in Xcode.
See [CONTRIBUTING.md](../CONTRIBUTING.md) for pinned tools and formatter setup.

### Release versioning

`project.yml` sets `MARKETING_VERSION: "0.0.0"` and `CURRENT_PROJECT_VERSION: "0"` as development
placeholders, and the Info.plist references both as `$(…)` rather than holding literals. A version
that reads `0.0.0` is therefore a build nobody stamped — not a release. To stamp one, pass both on
the `xcodebuild` command line and read the result back out of the bundle (from `menubar/`):

```bash
MARKETING=1.2.3 BUILD=42 && \
xcodebuild -project Lyrebird.xcodeproj -scheme Lyrebird \
  -configuration Release -derivedDataPath .build -destination 'platform=macOS' \
  MARKETING_VERSION="$MARKETING" CURRENT_PROJECT_VERSION="$BUILD" build && \
scripts/verify-version.sh .build/Build/Products/Release/Lyrebird.app "$MARKETING" "$BUILD"
```

The verify line is not optional politeness: `xcodebuild` accepts settings it does not use, so an
override that never reaches the plist produces a green build and a mis-versioned app. The script
only reads the bundle — a signed app cannot have its Info.plist patched after the fact.

The project builds **ad-hoc signed** (`CODE_SIGN_IDENTITY: "-"`, hardened runtime off), which is
fine for running it on your own machine. To distribute it, set `DEVELOPMENT_TEAM` and a Developer
ID identity in `project.yml`, enable `ENABLE_HARDENED_RUNTIME`, and notarize the result — a
system-proxy + CA-installing controller cannot run in the App Store sandbox, so Developer ID is the
conventional route.

## Menu-bar controls

Menu-bar glyph: filled bird with a green dot while intercepting, orange when the proxy is up but
not intercepting, and an outlined bird with no dot when stopped.
Click for a scenario menu, Start/Stop (`lyrebird up|down`), a Relaunch app command, the requests
the proxy has seen, and settings.

Lyrebird is a regular app: a Dock icon and a menu bar extra, both from launch. **Show in Dock only
while a window is open** in Settings puts it in the menu bar instead, with the Dock icon appearing
only while the window is. ⌘Q quits the app and removes the extra; it leaves a running proxy running,
exactly as Quit in the menu does.

## Scenario browser

Browse scenarios prepared by your coding agent and inspect the responses and transitions they
configure. **Scenarios** opens a three-column browser: scenarios and Recent in the sidebar, configured
requests or recorded traffic in the middle, and details on the right. A single click browses a
scenario; double-clicking activates it. Activation is also available from the context menu.
The sidebar marks the active scenario, and the toolbar shows the interception status.

Sequence rules appear in configured order, with each response state followed by its advancing
request. These are configured transitions, not a traffic trace: reads can repeat, and an advance
matcher may have different conditions from the rule that answers that request. The detail pane
shows those conditions and any candidate response rules. Other rules are listed separately.
Scenario notes provide context above the list.

**Recent** shows recorded requests newest first, with status and override/sequence metadata.
Request and response bodies are not captured. **Clear** removes recent traffic only; it leaves
rules, sequence progress and answer counters intact. Clearing requires an engine that supports
`DELETE /__mock__/recent`.

Scenarios that live in a folder appear under it, one section per folder, rows showing the name
without the folder and the full name in the tooltip. **Reload from disk** in the toolbar re-reads
the scenario files, for a profile edited outside the app; it refuses whole if any file cannot be
read, and it resets run evidence.

The menu lists only the folder the active scenario is in — the set one run is about — or the root
scenarios when the active one is at the root. An active scenario missing from the list is said to be
missing rather than shown as an empty profile. This window is where the whole profile is; switching
to a scenario in another folder happens here, or with `lyrebird use <folder>/<name>`.

The browser reads the engine's rule descriptions through `GET /__mock__/rules`. It distinguishes
failed reads from empty results, preserves selection across polls, and does not edit rules.

## Configuration

Settings holds the control URL, the `lyrebird` launcher path, and the **profile directory**. When
that is set the app passes `--profile` explicitly on every CLI call, because an app launched
from Finder inherits no shell environment — relying on `LYREBIRD_PROFILE` would silently select
the wrong profile. Left blank, the engine falls back to its own default.

One proxy holds the control port, so the menu says which profile it means on every call it makes.
It learns that profile's fingerprint from `lyrebird status --json` — at launch and again when the
Settings are saved — and never computes it: the fingerprint is the engine's own
`sha256(profile dir)[:12]`, and with the profile left blank the app cannot even see which directory
the engine picked. It travels as `X-Lyrebird-Profile`, the header the control API compares against
the running profile. A proxy running someone else's profile is then shown as exactly that, with
that profile's fingerprint and no scenario list, traffic or Relaunch button borrowed from it; a
proxy that answers something unreadable is shown as unreadable rather than as stopped. If the
fingerprint cannot be established at all — a wrong launcher path, usually — the menu says the
profile is unknown and sends nothing: an unscoped request is answered by whichever profile holds
the port, which is the reading this is here to avoid. Start and Stop keep working in all of those,
since the CLI does its own checking.

The simulator bundle id to relaunch comes from the engine (`simBundleId` in your `profile.json`,
surfaced via `GET /__mock__/health`), so the app ships with no app identifier of its own. Relaunch
stays disabled until your profile sets one.

The app has no simulator picker: Start runs a bare `lyrebird up`, so the CLI's default applies —
the booted iOS simulator when exactly one is booted, and a refusal that names the booted devices
when there are several. Relaunch runs `lyrebird relaunch <bundleid>`, which uses the device that
`up` recorded, so the button cannot relaunch the app on a simulator that never received the CA;
it shows the CLI's own refusal when there is no single device it can mean. Nothing in the app
names simctl's `booted`. To bind a run to a particular device, start it from the CLI with
`lyrebird up --simulator <udid>` (see [AGENTS.md](../AGENTS.md)) — the button then follows it.

If the `lyrebird` path is unset, the app searches the inherited `PATH`, then `/usr/local/bin`,
`/opt/homebrew/bin`, `~/.local/bin` and `~/bin`. CLI failures are shown in the menu rather than swallowed.

## Native window implementation

The browser uses an `NSWindowController`, `NSSplitViewController`, `NSOutlineView` sidebar,
`NSTableView` request list and selectable `NSTextView` detail. The sidebar background extends
through the native toolbar; safe-area constraints keep list content and headers below the window
controls. JSON wraps within the detail pane; Copy body/patch preserves the original JSON formatting. The menu-bar item and Settings window also use AppKit. No SwiftUI hosting remains.

Settings changes apply together when Save is pressed; Cancel leaves the configuration unchanged.
The browser keeps selection and scroll positions across unchanged polls. Selecting a scenario
browses it; double-click or Activate scenario in its context menu makes it active.

Debug builds use a separate bundle identifier and preferences from the installed Release app,
so UI automation can run while the installed app is being tried. The tests launch Debug with
`--preview` (and optionally `--dark`), using a synthetic in-memory control API and separate
preview settings. This preview is excluded from Release builds. It loads no profile and its
launcher path is deliberately nonexistent.

`make test-app` runs model/controller tests and native UI automation, including navigation, window
reopening and full screen. UI automation requires an unlocked macOS desktop. Synthetic light/dark
screenshots are attached to its Xcode test result.
