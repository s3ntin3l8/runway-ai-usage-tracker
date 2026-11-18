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
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib import error, request

from scripts.sidecar_pkg.update_limits import MAX_METADATA_BYTES, bounded_read

logger = logging.getLogger(__name__)

_LATEST_URL = "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/releases/latest"
_BETA_RELEASES_API_URL = (
    "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/releases?per_page=100"
)
_EDGE_REFS_URL = "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/git/refs/tags/edge"
_RELEASES_URL = "https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases"
_CHECK_INTERVAL_SECONDS = 86400  # 24h
_FAILURE_LOG_COOLDOWN_SECONDS = 3600  # 1h
_TIMEOUT_SECONDS = 10
_BETA_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+-beta\.\d+$")


class GitHubRateLimitError(OSError):
    """Raised when GitHub API rejects a request due to rate limiting."""


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


def _header_get(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    value = headers.get(name)
    if value is not None:
        return str(value)
    items = getattr(headers, "items", None)
    if callable(items):
        target = name.lower()
        for key, val in items():
            if str(key).lower() == target:
                return str(val)
    return None


def _format_rate_limit_reset(reset_header: str | None) -> str:
    if not reset_header:
        return "unknown time"
    try:
        ts = int(float(reset_header.strip()))
    except (TypeError, ValueError):
        return "unknown time"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def fetch_github_json(
    url: str,
    *,
    user_agent: str = "Runway-Sidecar-UpdateCheck",
    timeout: int = _TIMEOUT_SECONDS,
    context: Any = None,
) -> Any:
    """Fetch JSON from GitHub, raising GitHubRateLimitError when quota is exhausted."""
    if context is None:
        from scripts.sidecar_pkg.tls import build_context

        # certifi-backed context so the frozen binary trusts GitHub's public cert;
        # GitHub always presents a valid cert, so never honour the insecure opt-in.
        context = build_context(url, insecure=False)
    req = request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310
    try:
        with request.urlopen(req, timeout=timeout, context=context) as resp:  # noqa: S310
            return json.loads(bounded_read(resp, MAX_METADATA_BYTES).decode())
    except error.HTTPError as exc:
        if exc.code in (403, 429):
            remaining = _header_get(exc.headers, "X-RateLimit-Remaining")
            if remaining is not None and remaining.strip() == "0":
                reset_at = _format_rate_limit_reset(_header_get(exc.headers, "X-RateLimit-Reset"))
                raise GitHubRateLimitError(
                    f"GitHub API rate limit reached, resets at {reset_at}"
                ) from exc
        raise


def _get_json(url: str) -> Any:
    return fetch_github_json(url)


def _stable_update(current: str, *, strict: bool = False) -> str | None:
    """Latest stable tag (without ``v``) if it is newer than *current*, else None."""
    try:
        release = _get_json(_LATEST_URL)
        if not isinstance(release, dict):
            raise ValueError("stable release response is not an object")
        latest = str(release.get("tag_name", "")).lstrip("v").strip()
    except (error.URLError, OSError, ValueError, KeyError):
        if strict:
            raise
        return None
    if not latest:
        if strict:
            raise ValueError("stable release has no tag")
        return None
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return latest if Version(latest) > Version(current) else None
        except InvalidVersion:
            if strict:
                raise
            return None
    except Exception:
        if strict:
            raise
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


def _beta_update(current: str, *, strict: bool = False) -> str | None:
    """Latest beta version if newer than *current*, else None."""
    try:
        releases = _get_json(_BETA_RELEASES_API_URL)
    except (error.URLError, OSError, ValueError, KeyError):
        if strict:
            raise
        return None
    if strict and not isinstance(releases, list):
        raise ValueError("beta releases response is not a list")
    release = latest_beta_release(releases)
    if not release:
        return None
    latest = str(release.get("tag_name", "")).lstrip("vV").strip()
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return latest if Version(latest) > Version(current) else None
        except InvalidVersion:
            if strict:
                raise
            return None
    except Exception:
        if strict:
            raise
        return None


def _edge_update(embedded_sha: str, *, strict: bool = False) -> str | None:
    """Current ``edge`` tag commit sha if it differs from *embedded_sha*, else None.

    *embedded_sha* is the short sha stamped into an edge build; the tag ref
    returns the full 40-char sha, so a prefix match means "same build."
    """
    embedded_sha = embedded_sha.strip()
    if not embedded_sha:
        if strict:
            raise ValueError("edge build has no embedded commit SHA")
        logger.warning("Skipping edge update check: build has no embedded commit SHA")
        return None
    try:
        ref = _get_json(_EDGE_REFS_URL)
        if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
            raise ValueError("edge ref response has no object")
        latest_sha = str(ref["object"].get("sha", "")).strip()
    except (error.URLError, OSError, ValueError, KeyError):
        if strict:
            raise
        return None
    if not latest_sha:
        if strict:
            raise ValueError("edge ref has no commit SHA")
        return None
    return latest_sha[:12] if not latest_sha.startswith(embedded_sha) else None


def check_once(
    current_version: str, channel: str | None = None, *, strict: bool = False
) -> str | None:
    """Run one update check.

    *channel* overrides the version-inferred channel (used to honour the
    dashboard-synced setting). Returns a short human-readable description of the
    available update, or None when up to date / the check failed. With *strict*,
    lookup and malformed-metadata errors propagate so an explicit user check can
    distinguish a failure from an up-to-date result.
    """
    own_channel, embedded_sha = parse_channel(current_version)
    active = (channel or own_channel or "stable").lower()

    if active == "edge" and embedded_sha:
        sha = _edge_update(embedded_sha, strict=strict)
        return f"edge build {sha}" if sha else None

    if active == "beta":
        beta = _beta_update(current_version, strict=strict)
        return f"v{beta}" if beta else None

    # Stable channel, or a stable binary that opted into edge (no sha to diff —
    # fall back to offering stable releases so it is never left blind).
    newer = _stable_update(current_version, strict=strict)
    return f"v{newer}" if newer else None


class FailureLogState:
    """Tracks the last logged update-check failure for 1-hour deduplication."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock
        self.last_message: str | None = None
        self.last_logged_at: float | None = None

    def now(self) -> float:
        return self._clock() if self._clock is not None else time.monotonic()

    def reset(self) -> None:
        self.last_message = None
        self.last_logged_at = None


def log_check_failure(
    exc: BaseException,
    state: FailureLogState,
    *,
    log: logging.Logger = logger,
) -> None:
    """Log an update-check failure once per hour at INFO (one line) and DEBUG (traceback)."""
    exc_detail = " ".join(str(exc).splitlines()).strip()
    msg = (
        f"Update check failed: {type(exc).__name__}: {exc_detail}"
        if exc_detail
        else f"Update check failed: {type(exc).__name__}"
    )
    now = state.now()
    if (
        state.last_message == msg
        and state.last_logged_at is not None
        and (now - state.last_logged_at) < _FAILURE_LOG_COOLDOWN_SECONDS
    ):
        return
    state.last_message = msg
    state.last_logged_at = now
    log.info(msg)
    log.debug("Update check failed", exc_info=True)


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
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._current = current_version
        self._channel_getter = channel_getter or (lambda: None)
        self._interval = interval
        self._on_update_available = on_update_available
        self._failure_state = FailureLogState(clock=clock)
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
            available = check_once(self._current, self._channel_getter(), strict=True)
        except Exception as exc:
            log_check_failure(exc, self._failure_state, log=logger)
            return
        self._failure_state.reset()
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
