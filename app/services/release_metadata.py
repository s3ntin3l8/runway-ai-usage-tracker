"""Pure helpers for interpreting Runway GitHub release metadata.

The server image intentionally ships only ``app/``. Keep these helpers in the
server package rather than importing the sidecar source tree.
"""

from __future__ import annotations

import re

_BETA_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+-beta\.\d+$")


def normalize_version(version: str) -> str:
    """Remove whitespace, an optional BOM, and all leading v prefixes."""
    return version.strip().lstrip("\ufeff").strip().lstrip("vV")


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
