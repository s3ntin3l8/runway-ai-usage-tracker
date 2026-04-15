# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Runway Sidecar on macOS
# Build with: pyinstaller sidecar_app/spec/macos.spec

import json as _json
import os

# PyInstaller 6+ resolves relative paths against the spec's directory.
# Anchor everything to the repo root regardless of the invoking CWD.
_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# Read version from package.json so CFBundleVersion stays in sync with releases.
_VERSION = _json.loads(open(os.path.join(_ROOT, "package.json")).read()).get("version", "0.0.0")

a = Analysis(
    [os.path.join(_ROOT, "sidecar_app", "__main__.py")],
    pathex=[_ROOT],
    binaries=[],
    datas=[
        (os.path.join(_ROOT, "scripts", "sidecar.py"), "scripts"),
        (os.path.join(_ROOT, "sidecar_app", "assets"), "assets"),
        (os.path.join(_ROOT, "assets", "logo_reference.png"), "assets"),
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
    icon=None,
    bundle_identifier="com.runway.sidecar",
    info_plist={
        "LSUIElement": True,
        "CFBundleDisplayName": "Runway Sidecar",
        "CFBundleVersion": _VERSION,
        "CFBundleShortVersionString": _VERSION,
        "NSHighResolutionCapable": True,
    },
)
