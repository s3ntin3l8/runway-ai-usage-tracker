"""pystray-based system tray icon for the Runway sidecar."""

import pathlib
import queue
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable

import pystray
from PIL import Image, ImageDraw

from sidecar_app.autostart import install_login_item, is_login_item_installed, remove_login_item
from sidecar_app.daemon import TrayDaemon

RELEASES_URL = "https://github.com/bjoernf73/runway/releases"

if getattr(sys, "frozen", False):
    _ASSETS_DIR = pathlib.Path(sys._MEIPASS) / "assets"  # type: ignore[attr-defined]
else:
    _ASSETS_DIR = pathlib.Path(__file__).parent / "assets"

# Logo used as the base tray icon (bundled from assets/logo_reference.png)
if getattr(sys, "frozen", False):
    _LOGO_PATH = pathlib.Path(sys._MEIPASS) / "assets" / "logo_reference.png"  # type: ignore[attr-defined]
else:
    _LOGO_PATH = pathlib.Path(__file__).parent.parent / "assets" / "logo_reference.png"

_STATUS_TITLE: dict[str, str] = {
    "ok": "Runway Sidecar — Healthy",
    "warn": "Runway Sidecar — Warning",
    "err": "Runway Sidecar — Error",
    "paused": "Runway Sidecar — Paused",
    "starting": "Runway Sidecar — Starting",
}

# Status dot colours — bottom-right corner overlay on the logo
_STATUS_DOT_COLOR: dict[str, tuple[int, int, int]] = {
    "ok": (0x22, 0xC5, 0x5E),  # green
    "warn": (0xF5, 0x9E, 0x0B),  # amber
    "err": (0xEF, 0x44, 0x44),  # red
    "paused": (0x9C, 0xA3, 0xAF),  # grey
    "starting": (0xF5, 0x9E, 0x0B),  # amber
}


def _build_status_icon(status: str) -> Image.Image:
    """Return a 128×128 RGBA icon: logo with transparent background + status dot.

    The source image has a white/light-gray background (JPEG-in-PNG).  We strip
    any pixel with all channels > 220, tight-crop the result, add a small pad,
    then scale to 128×128 so the logo fills the full icon area.
    """
    if sys.platform == "darwin":
        return _build_status_icon_macos(status)
    return _build_status_icon_generic(status)


def _build_status_icon_macos(status: str) -> Image.Image:
    """macOS template icon: clean white pill shape with status dot (template rendering).

    macOS will render all non-transparent pixels as white/black regardless of color,
    so we build a minimalist shape: a centered vertical pill with a status dot.
    Size of the dot varies by status to convey rough state.
    """
    SIZE = 128
    # Pill dimensions: narrow, centered vertical bar
    PILL_W = 30
    PILL_H = 80
    PILL_X = (SIZE - PILL_W) // 2
    PILL_Y = (SIZE - PILL_H) // 2

    # Status dot size varies per status to convey urgency/state
    # ok/starting: smaller (18); warn: medium (22); err: large (22); paused: small (14)
    DOT_SIZES = {
        "ok": 18,
        "warn": 22,
        "err": 22,
        "paused": 14,
        "starting": 18,
    }
    DOT = DOT_SIZES.get(status, 18)
    DOT_MARGIN = 4

    # Start with fully transparent canvas
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw the main pill: rounded vertical rectangle, opaque white
    draw.rounded_rectangle(
        (PILL_X, PILL_Y, PILL_X + PILL_W, PILL_Y + PILL_H),
        radius=PILL_W // 2,
        fill=(255, 255, 255, 255),
    )

    # Draw status dot in bottom-right corner, opaque white
    x0 = SIZE - DOT - DOT_MARGIN
    y0 = SIZE - DOT - DOT_MARGIN
    draw.ellipse((x0, y0, x0 + DOT, y0 + DOT), fill=(255, 255, 255, 255))

    return img


def _build_status_icon_generic(status: str) -> Image.Image:
    """Windows/Linux version (original implementation)."""
    SIZE = 128
    DOT = 24
    MARGIN = 2

    # Resize to workable intermediate size before per-pixel background removal
    WORK = 256
    work = Image.open(_LOGO_PATH).convert("RGBA").resize((WORK, WORK), Image.LANCZOS)

    # Strip white/near-white background — threshold: all channels > 220
    px = work.load()
    for y in range(WORK):
        for x in range(WORK):
            r, g, b, a = px[x, y]  # type: ignore[misc]
            if r > 220 and g > 220 and b > 220:
                px[x, y] = (r, g, b, 0)  # type: ignore[index]

    # Tight crop to the non-transparent bounding box
    bbox = work.getbbox()
    if bbox:
        work = work.crop(bbox)

    # Add ~1 % padding so the logo fills as much of the icon as possible
    w, h = work.size
    pad = max(int(w * 0.01), int(h * 0.01), 2)
    canvas = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    canvas.paste(work, (pad, pad), work)

    # Scale to final target size
    img = canvas.resize((SIZE, SIZE), Image.LANCZOS)

    # Draw status dot in bottom-right corner
    r, g, b = _STATUS_DOT_COLOR.get(status, (0xF5, 0x9E, 0x0B))
    x0 = SIZE - DOT - MARGIN
    y0 = SIZE - DOT - MARGIN
    draw = ImageDraw.Draw(img)
    # White border for contrast on any background
    draw.ellipse((x0 - 2, y0 - 2, x0 + DOT + 2, y0 + DOT + 2), fill=(255, 255, 255, 255))
    draw.ellipse((x0, y0, x0 + DOT, y0 + DOT), fill=(r, g, b, 255))
    return img


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
        self._on_reload_config: Callable[[], None] | None = None
        self._settings_server: object | None = None  # SettingsServer, set by __main__
        # Queue for icon/title updates from background threads; drained on the
        # pystray thread to avoid AppKit/Win32 cross-thread mutation.
        self._update_queue: queue.Queue[str] = queue.Queue()

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
        title = self._build_title(status)
        img = _build_status_icon(status)

        self._after_start = after_start
        self._icon = pystray.Icon(
            name="runway-sidecar",
            icon=img,
            title=title,
            menu=self._build_menu(),
        )
        self._icon.run(setup=self._on_tray_ready)
        self._icon = None  # signal _drain_updates to exit

    def _on_tray_ready(self, icon: pystray.Icon) -> None:
        """Called by pystray once the icon is running; fires the after_start hook."""
        icon.visible = True
        if self._after_start:
            self._after_start()
        # Drain any status updates that arrived before the icon was ready, then
        # keep draining on a background thread so all pystray mutations happen
        # from the pystray-owned thread (required by AppKit / Win32).
        threading.Thread(target=self._drain_updates, daemon=True).start()

    def _drain_updates(self) -> None:
        """Consume status updates from the queue and apply them to the icon."""
        while self._icon is not None:
            try:
                status = self._update_queue.get(timeout=1)
            except queue.Empty:
                continue
            if self._icon is None:
                break
            self._icon.icon = _build_status_icon(status)
            self._icon.title = self._build_title(status)
            self._icon.update_menu()

    def _build_title(self, status: str) -> str:
        """Return the tray tooltip title with live stats and optional update notice."""
        base = _STATUS_TITLE.get(status, "Runway Sidecar")
        if self._update_available:
            base = f"{base} (update available)"
        stats = self._daemon.stats_summary
        return f"{base}\n{stats}"

    def _update_icon(self, status: str) -> None:
        """Enqueue a status change; the drain thread applies it on the pystray thread."""
        if self._icon is None:
            return
        self._update_queue.put(status)

    # ------------------------------------------------------------------
    # Menu helpers
    # ------------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        """Construct the full context menu."""

        def title_text(item: pystray.MenuItem) -> str:
            return self._build_title(self._daemon.status)

        def pause_resume_text(item: pystray.MenuItem) -> str:
            return "Resume" if self._paused else "Pause"

        def on_open_settings(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            if self._settings_server is not None:
                self._settings_server.open()  # type: ignore[attr-defined]

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

        def on_reload_config(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            if self._on_reload_config is not None:
                self._on_reload_config()

        def on_check_updates(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            webbrowser.open(RELEASES_URL)

        def on_launch_at_login(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            if is_login_item_installed():
                remove_login_item()
            else:
                install_login_item()
            icon.update_menu()

        def on_about(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            from sidecar_app import __version__

            try:
                icon.notify(f"Version {__version__}", "Runway Sidecar")
            except Exception:
                pass  # notifications not supported on all platforms

        def on_quit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
            icon.stop()
            self._daemon.stop()

        return pystray.Menu(
            pystray.MenuItem(title_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings…", on_open_settings),
            pystray.MenuItem("Open Dashboard", on_open_dashboard),
            pystray.MenuItem("Run Now", on_run_now),
            pystray.MenuItem(pause_resume_text, on_pause_resume),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Launch at Login",
                on_launch_at_login,
                checked=lambda item: is_login_item_installed(),  # noqa: ARG005
            ),
            pystray.MenuItem("Reload Config", on_reload_config),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Check for Updates…", on_check_updates),
            pystray.MenuItem("About", on_about),
            pystray.MenuItem("Quit", on_quit),
        )
