"""Update-availability check for the Runway sidecar (notify only — no download).

Shared by the headless CLI (`scripts/sidecar.py`) and the desktop tray
(`sidecar_app/updater.py`). The server's own checker lives in
`app/services/sidecar_version_checker.py`; this module is the sidecar-side
equivalent and never imports `app.*` so the frozen sidecar binary stays
self-contained.

Channel model:
  * A build's version string is the source of truth for its channel. Edge
    builds are stamped `<base>+edge.<short_sha>` (see the build workflow), so a
    `+edge.` local segment means "this is an edge build" and carries the commit
    sha it was built from.
  * Stable channel → compare the running version against the latest GitHub
    release (`/releases/latest`, which excludes prereleases) via PEP 440.
  * Edge channel → the rolling `edge` prerelease never bumps the public version,
    so "is there a newer build?" is decided by commit-sha inequality against the
    `edge` git tag, not version ordering.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from typing import Any
from urllib import error, request

logger = logging.getLogger(__name__)

_LATEST_URL = "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/releases/latest"
_BETA_RELEASES_API_URL = (
    "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/releases?per_page=100"
)
_EDGE_REFS_URL = "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/git/refs/tags/edge"
_RELEASES_URL = "https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases"
_CHECK_INTERVAL_SECONDS = 86400  # 24h
_TIMEOUT_SECONDS = 10
_BETA_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+-beta\.\d+$")


def normalize_version(version: str) -> str:
    """Remove whitespace, an optional BOM, and all leading v prefixes."""
    return version.strip().lstrip("\ufeff").strip().lstrip("vV")


def parse_channel(version: str | None) -> tuple[str, str | None]:
    """Classify a version string.

    Returns ``("edge", short_sha)`` for edge builds, ``("beta", None)`` for
    numbered beta releases, and ``("stable", None)`` otherwise.
    """
    if version and "+edge." in version:
        normalized = normalize_version(version)
        return "edge", normalized.split("+edge.", 1)[1] or None
    if version:
        normalized = normalize_version(version)
        if _BETA_TAG_RE.fullmatch(f"v{normalized}"):
            return "beta", None
    return "stable", None


def _get_json(url: str) -> Any:
    from scripts.sidecar_pkg.tls import build_context

    req = request.Request(  # noqa: S310 — fixed https GitHub API URL
        url, headers={"User-Agent": "Runway-Sidecar-UpdateCheck"}
    )
    # certifi-backed context so the frozen binary trusts GitHub's public cert;
    # GitHub always presents a valid cert, so never honour the insecure opt-in.
    ctx = build_context(url, insecure=False)
    with request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=ctx) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _stable_update(current: str) -> str | None:
    """Latest stable tag (without ``v``) if it is newer than *current*, else None."""
    try:
        latest = str(_get_json(_LATEST_URL).get("tag_name", "")).lstrip("v").strip()
    except (error.URLError, OSError, ValueError, KeyError):
        return None
    if not latest:
        return None
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return latest if Version(latest) > Version(current) else None
        except InvalidVersion:
            return None
    except Exception:
        return None


def latest_beta_release(releases: object) -> dict | None:
    """Return the newest numbered beta release from GitHub's releases list."""
    if not isinstance(releases, list):
        return None
    candidates = [
        release
        for release in releases
        if isinstance(release, dict)
        and release.get("prerelease") is True
        and _BETA_TAG_RE.fullmatch(str(release.get("tag_name") or ""))
    ]
    if not candidates:
        return None
    try:
        from packaging.version import Version

        return max(candidates, key=lambda release: Version(str(release["tag_name"]).lstrip("vV")))
    except Exception:
        return None


def _beta_update(current: str) -> str | None:
    """Latest beta version if newer than *current*, else None."""
    try:
        release = latest_beta_release(_get_json(_BETA_RELEASES_API_URL))
    except (error.URLError, OSError, ValueError, KeyError):
        return None
    if not release:
        return None
    latest = str(release.get("tag_name", "")).lstrip("vV").strip()
    if not latest:
        return None
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return latest if Version(latest) > Version(current) else None
        except InvalidVersion:
            return None
    except Exception:
        return None


def _edge_update(embedded_sha: str) -> str | None:
    """Current ``edge`` tag commit sha if it differs from *embedded_sha*, else None.

    *embedded_sha* is the short sha stamped into an edge build; the tag ref
    returns the full 40-char sha, so a prefix match means "same build."
    """
    try:
        latest_sha = str(_get_json(_EDGE_REFS_URL).get("object", {}).get("sha", "")).strip()
    except (error.URLError, OSError, ValueError, KeyError):
        return None
    if not latest_sha or not embedded_sha:
        return None
    return latest_sha[:12] if not latest_sha.startswith(embedded_sha) else None


def check_once(current_version: str, channel: str | None = None) -> str | None:
    """Run one update check.

    *channel* overrides the version-inferred channel (used to honour the
    dashboard-synced setting). Returns a short human-readable description of the
    available update, or None when up to date / the check failed.
    """
    own_channel, embedded_sha = parse_channel(current_version)
    active = (channel or own_channel or "stable").lower()

    if active == "edge" and embedded_sha:
        sha = _edge_update(embedded_sha)
        return f"edge build {sha}" if sha else None

    if active == "beta":
        beta = _beta_update(current_version)
        return f"v{beta}" if beta else None

    # Stable channel, or a stable binary that opted into edge (no sha to diff —
    # fall back to offering stable releases so it is never left blind).
    newer = _stable_update(current_version)
    return f"v{newer}" if newer else None


class UpdateCheckThread:
    """Background daemon thread: check on start, then every 24h.

    Logs a warning when a newer build is available. If *on_update_available* is
    supplied it is also invoked with the update description — the self-update
    apply layer hooks in here when the ``auto_update`` config flag is on; with no
    callback the behaviour is notify-only.
    """

    def __init__(
        self,
        current_version: str,
        channel_getter: Callable[[], str | None] | None = None,
        interval: int = _CHECK_INTERVAL_SECONDS,
        on_update_available: Callable[[str], None] | None = None,
    ) -> None:
        self._current = current_version
        self._channel_getter = channel_getter or (lambda: None)
        self._interval = interval
        self._on_update_available = on_update_available
        self._stop = threading.Event()
        self._poke_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="runway-update-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._poke_event.set()

    def poke(self) -> None:
        """Re-check now instead of at the next interval (e.g. once the server has
        told us the fleet's auto-update flag and channel)."""
        self._poke_event.set()

    def _check(self) -> None:
        try:
            available = check_once(self._current, self._channel_getter())
        except Exception:
            logger.debug("Update check failed", exc_info=True)
            return
        if available:
            logger.warning(
                f"A newer Runway sidecar is available ({available}). Download: {_RELEASES_URL}"
            )
            if self._on_update_available is not None:
                try:
                    self._on_update_available(available)
                except Exception:
                    logger.debug("on_update_available callback failed", exc_info=True)

    def _loop(self) -> None:
        self._check()
        while not self._stop.is_set():
            self._poke_event.wait(self._interval)
            if self._stop.is_set():
                break
            self._poke_event.clear()
            self._check()
