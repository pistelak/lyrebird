#!/usr/bin/env bash
# Install only into this checkout. An existing matching formatter can be reused.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/tool-versions.env
mkdir -p .build/tools/bin
tool_tmp="$(mktemp -d "${TMPDIR:-/tmp}/lyrebird-tools.XXXXXX")"
trap 'rm -rf "$tool_tmp"' EXIT

download() {
    local url="$1" checksum="$2" destination="$3"
    curl --fail --location --silent --show-error "$url" -o "$destination"
    printf '%s  %s\n' "$checksum" "$destination" | shasum -a 256 --check
}

matches_version() {
    local actual
    [[ -x "$1" ]] && actual="$("$1" --version)" && [[ "$actual" == "$2" ]]
}

for tool in xcodegen swift-format; do
    case "$tool" in
        xcodegen) expected="Version: $XCODEGEN_VERSION" ;;
        swift-format) expected="$SWIFT_FORMAT_VERSION" ;;
    esac
    destination=".build/tools/bin/$tool"
    if matches_version "$destination" "$expected" && \
        { [[ "$tool" == swift-format ]] || [[ -f .build/tools/bin/XcodeGen_XcodeGenKit.bundle/Contents/Resources/SettingPresets/base.yml ]]; }; then
        continue
    fi
    if [[ "$tool" == swift-format ]] && candidate="$(command -v "$tool")" && \
        matches_version "$candidate" "$expected"; then
        cp -f "$candidate" "$destination"
        continue
    fi
    case "$tool" in
        xcodegen)
            download "https://github.com/yonaskolb/XcodeGen/releases/download/$XCODEGEN_VERSION/xcodegen.artifactbundle.zip" \
                "$XCODEGEN_SHA256" "$tool_tmp/xcodegen.zip"
            unzip -q "$tool_tmp/xcodegen.zip" -d "$tool_tmp/xcodegen"
            # Keep the settings bundle beside the universal executable. Copying only the
            # executable makes project generation silently omit essential build settings.
            distribution="$tool_tmp/xcodegen/xcodegen.artifactbundle/xcodegen-$XCODEGEN_VERSION-macosx/bin"
            test -f "$distribution/XcodeGen_XcodeGenKit.bundle/Contents/Resources/SettingPresets/base.yml"
            cp -Rf "$distribution/." .build/tools/bin/
            ;;
        swift-format)
            swift_version="$(xcrun swift --version)"
            if [[ ! "$swift_version" =~ Swift\ version\ ([0-9]+)\. ]] || (( BASH_REMATCH[1] < 6 )); then
                echo 'Building swift-format requires Swift 6 or newer; select Xcode 16+ with xcode-select' >&2
                exit 1
            fi
            download "https://github.com/swiftlang/swift-format/archive/refs/tags/$SWIFT_FORMAT_VERSION.tar.gz" \
                "$SWIFT_FORMAT_SHA256" "$tool_tmp/swift-format.tar.gz"
            tar -xzf "$tool_tmp/swift-format.tar.gz" -C "$tool_tmp"
            source_dir="$tool_tmp/swift-format-$SWIFT_FORMAT_VERSION"
            cp scripts/swift-format.resolved "$source_dir/Package.resolved"
            xcrun swift build --package-path "$source_dir" --force-resolved-versions -c release --product swift-format
            binary_dir="$(xcrun swift build --package-path "$source_dir" --force-resolved-versions -c release --show-bin-path)"
            cp -f "$binary_dir/swift-format" "$destination"
            ;;
    esac
done
bash scripts/check-tools.sh
