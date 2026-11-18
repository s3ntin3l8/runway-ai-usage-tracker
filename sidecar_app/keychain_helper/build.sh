#!/bin/sh
# Build only on deliberate helper changes, never as part of a sidecar release.
set -eu
cd "$(dirname "$0")"
mkdir -p bin
build_dir=$(mktemp -d "./.build.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM
# arm64 only: the sidecar app is built arm64-only (PyInstaller targets the runner's
# architecture) and PyInstaller thins a universal helper to it anyway, which used
# to make the x86_64 golden CDHash unreachable.
xcrun clang -arch arm64 -mmacosx-version-min=11.0 -std=c11 -Os \
    -Wall -Wextra -Werror -Wno-deprecated-declarations \
    -Wl,-no_adhoc_codesign \
    -framework CoreFoundation -framework Security main.c -o "$build_dir/arm64"
cp "$build_dir/arm64" bin/kc_helper
codesign --force --sign - --identifier com.runway.sidecar.keychain-helper --timestamp=none bin/kc_helper
