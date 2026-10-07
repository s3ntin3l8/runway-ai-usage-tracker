# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Runway Sidecar on Linux (headless CLI)
# Build with: pyinstaller sidecar_app/spec/linux-cli.spec
#
# Entry point is scripts/sidecar.py directly — no pystray, no PIL, no tray UI.
# Runs in Docker, on headless servers, and in any environment without an X
# server or DBus session.

import os

_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

a = Analysis(
    [os.path.join(_ROOT, "scripts", "sidecar.py")],
    pathex=[_ROOT],
    binaries=[],
    datas=[
        # package.json is read at runtime for --version output.
        (os.path.join(_ROOT, "package.json"), "."),
    ],
    hiddenimports=[
        # Mirror every top-level import in scripts/sidecar.py. PyInstaller's
        # static scan should find these, but declaring them is cheap insurance.
        "argparse",
        "atexit",
        "datetime",
        "hashlib",
        "hmac",
        "json",
        "logging",
        "logging.handlers",
        "platform",
        "re",
        "signal",
        "socket",
        "sqlite3",
        "struct",
        "subprocess",
        "threading",
        "time",
        "urllib",
        "urllib.error",
        "urllib.request",
        # Notify-only update check (function-local import in scripts/sidecar.py).
        "scripts.sidecar_pkg.update_check",
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
        "scripts.sidecar_pkg.keep_alive",
        "scripts.sidecar_pkg.runtime_cleanup",
        "scripts.sidecar_pkg.self_update",
        "scripts.sidecar_pkg.xai_renewer",
        "scripts.sidecar_pkg.anthropic_renewer",
        "scripts.sidecar_pkg.codex_renewer",
        "scripts.sidecar_pkg.oauth_renewal",
        "scripts.sidecar_pkg.asset_names",
        "scripts.sidecar_pkg.canonical_providers",
        "certifi",
        # Lazy imports inside the Linux browser-cookie decryption branch.
        "secretstorage",
        "cryptography.hazmat.primitives.ciphers",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Strip everything that only the tray UI needs — keeps the headless binary
    # small enough to drop into a slim Docker image.
    excludes=["pystray", "PIL", "tkinter", "matplotlib"],
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
    name="runway-sidecar-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
)
