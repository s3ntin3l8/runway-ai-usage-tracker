# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Runway Sidecar on macOS
# Build with: pyinstaller sidecar_app/spec/macos.spec

a = Analysis(
    ["sidecar_app/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("scripts/sidecar.py", "scripts"),
        ("sidecar_app/assets", "assets"),
        ("package.json", "."),
    ],
    hiddenimports=["pystray._darwin", "PIL.Image", "PIL.PngImagePlugin", "pkg_resources"],
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
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
    },
)
