"""pystray-based system tray icon for the Runway sidecar."""

import pathlib
import subprocess
import sys
import webbrowser
from collections.abc import Callable

import pystray
from PIL import Image

from sidecar_app.autostart import install_login_item, is_login_item_installed, remove_login_item
from sidecar_app.daemon import TrayDaemon

RELEASES_URL = "https://github.com/bjoernf73/runway/releases"

if getattr(sys, "frozen", False):
    _ASSETS_DIR = pathlib.Path(sys._MEIPASS) / "assets"  # type: ignore[attr-defined]
else:
    _ASSETS_DIR = pathlib.Path(__file__).parent / "assets"

_STATUS_ICON: dict[str, str] = {
    "ok": "icon_ok",
    "warn": "icon_warn",
    "err": "icon_err",
    "paused": "icon_paused",
    "starting": "icon_warn",
}

_STATUS_TITLE: dict[str, str] = {
    "ok": "Runway Sidecar — Healthy",
    "warn": "Runway Sidecar — Warning",
    "err": "Runway Sidecar — Error",
    "paused": "Runway Sidecar — Paused",
    "starting": "Runway Sidecar — Starting",
}


def _load_image(icon_name: str) -> Image.Image:
    """Load a PNG from the assets directory and return a PIL Image."""
    path = _ASSETS_DIR / f"{icon_name}.png"
    return Image.open(path)


def _open_in_editor(path: pathlib.Path) -> None:
    """Open *path* in the system default editor."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])  # noqa: S603 S607
    elif sys.platform == "win32":
        subprocess.Popen(["notepad.exe", str(path)])  # noqa: S603 S607
    else:
        subprocess.Popen(["xdg-open", str(path)])  # noqa: S603 S607


class SidecarTray:
    """pystray system tray icon + menu for the Runway sidecar."""

    def __init__(
        self,
        daemon: TrayDaemon,
        config: dict,
        config_path: pathlib.Path,
    ) -> None:
        self._daemon = daemon
        self._config = config
        self._config_path = config_path
        self._icon: pystray.Icon | None = None
        self._paused = False
        self._update_available = False
        self._after_start: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def set_update_available(self, flag: bool) -> None:
        """Set the update-available flag and refresh the tray title/menu."""
        self._update_available = flag
        if self._icon is not None:
            self._icon.title = self._build_title(self._daemon.status)
            self._icon.update_menu()

    def run(self, after_start: Callable[[], None] | None = None) -> None:
        """Build the pystray icon and start the event loop (blocks)."""
        status = self._daemon.status
        icon_name = _STATUS_ICON.get(status, "icon_warn")
        title = self._build_title(status)

        print(f"[DIAG] tray.run: status={status} icon={icon_name}", flush=True)
        img = _load_image(icon_name)
        print(f"[DIAG] icon image loaded: size={img.size} mode={img.mode}", flush=True)

        self._after_start = after_start
        self._icon = pystray.Icon(
            name="runway-sidecar",
            icon=img,
            title=title,
            menu=self._build_menu(),
        )
        print("[DIAG] pystray.Icon created, calling run()...", flush=True)
        self._icon.run(setup=self._on_tray_ready)
        print("[DIAG] pystray.Icon.run() returned (tray exited)", flush=True)

    def _on_tray_ready(self, icon: pystray.Icon) -> None:
        """Called by pystray once the icon is running; fires the after_start hook."""
        print(f"[DIAG] _on_tray_ready: icon.visible before={icon.visible}", flush=True)
        icon.visible = True
        print(f"[DIAG] _on_tray_ready: icon.visible after={icon.visible}", flush=True)
        if self._after_start:
            self._after_start()

    def _build_title(self, status: str) -> str:
        """Return the tray tooltip title, appending an update notice when available."""
        base = _STATUS_TITLE.get(status, "Runway Sidecar")
        if self._update_available:
            return f"{base} (update available)"
        return base

    def _update_icon(self, status: str) -> None:
        """Swap the tray icon image and title to reflect the new status."""
        if self._icon is None:
            return
        icon_name = _STATUS_ICON.get(status, "icon_warn")
        self._icon.icon = _load_image(icon_name)
        self._icon.title = self._build_title(status)
        self._icon.update_menu()

    # ------------------------------------------------------------------
    # Menu helpers
    # ------------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        """Construct the full context menu."""

        def title_text(item: pystray.MenuItem) -> str:
            return self._build_title(self._daemon.status)

        def pause_resume_text(item: pystray.MenuItem) -> str:
            return "Resume" if self._paused else "Pause"

        def on_open_dashboard(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            webbrowser.open(self._config.get("api_url", "http://localhost:8765"))

        def on_run_now(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            self._daemon.trigger_now()

        def on_pause_resume(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            if self._paused:
                self._daemon.resume()
                self._paused = False
            else:
                self._daemon.pause()
                self._paused = True
            icon.update_menu()

        def on_edit_config(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            _open_in_editor(self._config_path)

        def on_view_logs(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            from sidecar_app.config import get_log_path

            _open_in_editor(get_log_path())

        def on_check_updates(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            webbrowser.open(RELEASES_URL)

        def on_launch_at_login(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            if is_login_item_installed():
                remove_login_item()
            else:
                install_login_item()
            icon.update_menu()

        def on_quit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            icon.stop()
            self._daemon.stop()

        return pystray.Menu(
            pystray.MenuItem(title_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Dashboard", on_open_dashboard),
            pystray.MenuItem("Run Now", on_run_now),
            pystray.MenuItem(pause_resume_text, on_pause_resume),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Launch at Login",
                on_launch_at_login,
                checked=lambda item: is_login_item_installed(),  # noqa: ARG005
            ),
            pystray.MenuItem("Edit Config…", on_edit_config),
            pystray.MenuItem("View Logs…", on_view_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Check for Updates…", on_check_updates),
            pystray.MenuItem("About", None),
            pystray.MenuItem("Quit", on_quit),
        )
