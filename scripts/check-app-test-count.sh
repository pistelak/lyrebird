#!/bin/bash
# check-app-test-count.sh <result-bundle> [filter]
#
# Fails when an xcodebuild test run executed no tests. `xcodebuild -only-testing:` with an identifier
# that matches nothing exits 0 and reports "0 tests, passed", so a `make test-app TEST=...` with a typo
# in it was green having run nothing. The count comes from the result bundle, which is the one place
# xcodebuild writes it whatever its verbosity.
set -euo pipefail

bundle="${1:?usage: check-app-test-count.sh <result-bundle> [filter]}"
filter="${2:-}"

summary=$(xcrun xcresulttool get test-results summary --path "$bundle" --compact)
count=$(printf '%s' "$summary" | python3 -c 'import json, sys; print(int(json.load(sys.stdin)["totalTestCount"]))')

echo "app tests executed: $count"
if [ "$count" -eq 0 ]; then
    if [ -n "$filter" ]; then
        echo "✗ the filter '$filter' matched no test — check the identifier (Target/Suite/test)" >&2
    else
        echo "✗ no test ran" >&2
    fi
    exit 1
fi
