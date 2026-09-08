#!/usr/bin/env bash
# Build the menu-bar app as Release from the current checkout and put it in /Applications (or the
# directory given as $1), replacing the copy there, then launch it.
#
# One installed copy, so Spotlight finds one "Lyrebird" — every Xcode build of the project used to
# leave a launchable .app under DerivedData, and Spotlight listed all of them with nothing to tell
# which was current. The build number is the commit count, so About says which commit this is.
set -euo pipefail

dest="${1:-/Applications}"
if [[ ! -f menubar/project.yml ]]; then
  echo "install-app: run from the repository root" >&2
  exit 2
fi
root="$PWD"
# Absolute before any `cd`: a relative APP_INSTALL_DIR would otherwise land under menubar/, replace
# a Lyrebird.app there, and exit 0 with the intended install untouched.
if ! dest="$(cd "$dest" 2>/dev/null && pwd)"; then
  echo "install-app: install directory does not exist: ${1:-/Applications}" >&2
  exit 2
fi
build="$(git rev-list --count HEAD)"

cd "$root/menubar"
"$root/.build/tools/bin/xcodegen" generate
xcodebuild -quiet -project Lyrebird.xcodeproj -scheme Lyrebird -configuration Release \
  -derivedDataPath .build -destination 'platform=macOS' CURRENT_PROJECT_VERSION="$build" build
built="$root/menubar/.build/Build/Products/Release/Lyrebird.app"
if [[ "$dest" == "$(dirname "$built")" ]]; then
  echo "install-app: install directory is the build products directory; refusing to replace the build with itself" >&2
  exit 2
fi
# xcodebuild accepts settings it does not use, so read the number back out of the bundle.
scripts/verify-version.sh "$built" 0.0.0 "$build"

# Processes running out of one bundle, by executable path. By path and not by name: the unit tests
# run hosted inside an app also named Lyrebird, and `pkill -x Lyrebird` killed a `make check-app`
# that happened to be bootstrapping at the time.
running_from() {
  local pid
  for pid in $(pgrep -x Lyrebird); do
    case "$(ps -o comm= -p "$pid")" in "$1/"*) echo "$pid" ;; esac
  done
}

# A running copy keeps the old code; replacing the bundle under it is what leaves two Lyrebirds in
# the menu bar. SIGTERM, then wait: `ditto` over a bundle still being read is not an install.
if [[ -n "$(running_from "$dest/Lyrebird.app")" ]]; then
  kill $(running_from "$dest/Lyrebird.app") || true
  for _ in $(seq 1 50); do
    [[ -z "$(running_from "$dest/Lyrebird.app")" ]] && break
    sleep 0.1
  done
  if [[ -n "$(running_from "$dest/Lyrebird.app")" ]]; then
    echo "install-app: the Lyrebird running from $dest/Lyrebird.app did not quit; quit it and run again" >&2
    exit 1
  fi
fi
for pid in $(pgrep -x Lyrebird); do
  echo "install-app: note: another Lyrebird is running and is left alone: $(ps -o comm= -p "$pid")" >&2
done

# Copy beside the old bundle, check the copy, then swap: removing the old one first left nothing
# installed when the copy failed halfway (a full disk, a Ctrl-C).
staged="$dest/.Lyrebird.app.installing-$$"
rm -rf "$staged"
trap 'rm -rf "$staged"' EXIT
ditto "$built" "$staged"
installed="$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$staged/Contents/Info.plist")"
if [[ "$installed" != "$build" ]]; then
  echo "install-app: the copied bundle reports build $installed, expected $build" >&2
  exit 1
fi
rm -rf "$dest/Lyrebird.app"
mv "$staged" "$dest/Lyrebird.app"

# `open` returns 0 once the launch request is submitted, which says nothing about the app staying
# up; the outcome is a process running out of the installed bundle.
open "$dest/Lyrebird.app"
for _ in $(seq 1 100); do
  if [[ -n "$(running_from "$dest/Lyrebird.app")" ]]; then
    echo "✓ Lyrebird.app build $build installed in $dest and running"
    exit 0
  fi
  sleep 0.1
done
echo "install-app: $dest/Lyrebird.app was installed but no process is running from it after 10s" >&2
exit 1
