"""Background service that tracks the latest published sidecar release.

Polls the GitHub Releases API once on startup and every 24h thereafter, caches
the latest tag in memory, and exposes a `get_latest()` accessor so the fleet
API can flag sidecars running an older version. Network failures keep the
previous cache (or `None` if we have never succeeded) — the fleet API treats
`None` as "unknown latest, don't flag anything."
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API_URL = "https://api.github.com/repos/s3ntin3l8/runway/releases/latest"
_CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # 24h
_HTTP_TIMEOUT_SECONDS = 10.0


class SidecarVersionChecker:
    """Periodically refreshes the cached "latest sidecar release" tag."""

    def __init__(
        self,
        api_url: str = _GITHUB_API_URL,
        check_interval: int = _CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._api_url = api_url
        self._interval = check_interval
        self._latest: str | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    def get_latest(self) -> str | None:
        """Return the cached latest tag (without the `v` prefix), or None."""
        return self._latest

    def start(self) -> None:
        """Kick off the background refresh loop."""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="sidecar-version-checker")
        logger.info("Sidecar version checker started.")

    async def stop(self) -> None:
        """Cancel the background loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.debug("Sidecar version checker task cancelled during shutdown")
            self._task = None
        logger.info("Sidecar version checker stopped.")

    async def check_now(self) -> str | None:
        """Fetch the latest release tag once and update the cache on success."""
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    self._api_url,
                    headers={"User-Agent": "Runway-Server-VersionChecker"},
                )
            if resp.status_code != 200:
                logger.debug(
                    f"Sidecar version check returned HTTP {resp.status_code}; keeping cache"
                )
                return self._latest
            data = resp.json()
            tag = str(data.get("tag_name", "")).lstrip("v").strip()
            if tag:
                if tag != self._latest:
                    logger.info(f"Latest sidecar release is {tag}")
                self._latest = tag
        except Exception as exc:
            # Network failure, JSON parse error, etc — silently keep the
            # previous cache. The fleet API degrades to "unknown latest."
            logger.debug(f"Sidecar version check failed: {exc}")
        return self._latest

    async def _run_loop(self) -> None:
        """Initial check, then sleep/check every `interval` seconds."""
        await self.check_now()
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break
            if self._running:
                await self.check_now()


def is_update_available(current: str | None, latest: str | None) -> bool:
    """Compare two version strings (without `v` prefix). Returns False on any ambiguity.

    `latest` of None → unknown; `current` of None/empty/unparseable → can't compare.
    Both sides parsed via packaging.Version when possible; falls back to string
    inequality (favouring "no update" on tie).
    """
    if not current or not latest:
        return False
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(latest) > Version(current)
        except InvalidVersion:
            return False
    except Exception:
        return False


sidecar_version_checker = SidecarVersionChecker()
