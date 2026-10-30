# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Runway Sidecar on Windows
# Build with: pyinstaller sidecar_app/spec/windows.spec

import os
from PyInstaller.utils.hooks import collect_all

sigstore_datas, sigstore_binaries, sigstore_imports = collect_all("sigstore")
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
    binaries=sigstore_binaries,
    datas=sigstore_datas
    + [
        (os.path.join(_ROOT, "scripts", "sidecar.py"), "scripts"),
        (os.path.join(_ROOT, "sidecar_app", "assets"), "assets"),
        (os.path.join(_ROOT, "package.json"), "."),
    ],
    hiddenimports=sigstore_imports
    + [
        "app.core.console",
        "app.core.kimi_device",
        "app.core.version",
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
        # Optional --keep-alive (agy + xAI + Claude Code login renewal) — imported lazily, so
        # PyInstaller's scan must be told about it.
        "scripts.sidecar_pkg.keep_alive",
        "app.core.login_dirs",
        # One-time pairing (runway-sidecar://pair links, --pair).
        "scripts.sidecar_pkg.pairing",
        # Shared TLS trust-store helper + bundled CA store (certifi). The
        # certifi hiddenimport triggers PyInstaller's hook-certifi, which
        # ships cacert.pem so HTTPS verifies without a system CA store.
        "scripts.sidecar_pkg.tls",
        "scripts.sidecar_pkg.transport",
        "scripts.sidecar_pkg.signatures",
        "scripts.sidecar_pkg.update_limits",
        # Every sidecar_pkg module scripts/sidecar.py imports inside a function body.
        # Declared explicitly (guarded by tests/unit/test_sidecar_release_contract.py) so a
        # lazily imported module can never be left out of a frozen build.
        "scripts.sidecar_pkg.credentials",
        "scripts.sidecar_pkg.source_catalog",
        "scripts.sidecar_pkg.source_catalog_data",
        "scripts.sidecar_pkg.source_consent",
        "scripts.sidecar_pkg.source_settings",
        "scripts.sidecar_pkg.source_protocol",
        "scripts.sidecar_pkg.source_setup",
        # Classified browser cookie stores (locked / app-bound). Imported at
        # module level in scripts/sidecar.py, which this spec bundles as data,
        # so PyInstaller never scans it.
        "scripts.sidecar_pkg.browser_store",
        "scripts.sidecar_pkg.claude_desktop",
        "scripts.sidecar_pkg.claude_code_keychain",
        "scripts.sidecar_pkg.macos_cookies",
        "scripts.sidecar_pkg.safari_cookies",
        "scripts.sidecar_pkg.cookie_scan",
        "scripts.sidecar_pkg.cowork_events",
        "cryptography.hazmat.primitives.padding",
        "cryptography.hazmat.primitives.ciphers",
        "scripts.sidecar_pkg.event_extractors.anthropic",
        "scripts.sidecar_pkg.event_extractors.antigravity",
        "scripts.sidecar_pkg.event_extractors.chatgpt",
        "scripts.sidecar_pkg.event_extractors.gemini",
        "scripts.sidecar_pkg.event_extractors.hermes",
        "scripts.sidecar_pkg.event_extractors.minimax_code",
        "scripts.sidecar_pkg.minimax_code_probe",
        "scripts.sidecar_pkg.bootstrap",
        "scripts.sidecar_pkg.event_extractors.opencode",
        "scripts.sidecar_pkg.event_extractors.xai",
        "scripts.sidecar_pkg.event_watermark",
        "scripts.sidecar_pkg.sqlite_cursor",
        "scripts.sidecar_pkg.identity",
        "scripts.sidecar_pkg.runtime_cleanup",
        "scripts.sidecar_pkg.self_update",
        "scripts.sidecar_pkg.xai_renewer",
        "scripts.sidecar_pkg.opencode_credentials",
        "scripts.sidecar_pkg.anthropic_renewer",
        "scripts.sidecar_pkg.claude_keychain_renewer",
        "scripts.sidecar_pkg.macos_keychain_store",
        "scripts.sidecar_pkg.keychain_client",
        "scripts.sidecar_pkg.keychain_protocol",
        "scripts.sidecar_pkg.keychain_worker",
        "scripts.sidecar_pkg.keychain_authorization",
        "scripts.sidecar_pkg.claude_renewal_lock",
        "scripts.sidecar_pkg.agy_version",
        "scripts.sidecar_pkg.codex_renewer",
        "scripts.sidecar_pkg.oauth_renewal",
        # The expiry reader, imported by the renewers and by sidecar.py.
        "scripts.sidecar_pkg.expiry",
        "scripts.sidecar_pkg.asset_names",
        "scripts.sidecar_pkg.canonical_providers",
        # The git repository a working directory belongs to (name only). Imported
        # lazily, so PyInstaller's static scan won't catch it.
        "scripts.sidecar_pkg.git_identity",
        "scripts.sidecar_pkg.session_names",
        # Native Windows Credential Manager reader (advapi32). Imported lazily,
        # so PyInstaller's static scan won't catch it.
        "scripts.sidecar_pkg.wincred",
        # go-keyring envelope decoder for the macOS Keychain entry the agy CLI
        # writes. Imported lazily, so PyInstaller's static scan won't catch it.
        "scripts.sidecar_pkg.keyring_value",
        # The macOS Keychain reader for the Antigravity CLI's login. Imported
        # lazily, so PyInstaller's static scan won't catch it.
        "scripts.sidecar_pkg.agy_keychain",
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
