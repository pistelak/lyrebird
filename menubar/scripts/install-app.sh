#!/usr/bin/env bash
# Build the menu-bar app as Release from this checkout, put it in /Applications (or the directory
# given as $1), replacing the copy there, and launch it. One installed copy on purpose: every Xcode
# build leaves a launchable .app that Spotlight lists as "Lyrebird" too, with no way to tell which.
set -euo pipefail

dest="${1:-/Applications}"
if [[ ! -f menubar/project.yml ]]; then
  echo "install-app: run from the repository root" >&2
  exit 2
fi
root="$PWD"
# Absolute and physical before any `cd`: a relative APP_INSTALL_DIR would otherwise land under
# menubar/ and exit 0 with the intended install untouched, and a `pwd` that keeps symlinks let an
# alias of the build products directory past the refusal below — see
# test_install_app_refuses_a_dest_that_is_a_symlink_to_the_build_products_directory and
# test_install_app_resolves_a_relative_install_directory_against_the_invocation_directory.
if ! dest="$(cd "$dest" 2>/dev/null && pwd -P)"; then
  echo "install-app: install directory does not exist: ${1:-/Applications}" >&2
  exit 2
fi
# Overlapping installs interleave the swap; `mkdir` is atomic, so the second run refuses — see
# test_install_app_refuses_to_start_while_another_install_holds_the_lock.
lock="$dest/.Lyrebird.app.install-lock"
staged=""
if ! mkdir "$lock" 2>/dev/null; then
  echo "install-app: another install-app is running against $dest (lock: $lock); wait for it, or remove the lock if no install is running" >&2
  exit 1
fi
cleanup() {
  rm -rf "$lock"
  # Only this run's staging directory: after the old bundle is moved aside the backup holds the
  # sole copy of the previous install until the swap succeeds, and deleting it here would lose it.
  if [[ -n "$staged" ]]; then
    rm -rf "$staged"
  fi
}
trap cleanup EXIT

bundle="$dest/Lyrebird.app"
# A symlinked bundle would be replaced by a real directory while the app it points at keeps
# running: `ps` reports the physical executable path, so the running copy never matches the link
# and nothing is quit — see test_install_app_refuses_an_installed_bundle_that_is_a_symlink.
if [[ -L "$bundle" ]]; then
  echo "install-app: $bundle is a symlink; refusing to replace a link with an installed app" >&2
  exit 2
fi

# Seconds to wait for the launched app to show up. It is settable so a check that knows the launch
# will never happen does not sit through the full wait — see
# test_install_app_fails_when_the_installed_app_never_starts.
launch_wait="${LYREBIRD_INSTALL_LAUNCH_WAIT:-10}"
case "$launch_wait" in
  '' | *[!0-9]*)
    echo "install-app: LYREBIRD_INSTALL_LAUNCH_WAIT must be whole seconds, got '$launch_wait'" >&2
    exit 2
    ;;
esac
launch_wait=$((10#$launch_wait))
if [[ "$launch_wait" -lt 1 ]]; then
  echo "install-app: LYREBIRD_INSTALL_LAUNCH_WAIT must be at least 1 second" >&2
  exit 2
fi

# Paths are compared by what they point at, never as strings. macOS filesystems are
# case-insensitive and `pwd -P` keeps the case the caller typed, so `APP_INSTALL_DIR=/applications`
# never matched a process reported under `/Applications`: the running copy was left alone and its
# bundle replaced under it, and the same spelling walked past the build-products refusal — see
# test_install_app_quits_the_running_copy_when_the_install_directory_is_spelled_in_another_case.
# An empty answer means the path could not be stat'ed, which is never "the same file".
file_id() {
  stat -f '%d:%i' "$1" 2>/dev/null || true
}

build="$(git rev-list --count HEAD)"

cd "$root/menubar"
"$root/.build/tools/bin/xcodegen" generate
xcodebuild -quiet -project Lyrebird.xcodeproj -scheme Lyrebird -configuration Release \
  -derivedDataPath .build -destination 'platform=macOS' CURRENT_PROJECT_VERSION="$build" build
products="$(cd "$root/menubar/.build/Build/Products/Release" && pwd -P)"
built="$products/Lyrebird.app"
# Both exist here, so an identity that cannot be read is a failed question, not a different answer.
dest_id="$(file_id "$dest")"
products_id="$(file_id "$products")"
if [[ -z "$dest_id" || -z "$products_id" ]]; then
  echo "install-app: cannot stat $dest or $products; cannot tell whether the install directory is the build products directory" >&2
  exit 1
fi
if [[ "$dest_id" == "$products_id" ]]; then
  echo "install-app: install directory is the build products directory; refusing to replace the build with itself" >&2
  exit 2
fi
# xcodebuild accepts settings it does not use, so read the number back out of the bundle.
scripts/verify-version.sh "$built" 0.0.0 "$build"

# Lyrebird processes, split into the ones running out of the bundle named in $1 and the rest, by
# which file is executing. By file and not by name: the unit tests run hosted inside an app also named
# Lyrebird, and `pkill -x Lyrebird` killed a `make check-app` that happened to be bootstrapping at
# the time. The answers land in globals because a query that cannot be answered exits the script,
# and an `exit` inside `$(...)` only leaves the subshell.
find_lyrebirds() {
  local pids status pid comm signal_error executable comm_id
  running_pids=()
  other_lyrebirds=()
  # Empty only when there is nothing installed yet: an executable that is there but cannot be
  # stat'ed is a question left open, and answering it "no installation" filed the running installed
  # copy as somebody else's — see test_install_app_stops_when_the_installed_executable_cannot_be_stated.
  executable=""
  if [[ -e "$1/Contents/MacOS/Lyrebird" ]]; then
    executable="$(file_id "$1/Contents/MacOS/Lyrebird")"
    if [[ -z "$executable" ]]; then
      echo "install-app: cannot stat $1/Contents/MacOS/Lyrebird; cannot tell which running Lyrebird is the installed one" >&2
      exit 1
    fi
  fi
  status=0
  pids="$(pgrep -x Lyrebird)" || status=$?
  # A pgrep that errored (2 and up) read as "nothing is running" — see
  # test_install_app_stops_when_pgrep_cannot_answer.
  if [[ "$status" -gt 1 ]]; then
    echo "install-app: 'pgrep -x Lyrebird' failed with status $status; cannot tell what is running" >&2
    exit 1
  fi
  for pid in $pids; do
    # `ps` exits 1 both for a pid that is gone and for a `ps` that failed, so existence is settled
    # first — see test_install_app_stops_when_ps_cannot_read_a_live_process.
    status=0
    signal_error="$(kill -0 "$pid" 2>&1)" || status=$?
    if [[ "$status" -ne 0 ]]; then
      case "$signal_error" in
        *"No such process"*) continue ;;
      esac
    fi
    status=0
    comm="$(ps -o comm= -p "$pid")" || status=$?
    if [[ "$status" -ne 0 || -z "$comm" ]]; then
      echo "install-app: 'ps -o comm= -p $pid' gave no executable path for a process that is running; cannot tell what is running" >&2
      exit 1
    fi
    # A live process whose executable cannot be stat'ed is the one that must not be filed as
    # somebody else's — see test_install_app_stops_when_a_running_copys_executable_cannot_be_stated.
    comm_id="$(file_id "$comm")"
    if [[ -z "$comm_id" ]]; then
      echo "install-app: cannot stat the executable of the Lyrebird running as $pid ($comm); cannot tell whether it is the copy in $1" >&2
      exit 1
    fi
    if [[ "$comm_id" == "$executable" ]]; then
      running_pids+=("$pid")
    else
      other_lyrebirds+=("$comm")
    fi
  done
}

# A running copy keeps the old code; replacing the bundle under it is what leaves two Lyrebirds in
# the menu bar. SIGTERM, then wait: `ditto` over a bundle still being read is not an install.
find_lyrebirds "$bundle"
if [[ "${#running_pids[@]}" -gt 0 ]]; then
  kill "${running_pids[@]}" || true
  for _ in $(seq 1 50); do
    find_lyrebirds "$bundle"
    [[ "${#running_pids[@]}" -eq 0 ]] && break
    sleep 0.1
  done
  if [[ "${#running_pids[@]}" -gt 0 ]]; then
    echo "install-app: the Lyrebird running from $bundle did not quit; quit it and run again" >&2
    exit 1
  fi
fi
for comm in ${other_lyrebirds[@]+"${other_lyrebirds[@]}"}; do
  echo "install-app: note: another Lyrebird is running and is left alone: $comm" >&2
done

# Copy beside the old bundle, check the copy, then swap: removing the old one first left nothing
# installed when the copy failed halfway (a full disk, a Ctrl-C).
#
# The directories are named by `mktemp -d`, not by pid: an interrupted run leaves its recovery copy
# behind, pids are reused, and the old `-$$` names meant a later run could delete the one bundle a
# previous one had left. Nothing already in $dest is removed — see
# test_install_app_leaves_an_earlier_runs_recovery_copy_alone.
staged="$(mktemp -d "$dest/.Lyrebird.app.installing-XXXXXX")"
ditto "$built" "$staged/Lyrebird.app"
# The plist is checked for before it is read: PlistBuddy prints "File Doesn't Exist, Will Create"
# and exits 1, which under `set -e` would end the run with no word about what went wrong — see
# test_install_app_stops_when_the_copy_arrives_without_its_plist.
if [[ ! -f "$staged/Lyrebird.app/Contents/Info.plist" ]]; then
  echo "install-app: the copy at $staged/Lyrebird.app has no Contents/Info.plist; it did not complete" >&2
  exit 1
fi
installed="$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$staged/Lyrebird.app/Contents/Info.plist")"
if [[ "$installed" != "$build" ]]; then
  echo "install-app: the copied bundle reports build $installed, expected $build" >&2
  exit 1
fi
# Move the old bundle aside rather than deleting it: `rm -rf old && mv staged old` leaves nothing
# installed when that `mv` fails — see
# test_install_app_puts_the_previous_bundle_back_when_the_swap_fails.
staged_inode="$(stat -f %i "$staged/Lyrebird.app")"

backup=""
if [[ -e "$bundle" ]]; then
  backup="$(mktemp -d "$dest/.Lyrebird.app.previous-XXXXXX")"
  mv "$bundle" "$backup/Lyrebird.app"
fi
if ! mv "$staged/Lyrebird.app" "$bundle"; then
  echo "install-app: could not move the new bundle into place at $bundle" >&2
  if [[ -n "$backup" ]]; then
    if mv "$backup/Lyrebird.app" "$bundle"; then
      echo "install-app: the previous $bundle was put back" >&2
      rmdir "$backup" || true
    else
      echo "install-app: the previous bundle is at $backup/Lyrebird.app; move it back to $bundle" >&2
    fi
  fi
  exit 1
fi

# Identity by inode: a competing bundle can carry the same build number, and `mv` into one that
# arrived here nests ours inside it and exits 0 — see
# test_install_app_rejects_a_bundle_that_appeared_during_the_swap. The checks after it are for a
# copy that arrived damaged.
landed=""
landed_inode="$(stat -f %i "$bundle" 2>/dev/null || true)"
if [[ "$landed_inode" != "$staged_inode" ]]; then
  landed="a different bundle is at $bundle"
elif [[ ! -f "$bundle/Contents/Info.plist" ]]; then
  landed="$bundle has no Contents/Info.plist"
elif ! version="$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$bundle/Contents/Info.plist" 2>&1)"; then
  landed="$bundle has no readable CFBundleVersion: $version"
elif [[ "$version" != "$build" ]]; then
  landed="$bundle reports build $version, expected $build"
elif [[ ! -f "$bundle/Contents/MacOS/Lyrebird" || ! -x "$bundle/Contents/MacOS/Lyrebird" ]]; then
  landed="$bundle has no executable at Contents/MacOS/Lyrebird"
fi
if [[ -n "$landed" ]]; then
  echo "install-app: $landed; the install did not land where it was meant to" >&2
  if [[ -n "$backup" ]]; then
    echo "install-app: the previous bundle is kept at $backup/Lyrebird.app" >&2
  fi
  exit 1
fi

if [[ -n "$backup" ]]; then
  rm -rf "$backup"
fi

# `open` returns 0 once the launch request is submitted, which says nothing about the app staying
# up; the outcome is a process running out of the installed bundle.
open "$bundle"
for _ in $(seq 1 $((launch_wait * 10))); do
  find_lyrebirds "$bundle"
  if [[ "${#running_pids[@]}" -gt 0 ]]; then
    echo "✓ Lyrebird.app build $build installed in $dest and running"
    exit 0
  fi
  sleep 0.1
done
echo "install-app: $bundle was installed but no process is running from it after ${launch_wait}s" >&2
exit 1
