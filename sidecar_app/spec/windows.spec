# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Runway Sidecar on Windows
# Build with: pyinstaller sidecar_app/spec/windows.spec

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
    hiddenimports=["pystray._win32", "PIL.Image", "PIL.PngImagePlugin", "pkg_resources"],
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
)
