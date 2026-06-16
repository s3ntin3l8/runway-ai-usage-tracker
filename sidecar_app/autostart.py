"""Autostart (login item) management for the Runway sidecar.

Provides three idempotent public functions:
  - is_login_item_installed() -> bool
  - install_login_item() -> None
  - remove_login_item() -> None

macOS:   LaunchAgent plist at ~/Library/LaunchAgents/com.runway.sidecar.plist
Windows: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run registry key
Linux:   XDG autostart .desktop at ~/.config/autostart/runway-sidecar.desktop
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
    """Write the LaunchAgent plist (registers autostart for the next login).

    We deliberately do *not* ``launchctl load`` here. Install is always invoked
    from the already-running tray app, and the plist carries ``RunAtLoad``, so
    loading it mid-session would immediately spawn a second sidecar on top of the
    one the user is interacting with. macOS auto-loads ``~/Library/LaunchAgents``
    at the next GUI login, so autostart still works without the duplicate spawn.
    """
    _MACOS_LOG_DIR.mkdir(parents=True, exist_ok=True)

    plist_content = _PLIST_TEMPLATE.format(
        executable_path=sys.executable,
        log_dir=str(_MACOS_LOG_DIR),
    )
    _MACOS_PLIST_PATH.write_text(plist_content, encoding="utf-8")


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
    except OSError:
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
# Linux constants
# ---------------------------------------------------------------------------

_LINUX_DESKTOP_PATH = pathlib.Path.home() / ".config" / "autostart" / "runway-sidecar.desktop"

_DESKTOP_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Runway Sidecar
Comment=Runway AI usage tracker sidecar
Exec={executable_path}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
"""


# ---------------------------------------------------------------------------
# Linux implementation
# ---------------------------------------------------------------------------


def _linux_is_installed() -> bool:
    return _LINUX_DESKTOP_PATH.exists()


def _linux_install() -> None:
    """Write the XDG autostart .desktop file."""
    _LINUX_DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LINUX_DESKTOP_PATH.write_text(
        _DESKTOP_TEMPLATE.format(executable_path=sys.executable),
        encoding="utf-8",
    )


def _linux_remove() -> None:
    """Remove the XDG autostart .desktop file if present."""
    if _LINUX_DESKTOP_PATH.exists():
        _LINUX_DESKTOP_PATH.unlink()


# ---------------------------------------------------------------------------
# Public API — platform dispatch
# ---------------------------------------------------------------------------


def is_login_item_installed() -> bool:
    """Return True if the login item / autostart entry is installed."""
    if _SYSTEM == "Darwin":
        return _macos_is_installed()
    if _SYSTEM == "Windows":
        return _windows_is_installed()
    if _SYSTEM == "Linux":
        return _linux_is_installed()
    return False  # unsupported platform


def install_login_item() -> None:
    """Install the login item / autostart entry (idempotent)."""
    if _SYSTEM == "Darwin":
        _macos_install()
    elif _SYSTEM == "Windows":
        _windows_install()
    elif _SYSTEM == "Linux":
        _linux_install()


def remove_login_item() -> None:
    """Remove the login item / autostart entry (idempotent)."""
    if _SYSTEM == "Darwin":
        _macos_remove()
    elif _SYSTEM == "Windows":
        _windows_remove()
    elif _SYSTEM == "Linux":
        _linux_remove()
