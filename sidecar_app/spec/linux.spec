# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Runway Sidecar on Linux (tray app)
# Build with: pyinstaller sidecar_app/spec/linux.spec

import os

# PyInstaller 6+ resolves relative paths against the spec's directory.
# Anchor everything to the repo root regardless of the invoking CWD.
_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

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
        # pystray picks the available backend at runtime; bundle both so the
        # binary works on AppIndicator-capable desktops (GNOME/Unity/KDE with
        # the extension) and falls back to plain GTK elsewhere.
        "pystray._appindicator",
        "pystray._gtk",
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
        # Optional --keep-alive (agy + xAI + Claude Code login renewal) — imported lazily, so
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
        "scripts.sidecar_pkg.anthropic_renewer",
        "scripts.sidecar_pkg.codex_renewer",
        "scripts.sidecar_pkg.oauth_renewal",
        "scripts.sidecar_pkg.asset_names",
        "scripts.sidecar_pkg.canonical_providers",
        "certifi",
        # Linux browser-cookie decryption path inside scripts/sidecar.py imports
        # these lazily; PyInstaller's static scan won't catch them.
        "secretstorage",
        "cryptography.hazmat.primitives.ciphers",
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
    # Keep stderr visible: a tray binary that can't find DBus/X11 should print
    # the error instead of silently exiting on first run.
    console=True,
    disable_windowed_traceback=False,
)
