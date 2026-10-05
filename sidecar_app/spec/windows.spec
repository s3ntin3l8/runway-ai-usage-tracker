# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Runway Sidecar on Windows
# Build with: pyinstaller sidecar_app/spec/windows.spec

import os
import sys

# PyInstaller 6+ resolves relative paths against the spec's directory.
# Anchor everything to the repo root regardless of the invoking CWD.
_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# Stamp a VSVersionInfo resource (Explorer → Properties → Details) from
# package.json; the same helper feeds makensis its version (see win_version.py).
sys.path.insert(0, SPECPATH)
from win_version import write_version_file  # noqa: E402

_VERSION_FILE = write_version_file(os.path.join(workpath, "version_info.txt"))  # noqa: F821

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
        "pystray._win32",
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
        # Optional --keep-alive (agy + xAI login renewal) — imported lazily, so
        # PyInstaller's scan must be told about it.
        "scripts.sidecar_pkg.keep_alive",
        # One-time pairing (runway-sidecar://pair links, --pair).
        "scripts.sidecar_pkg.pairing",
        # Shared TLS trust-store helper + bundled CA store (certifi). The
        # certifi hiddenimport triggers PyInstaller's hook-certifi, which
        # ships cacert.pem so HTTPS verifies without a system CA store.
        "scripts.sidecar_pkg.tls",
        # Every sidecar_pkg module scripts/sidecar.py imports inside a function body.
        # Declared explicitly (guarded by tests/unit/test_sidecar_release_contract.py) so a
        # lazily imported module can never be left out of a frozen build.
        "scripts.sidecar_pkg.credentials",
        "scripts.sidecar_pkg.event_extractors.anthropic",
        "scripts.sidecar_pkg.event_extractors.antigravity",
        "scripts.sidecar_pkg.event_extractors.chatgpt",
        "scripts.sidecar_pkg.event_extractors.gemini",
        "scripts.sidecar_pkg.event_extractors.hermes",
        "scripts.sidecar_pkg.event_extractors.opencode",
        "scripts.sidecar_pkg.event_extractors.xai",
        "scripts.sidecar_pkg.event_watermark",
        "scripts.sidecar_pkg.identity",
        "scripts.sidecar_pkg.runtime_cleanup",
        "scripts.sidecar_pkg.self_update",
        "scripts.sidecar_pkg.xai_renewer",
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="RunwaySidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    # Rendered from assets/logo.svg by `make logo` (installer/generate_app_icons.py).
    icon=os.path.join(_ROOT, "installer", "assets", "app.ico"),
    version=_VERSION_FILE,
)
