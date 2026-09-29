"""Background service that tracks the latest published sidecar release.

Polls GitHub Releases once on startup and every 24h thereafter, caches the
latest stable and beta tags plus the rolling edge sha, and exposes accessors so
the fleet API can flag sidecars running an older version. Network failures keep
the previous cache (or `None` if never fetched) — the fleet API treats `None`
as "unknown latest, don't flag anything."

Because release-please tags the whole repo, the cached latest tag doubles as the
latest **server** release — `app/api/endpoints/system.py` reuses `get_latest()`
(and `check_now()` for the manual "check for updates" trigger) to drive the
in-app server-update banner.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.services.release_metadata import _BETA_TAG_RE, latest_beta_release, normalize_version

logger = logging.getLogger(__name__)

_GITHUB_API_URL = "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/releases/latest"
_BETA_RELEASES_API_URL = (
    "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/releases?per_page=100"
)
_GITHUB_EDGE_REFS_URL = (
    "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker/git/refs/tags/edge"
)
_CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # 24h
_HTTP_TIMEOUT_SECONDS = 10.0


def parse_channel(version: str | None) -> tuple[str, str | None]:
    """Classify a reported sidecar version.

    Edge builds are stamped ``<base>+edge.<short_sha>``; numbered prereleases
    such as ``3.0.0-beta.1`` belong to the beta channel.
    """
    if not version:
        return "stable", None
    normalized = normalize_version(version)
    if "+edge." in normalized:
        return "edge", normalized.split("+edge.", 1)[1] or None
    if _BETA_TAG_RE.fullmatch(f"v{normalized}"):
        return "beta", None
    return "stable", None


class SidecarVersionChecker:
    """Periodically refreshes cached stable/beta release tags and edge sha."""

    def __init__(
        self,
        api_url: str = _GITHUB_API_URL,
        check_interval: int = _CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._api_url = api_url
        self._interval = check_interval
        self._latest: str | None = None
        self._latest_beta: str | None = None
        self._latest_edge_sha: str | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    def get_latest(self) -> str | None:
        """Return the cached latest tag (without the `v` prefix), or None."""
        return self._latest

    def get_latest_edge_sha(self) -> str | None:
        """Return the cached commit sha of the rolling `edge` tag, or None."""
        return self._latest_edge_sha

    def get_latest_beta(self) -> str | None:
        """Return the newest numbered beta tag without its `v` prefix."""
        return self._latest_beta

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
        """Refresh the cached stable/beta tags and edge-tag sha.

        All fetches are best-effort: on any failure the previous cache is kept
        and the fleet API degrades to "unknown latest" for that channel.
        """
        headers = {"User-Agent": "Runway-Server-VersionChecker"}
        try:
            # follow_redirects: GitHub 301-redirects API calls after a repo
            # rename. httpx does NOT follow by default — without this a rename
            # silently breaks the whole update check (see the URL constants).
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                resp = await client.get(self._api_url, headers=headers)
                if resp.status_code == 200:
                    tag = str(resp.json().get("tag_name", "")).lstrip("v").strip()
                    if tag:
                        if tag != self._latest:
                            logger.info(f"Latest sidecar release is {tag}")
                        self._latest = tag
                else:
                    # WARN, not DEBUG: a persistent non-200 here means the fleet
                    # silently stops flagging outdated sidecars.
                    logger.warning(
                        f"Sidecar version check returned HTTP {resp.status_code} "
                        f"for {self._api_url}; keeping cache"
                    )

                beta_resp = await client.get(_BETA_RELEASES_API_URL, headers=headers)
                if beta_resp.status_code == 200:
                    beta_release = latest_beta_release(beta_resp.json())
                    beta_tag = (
                        str(beta_release.get("tag_name", "")).lstrip("v").strip()
                        if beta_release
                        else None
                    )
                    if beta_tag != self._latest_beta:
                        if beta_tag:
                            logger.info(f"Latest sidecar beta release is {beta_tag}")
                        self._latest_beta = beta_tag
                else:
                    logger.warning(
                        f"Sidecar beta check returned HTTP {beta_resp.status_code}; keeping cache"
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
                elif edge_resp.status_code != 404:
                    logger.warning(
                        f"Sidecar edge-tag check returned HTTP {edge_resp.status_code}; keeping cache"
                    )
        except Exception as exc:
            logger.warning(f"Sidecar version check failed: {exc}")
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
    latest_beta: str | None = None,
    target_channel: str | None = None,
) -> bool:
    """Whether *current* is behind the newest build on its channel. False on ambiguity.

    Edge builds compare commit sha against the rolling `edge` tag. A configured
    target channel overrides the build's channel so beta installs can be
    promoted to stable by changing the fleet setting. Missing channel heads mean
    "unknown" and do not flag an update.
    """
    if not current:
        return False

    channel, embedded_sha = parse_channel(current)
    channel = target_channel if target_channel in ("stable", "beta", "edge") else channel
    if channel == "edge":
        # Tag ref returns the full sha; the build stamps a short prefix.
        if embedded_sha:
            if not latest_edge_sha:
                return False
            return not latest_edge_sha.startswith(embedded_sha)
        # A non-edge build opted into edge; like the sidecar updater, offer the
        # stable release until an edge build identity is available.
        channel = "stable"

    latest_target = latest_beta if channel == "beta" else latest
    if not latest_target:
        return False
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return False
    try:
        return Version(latest_target) > Version(current)
    except InvalidVersion:
        return False


sidecar_version_checker = SidecarVersionChecker()
