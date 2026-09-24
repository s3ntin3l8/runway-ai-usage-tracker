# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Runway Sidecar on macOS
# Build with: pyinstaller sidecar_app/spec/macos.spec

import json as _json
import os
import re as _re

# PyInstaller 6+ resolves relative paths against the spec's directory.
# Anchor everything to the repo root regardless of the invoking CWD.
_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# Read version from package.json so CFBundleVersion stays in sync with releases.
_VERSION = _json.loads(open(os.path.join(_ROOT, "package.json")).read()).get("version", "0.0.0")
# CFBundleVersion / CFBundleShortVersionString must be dotted integers: drop a
# pre-release or ``+edge.<sha>`` suffix (the full string still ships in the
# bundled package.json, which is what the updater reads).
_BUNDLE_VERSION = _re.split(r"[-+]", _VERSION.lstrip("vV"), maxsplit=1)[0] or "0.0.0"

a = Analysis(
    [os.path.join(_ROOT, "sidecar_app", "__main__.py")],
    pathex=[_ROOT],
    binaries=[],
    datas=[
        (os.path.join(_ROOT, "scripts", "sidecar.py"), "scripts"),
        (os.path.join(_ROOT, "sidecar_app", "assets"), "assets"),
        (os.path.join(_ROOT, "package.json"), "."),
    ],
    hiddenimports=[
        "pystray._darwin",
        "PIL.Image",
        "PIL.PngImagePlugin",
        "pkg_resources",
        # scripts/sidecar.py is bundled as data, so PyInstaller never scans its
        # imports — declare them explicitly so stdlib modules get collected.
        "argparse",
        "atexit",
        "datetime",
        "hashlib",
        "hmac",
        "json",
        "logging",
        "logging.handlers",
        "platform",
        "signal",
        "socket",
        "sqlite3",
        "struct",
        "subprocess",
        "threading",
        "urllib",
        "urllib.error",
        "urllib.request",
        # Notify-only update check, shared by the CLI and the tray updater.
        "scripts.sidecar_pkg.update_check",
        # Shared TLS trust-store helper + bundled CA store (certifi). The
        # certifi hiddenimport triggers PyInstaller's hook-certifi, which
        # ships cacert.pem so HTTPS verifies without a system CA store.
        "scripts.sidecar_pkg.tls",
        "certifi",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RunwaySidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name="RunwaySidecar",
)

app = BUNDLE(
    coll,
    name="Runway Sidecar.app",
    # Rendered from assets/logo.svg by `make logo` (installer/generate_app_icons.py).
    icon=os.path.join(_ROOT, "installer", "assets", "app.icns"),
    bundle_identifier="com.runway.sidecar",
    info_plist={
        "LSUIElement": True,
        "CFBundleName": "Runway Sidecar",
        "CFBundleDisplayName": "Runway Sidecar",
        "CFBundleVersion": _BUNDLE_VERSION,
        "CFBundleShortVersionString": _BUNDLE_VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Runway contributors. Licensed under AGPL-3.0.",
    },
)
