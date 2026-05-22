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
