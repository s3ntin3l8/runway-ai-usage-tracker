"""Autostart (login item) management for the Runway sidecar.

Provides three idempotent public functions:
  - is_login_item_installed() -> bool
  - install_login_item() -> None
  - remove_login_item() -> None

macOS: LaunchAgent plist at ~/Library/LaunchAgents/com.runway.sidecar.plist
Windows: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run registry key
Other platforms: no-ops (install/remove) / always returns False (is_installed)
"""

import pathlib
import platform
import subprocess
import sys

_SYSTEM = platform.system()

if _SYSTEM == "Windows":
    import winreg  # type: ignore[import]

# ---------------------------------------------------------------------------
# macOS constants
# ---------------------------------------------------------------------------

_MACOS_PLIST_PATH = pathlib.Path.home() / "Library" / "LaunchAgents" / "com.runway.sidecar.plist"
_MACOS_LOG_DIR = pathlib.Path.home() / "Library" / "Logs" / "RunwaySidecar"

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.runway.sidecar</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_dir}/sidecar.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/sidecar.stderr.log</string>
</dict>
</plist>
"""

# ---------------------------------------------------------------------------
# Windows constants
# ---------------------------------------------------------------------------

_WIN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_REG_KEY = "Runway Sidecar"


# ---------------------------------------------------------------------------
# macOS implementation
# ---------------------------------------------------------------------------


def _macos_is_installed() -> bool:
    return _MACOS_PLIST_PATH.exists()


def _macos_install() -> None:
    """Write the LaunchAgent plist and load it via launchctl."""
    _MACOS_LOG_DIR.mkdir(parents=True, exist_ok=True)

    plist_content = _PLIST_TEMPLATE.format(
        executable_path=sys.executable,
        log_dir=str(_MACOS_LOG_DIR),
    )
    _MACOS_PLIST_PATH.write_text(plist_content, encoding="utf-8")

    subprocess.run(  # noqa: S603
        ["launchctl", "load", str(_MACOS_PLIST_PATH)],  # noqa: S607
        check=False,
    )


def _macos_remove() -> None:
    """Unload the LaunchAgent and remove the plist file."""
    if _MACOS_PLIST_PATH.exists():
        subprocess.run(  # noqa: S603
            ["launchctl", "unload", str(_MACOS_PLIST_PATH)],  # noqa: S607
            check=False,
        )
        _MACOS_PLIST_PATH.unlink()


# ---------------------------------------------------------------------------
# Windows implementation
# ---------------------------------------------------------------------------


def _windows_is_installed() -> bool:
    try:
        with winreg.OpenKey(  # type: ignore[name-defined]
            winreg.HKEY_CURRENT_USER,  # type: ignore[name-defined]
            _WIN_REG_PATH,
            0,
            winreg.KEY_READ,  # type: ignore[name-defined]
        ) as key:
            winreg.QueryValueEx(key, _WIN_REG_KEY)  # type: ignore[name-defined]
            return True
    except FileNotFoundError:
        return False


def _windows_install() -> None:
    try:
        with winreg.OpenKey(  # type: ignore[name-defined]
            winreg.HKEY_CURRENT_USER,  # type: ignore[name-defined]
            _WIN_REG_PATH,
            0,
            winreg.KEY_SET_VALUE,  # type: ignore[name-defined]
        ) as key:
            winreg.SetValueEx(  # type: ignore[name-defined]
                key,
                _WIN_REG_KEY,
                0,
                winreg.REG_SZ,
                sys.executable,  # type: ignore[name-defined]
            )
    except OSError:
        pass  # registry is locked or inaccessible


def _windows_remove() -> None:
    try:
        with winreg.OpenKey(  # type: ignore[name-defined]
            winreg.HKEY_CURRENT_USER,  # type: ignore[name-defined]
            _WIN_REG_PATH,
            0,
            winreg.KEY_SET_VALUE,  # type: ignore[name-defined]
        ) as key:
            winreg.DeleteValue(key, _WIN_REG_KEY)  # type: ignore[name-defined]
    except FileNotFoundError:
        pass  # already absent


# ---------------------------------------------------------------------------
# Public API — platform dispatch
# ---------------------------------------------------------------------------


def is_login_item_installed() -> bool:
    """Return True if the login item / autostart entry is installed."""
    if _SYSTEM == "Darwin":
        return _macos_is_installed()
    if _SYSTEM == "Windows":
        return _windows_is_installed()
    return False  # unsupported platform


def install_login_item() -> None:
    """Install the login item / autostart entry (idempotent)."""
    if _SYSTEM == "Darwin":
        _macos_install()
    elif _SYSTEM == "Windows":
        _windows_install()
    # else: no-op on unsupported platforms


def remove_login_item() -> None:
    """Remove the login item / autostart entry (idempotent)."""
    if _SYSTEM == "Darwin":
        _macos_remove()
    elif _SYSTEM == "Windows":
        _windows_remove()
    # else: no-op on unsupported platforms
