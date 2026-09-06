#!/usr/bin/env bash
# Read the version back out of a built app bundle and fail if it is not what was asked for.
#
# The Info.plist references $(MARKETING_VERSION) and $(CURRENT_PROJECT_VERSION), which Xcode expands
# at build time. That expansion is invisible from the build log: a plist that hard-codes a literal
# builds exactly as happily as one that does not, and the mistake only surfaces when a release is
# already out. So a versioned build is not finished until something reads the shipped bundle back.
#
# This script only reads. It never edits a plist — a bundle is signed over its Info.plist, so
# patching the version afterwards would invalidate the signature it was just given.
#
# Usage: verify-version.sh <path/to/App.app> <marketing-version> <build-version>
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $(basename "$0") <path/to/App.app> <marketing-version> <build-version>" >&2
    exit 2
fi

app=$1
expected_marketing=$2
expected_build=$3
plist="$app/Contents/Info.plist"

if [ ! -f "$plist" ]; then
    echo "verify-version: no Info.plist at $plist" >&2
    exit 1
fi

if ! actual_marketing=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$plist" 2>&1); then
    echo "verify-version: cannot read CFBundleShortVersionString from $plist: $actual_marketing" >&2
    exit 1
fi

if ! actual_build=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$plist" 2>&1); then
    echo "verify-version: cannot read CFBundleVersion from $plist: $actual_build" >&2
    exit 1
fi

status=0
if [ "$actual_marketing" != "$expected_marketing" ]; then
    echo "verify-version: CFBundleShortVersionString expected '$expected_marketing', got '$actual_marketing'" >&2
    status=1
fi
if [ "$actual_build" != "$expected_build" ]; then
    echo "verify-version: CFBundleVersion expected '$expected_build', got '$actual_build'" >&2
    status=1
fi

if [ "$status" -ne 0 ]; then
    echo "verify-version: $app was not stamped with the requested version" >&2
    exit 1
fi

echo "verify-version: $app is $actual_marketing ($actual_build)"
