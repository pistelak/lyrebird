#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/tool-versions.env
for tool in xcodegen swift-format; do
    case "$tool" in
        xcodegen) expected="Version: $XCODEGEN_VERSION" ;;
        swift-format) expected="$SWIFT_FORMAT_VERSION" ;;
    esac
    executable=".build/tools/bin/$tool"
    if [[ ! -x "$executable" ]] || ! actual="$("$executable" --version)" || [[ "$actual" != "$expected" ]]; then
        echo "$tool: expected $expected; run make setup-app" >&2
        exit 1
    fi
    echo "$tool: $expected"
done
if [[ ! -f .build/tools/bin/XcodeGen_XcodeGenKit.bundle/Contents/Resources/SettingPresets/base.yml ]]; then
    echo 'xcodegen: missing settings bundle; run make setup-app' >&2
    exit 1
fi
