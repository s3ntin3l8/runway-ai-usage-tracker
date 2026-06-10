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
_GITHUB_EDGE_REFS_URL = "https://api.github.com/repos/s3ntin3l8/runway/git/refs/tags/edge"
_CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # 24h
_HTTP_TIMEOUT_SECONDS = 10.0


def parse_channel(version: str | None) -> tuple[str, str | None]:
    """Classify a reported sidecar version.

    Edge builds are stamped ``<base>+edge.<short_sha>`` by the build workflow, so
    a ``+edge.`` local segment identifies an edge build and carries the commit
    sha it was built from. Returns ``("edge", short_sha)`` or ``("stable", None)``.
    """
    if version and "+edge." in version:
        return "edge", version.split("+edge.", 1)[1] or None
    return "stable", None


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
        self._latest_edge_sha: str | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    def get_latest(self) -> str | None:
        """Return the cached latest tag (without the `v` prefix), or None."""
        return self._latest

    def get_latest_edge_sha(self) -> str | None:
        """Return the cached commit sha of the rolling `edge` tag, or None."""
        return self._latest_edge_sha

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
        """Refresh the cached latest stable tag and edge-tag sha.

        Both fetches are best-effort: on any failure the previous cache is kept
        and the fleet API degrades to "unknown latest" for that channel.
        """
        headers = {"User-Agent": "Runway-Server-VersionChecker"}
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.get(self._api_url, headers=headers)
                if resp.status_code == 200:
                    tag = str(resp.json().get("tag_name", "")).lstrip("v").strip()
                    if tag:
                        if tag != self._latest:
                            logger.info(f"Latest sidecar release is {tag}")
                        self._latest = tag
                else:
                    logger.debug(
                        f"Sidecar version check returned HTTP {resp.status_code}; keeping cache"
                    )

                # Rolling `edge` prerelease: track the tag's commit sha so edge
                # sidecars can be flagged when the tag moves. A 404 here is
                # normal before the first edge build exists.
                edge_resp = await client.get(_GITHUB_EDGE_REFS_URL, headers=headers)
                if edge_resp.status_code == 200:
                    sha = str(edge_resp.json().get("object", {}).get("sha", "")).strip()
                    if sha:
                        if sha != self._latest_edge_sha:
                            logger.info(f"Latest sidecar edge build is {sha[:12]}")
                        self._latest_edge_sha = sha
        except Exception as exc:
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


def is_update_available(
    current: str | None,
    latest: str | None,
    latest_edge_sha: str | None = None,
) -> bool:
    """Whether *current* is behind the newest build on its channel. False on ambiguity.

    Edge builds (``<base>+edge.<sha>``) are compared by commit sha against the
    rolling `edge` tag (*latest_edge_sha*), since the rolling prerelease never
    bumps the public version. Stable builds compare against *latest* via PEP 440.
    `latest`/`latest_edge_sha` of None → unknown channel head → don't flag.
    """
    if not current:
        return False

    channel, embedded_sha = parse_channel(current)
    if channel == "edge":
        # Tag ref returns the full sha; the build stamps a short prefix.
        if not latest_edge_sha or not embedded_sha:
            return False
        return not latest_edge_sha.startswith(embedded_sha)

    if not latest:
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
