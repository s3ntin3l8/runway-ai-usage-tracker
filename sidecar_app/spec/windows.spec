# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Runway Sidecar on Windows
# Build with: pyinstaller sidecar_app/spec/windows.spec

a = Analysis(
    ["sidecar_app/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("scripts/sidecar.py", "scripts"),
        ("sidecar_app/assets", "assets"),
        ("package.json", "."),
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
