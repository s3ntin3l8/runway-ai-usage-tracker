"""Sidecar identity helpers.

Deliberate mirrors of ``app.services.account_identity.normalize_sidecar_id``
and ``canonical_account_id`` so
the frozen sidecar binary stays self-contained and never imports ``app.*`` (same
pattern as ``update_check.py`` ↔ ``app/services/sidecar_version_checker.py``).
Keep the two copies in sync.
"""

from __future__ import annotations

import re

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def normalize_sidecar_id(raw: str) -> str:
    """Stable sidecar id: lowercased first DNS label.

    Collapses `Macbook.in.example.de`, `macbook.local`, and `macbook` to one id
    so a host that flips between its FQDN and Bonjour name keeps a single
    registry entry instead of spawning duplicates. IPv4 literals and dot-less
    sentinels (`local`) pass through lowercased.
    """
    h = (raw or "").strip()
    if not h or _IPV4.match(h):
        return h.lower()
    return h.split(".", 1)[0].lower()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def canonical_account_id(raw: str | None) -> str:
    """Canonical ``account_id``: blank → ``"default"``, emails lowercased,
    anything else stripped but otherwise verbatim. Mirrors the server's rule
    so the sidecar's stamps, watermark keys and hint comparisons agree with
    the ids the server stores."""
    s = (raw or "").strip()
    if not s:
        return "default"
    if _EMAIL_RE.match(s):
        return s.lower()
    return s
