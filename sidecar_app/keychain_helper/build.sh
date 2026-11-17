#!/bin/sh
# Build only on deliberate helper changes, never as part of a sidecar release.
set -eu
cd "$(dirname "$0")"
mkdir -p bin
build_dir=$(mktemp -d "./.build.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM
for arch in arm64 x86_64; do
    xcrun clang -arch "$arch" -mmacosx-version-min=11.0 -std=c11 -Os \
        -Wall -Wextra -Werror -Wno-deprecated-declarations \
        -Wl,-no_adhoc_codesign \
        -framework CoreFoundation -framework Security main.c -o "$build_dir/$arch"
done
xcrun lipo -create "$build_dir/arm64" "$build_dir/x86_64" -output bin/kc_helper
codesign --force --sign - --identifier com.runway.sidecar.keychain-helper --timestamp=none bin/kc_helper
