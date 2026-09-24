"""Receive ``runway-sidecar://`` deep links from the OS.

* **Windows** registers the scheme in ``HKCU\\Software\\Classes`` (NSIS
  installer), so a click launches ``RunwaySidecar.exe "<url>"``. If a tray is
  already running, that second process hands the URL to it through the running
  settings server's token-authenticated ``/pair-request`` endpoint
  (:func:`forward_to_running`) and exits.
* **macOS** declares the scheme in ``CFBundleURLTypes`` (macos.spec); Launch
  Services delivers it to the single running instance as a ``kAEGetURL``
  Apple Event, handled by :func:`install_macos_url_handler`.

Either way the URL only ever opens the pairing confirmation page — see
``SettingsServer.open_pair``.
"""

from __future__ import annotations

import json
import logging
import pathlib
import sys
from collections.abc import Callable
from urllib import request

logger = logging.getLogger(__name__)

CONTROL_FILE = "tray-control.json"
# Apple Event four-char codes (Carbon AE headers).
_K_INTERNET_EVENT_CLASS = 0x4755524C  # kInternetEventClass 'GURL'
_K_AE_GET_URL = 0x4755524C  # kAEGetURL 'GURL'
_KEY_DIRECT_OBJECT = 0x2D2D2D2D  # keyDirectObject '----'

# Keep the Objective-C handler object alive for the process lifetime.
_macos_handler: object | None = None


def pair_url_from_argv(argv: list[str]) -> str | None:
    """The first ``runway-sidecar:`` argument, if the OS launched us with one."""
    from scripts.sidecar_pkg.pairing import is_pair_url

    return next((a for a in argv[1:] if is_pair_url(a)), None)


def forward_to_running(control_path: pathlib.Path, url: str, timeout: float = 5.0) -> bool:
    """Hand *url* to the already-running tray. True if it accepted it."""
    try:
        info = json.loads(control_path.read_text(encoding="utf-8"))
        port = int(info["port"])
        token = str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("No running tray control file at %s", control_path)
        return False
    req = request.Request(  # noqa: S310 — fixed loopback URL
        f"http://127.0.0.1:{port}/pair-request",
        data=json.dumps({"url": url}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Runway-Control": token},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except OSError as exc:
        logger.warning("Could not hand the pairing link to the running sidecar: %s", exc)
        return False


def install_macos_url_handler(callback: Callable[[str], None]) -> bool:
    """Route ``kAEGetURL`` Apple Events (``runway-sidecar://…``) to *callback*.

    Must run before the NSApplication run loop starts (i.e. before
    ``pystray.Icon.run``) so a link that *launched* the app is delivered too.
    Returns False when not on macOS or PyObjC is unavailable.
    """
    global _macos_handler
    if sys.platform != "darwin":
        return False
    try:
        import objc  # type: ignore[import-not-found]
        from Foundation import NSAppleEventManager, NSObject  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("PyObjC unavailable; runway-sidecar:// links won't be handled")
        return False

    class _URLHandler(NSObject):  # type: ignore[misc]
        def handleURLEvent_withReplyEvent_(self, event, _reply):  # noqa: N802
            try:
                url = event.paramDescriptorForKeyword_(_KEY_DIRECT_OBJECT).stringValue()
                if url:
                    callback(str(url))
            except Exception:
                logger.exception("Failed to handle URL event")

    handler = _URLHandler.alloc().init()
    NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(
        handler,
        objc.selector(handler.handleURLEvent_withReplyEvent_, signature=b"v@:@@"),
        _K_INTERNET_EVENT_CLASS,
        _K_AE_GET_URL,
    )
    _macos_handler = handler
    return True
