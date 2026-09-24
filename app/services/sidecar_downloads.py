"""Resolve the downloadable sidecar builds for the Fleet page's download card.

Fetches the stable (``/releases/latest``) or rolling ``edge`` release from
GitHub and classifies its assets into installers (``.dmg`` / ``-setup.exe``)
and portable payloads (``.zip`` / ``.tar.gz``). Results are cached per channel
for an hour so a busy dashboard never hammers the (rate-limited, unauthenticated)
GitHub API; a failed fetch keeps serving the last good answer.

The asset-name grammar mirrors ``scripts/sidecar_pkg/asset_names.py`` — the
sidecar's own source of truth, which the server image does not ship.
``tests/unit/test_sidecar_downloads.py`` asserts the two classify every
published name identically.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from app.models.schemas import SidecarDownloadAsset, SidecarDownloadsResponse

logger = logging.getLogger(__name__)

_REPO_API = "https://api.github.com/repos/s3ntin3l8/runway-ai-usage-tracker"
RELEASES_URL = "https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases"
_RELEASE_API = {
    "stable": f"{_REPO_API}/releases/latest",
    "edge": f"{_REPO_API}/releases/tags/edge",
}
_CACHE_TTL_SECONDS = 60 * 60
_HTTP_TIMEOUT_SECONDS = 10.0

# Keep in sync with scripts/sidecar_pkg/asset_names.py:_ASSET_RE (contract-tested).
_ASSET_RE = re.compile(
    r"^Runway-Sidecar-(?P<platform>macOS|Windows|Linux-CLI|Linux)-(?P<label>edge|v[0-9][^-]*?(?:-[0-9A-Za-z.]+)?)"
    r"(?P<suffix>\.dmg|-setup\.exe|\.zip|\.tar\.gz)$"
)
_PLATFORM_ORDER = {"macOS": 0, "Windows": 1, "Linux": 2, "Linux-CLI": 3}


def classify(name: str) -> tuple[str, str, str] | None:
    """``(platform, label, kind)`` for a primary sidecar asset name, else ``None``."""
    m = _ASSET_RE.match(name)
    if not m:
        return None
    kind = "installer" if m["suffix"] in (".dmg", "-setup.exe") else "payload"
    return m["platform"], m["label"], kind


def parse_release(release: dict, channel: str) -> SidecarDownloadsResponse:
    """Turn a GitHub release object into the typed download list."""
    raw = release.get("assets") or []
    urls = {a.get("name"): a.get("browser_download_url") for a in raw}
    assets: list[SidecarDownloadAsset] = []
    for a in raw:
        name = str(a.get("name") or "")
        parsed = classify(name)
        url = a.get("browser_download_url")
        if not parsed or not url:
            continue
        platform, _label, kind = parsed
        assets.append(
            SidecarDownloadAsset(
                platform=platform,
                kind=kind,
                name=name,
                url=url,
                size=a.get("size"),
                sha256_url=urls.get(f"{name}.sha256"),
            )
        )
    assets.sort(key=lambda x: (_PLATFORM_ORDER.get(x.platform, 9), x.kind != "installer"))
    return SidecarDownloadsResponse(
        channel=channel,
        version=release.get("tag_name"),
        published_at=release.get("published_at"),
        release_url=release.get("html_url") or RELEASES_URL,
        checksums_url=urls.get("SHA256SUMS.txt"),
        assets=assets,
    )


class SidecarDownloads:
    """Per-channel cached view of the latest sidecar release assets."""

    def __init__(self, ttl: float = _CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._cache: dict[str, tuple[float, SidecarDownloadsResponse]] = {}
        self._lock = asyncio.Lock()

    async def get(self, channel: str) -> SidecarDownloadsResponse:
        channel = "edge" if channel == "edge" else "stable"
        cached = self._cache.get(channel)
        if cached and time.monotonic() - cached[0] < self._ttl:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(channel)  # another request may have refreshed it
            if cached and time.monotonic() - cached[0] < self._ttl:
                return cached[1]
            fresh = await self._fetch(channel)
            if fresh.error is None:
                self._cache[channel] = (time.monotonic(), fresh)
                return fresh
            # Degrade to the last good answer rather than an empty card.
            return cached[1] if cached else fresh

    async def _fetch(self, channel: str) -> SidecarDownloadsResponse:
        url = _RELEASE_API[channel]
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers={"User-Agent": "Runway-Server-Downloads"})
            if resp.status_code == 200:
                return parse_release(resp.json(), channel)
            error = f"GitHub returned HTTP {resp.status_code}"
        except Exception as exc:  # network / JSON — degrade, never 500 the card
            error = f"GitHub unreachable: {type(exc).__name__}"
        logger.warning("Sidecar downloads (%s): %s", channel, error)
        return SidecarDownloadsResponse(channel=channel, release_url=RELEASES_URL, error=error)


sidecar_downloads = SidecarDownloads()
