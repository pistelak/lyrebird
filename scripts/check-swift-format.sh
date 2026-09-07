#!/usr/bin/env bash
# A partial rules map disables naming rules; prove both naming and whitespace checks work.
set -euo pipefail
cd "$(dirname "$0")/.."
probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/lyrebird-format.XXXXXX")"
trap 'rm -rf "$probe_dir"' EXIT
printf 'func BadName() {\n  print("hello")\n}\n' > "$probe_dir/probe.swift"
if .build/tools/bin/swift-format lint --strict --configuration .swift-format \
    "$probe_dir/probe.swift" > "$probe_dir/output" 2>&1; then
    echo 'Swift lint accepted incorrect style; check the full rules map in .swift-format' >&2
    exit 1
fi
for rule in Indentation AlwaysUseLowerCamelCase; do
    if ! grep -Fq "[$rule]" "$probe_dir/output"; then
        cat "$probe_dir/output" >&2
        echo "Swift lint failed without diagnosing the $rule probe" >&2
        exit 1
    fi
done
