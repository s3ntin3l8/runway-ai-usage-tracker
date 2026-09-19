"""Sidecar identity cache (issue #272).

Reads the server's ``/fleet/config`` to discover what
``(provider_id, account_id)`` rows exist (one per user-configured account),
caches a token-per-row map keyed by ``(provider_id, account_id)``, and
exposes ``provider_accounts()`` so the sidecar's per-account event
extraction can iterate the right tuples on each cycle.

This module deliberately does NOT redeem credentials — the redeem endpoint
lands with the first production caller in the follow-up PR (see PR #283
code review). Until then, the sidecar's local-credential path (JWT
discovery for chatgpt/anthropic, SQLite for opencode, etc.) keeps working.

The sidecar ships as a frozen PyInstaller binary, so this module is
stdlib-only.
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)


def fetch_credential_tokens(
    api_url: str,
    *,
    timeout: int = 10,
) -> dict[tuple[str, str], str]:
    """Fetch per-account credential tokens from ``GET /api/v1/fleet/config``.

    Returns a ``{(provider_id, account_id): token}`` map. Providers with no
    configured credentials are simply absent. The endpoint is
    intentionally unauthenticated; tokens are useless without the sidecar's
    shared ingest secret (``INGEST_API_KEY``).

    On HTTP error / non-200 response / malformed JSON / non-dict ``config``,
    returns an empty dict — sidecar collection must keep working when the
    server is unreachable (e.g. before the first heartbeat). The caller
    decides whether to fall back to legacy ``account_id="default"``
    stamping.
    """
    from urllib import error, request

    from scripts.sidecar_pkg.tls import build_context

    url = f"{api_url.rstrip('/')}/api/v1/fleet/config"
    req = request.Request(url)
    try:
        with request.urlopen(req, timeout=timeout, context=build_context(url)) as resp:
            if resp.getcode() != 200:
                logger.debug("fetch_credential_tokens: %s returned %s", url, resp.getcode())
                return {}
            payload = json.loads(resp.read().decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("fetch_credential_tokens: %s failed: %s", url, exc)
        return {}

    cfg = payload.get("config") if isinstance(payload, dict) else None
    providers = cfg.get("providers") if isinstance(cfg, dict) else None
    if not isinstance(providers, dict):
        return {}

    out: dict[tuple[str, str], str] = {}
    for provider_id, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        for acct in entry.get("accounts") or []:
            if not isinstance(acct, dict):
                continue
            aid = acct.get("account_id")
            token = acct.get("credential_token")
            if isinstance(aid, str) and isinstance(token, str) and aid and token:
                out[(provider_id, aid)] = token
    return out


class CredentialCache:
    """In-memory cache of server-issued credential tokens.

    The sidecar refreshes tokens on a heartbeat cadence. Between refreshes,
    cached values are reused so the collection cycle doesn't round-trip
    ``/fleet/config`` per cycle.

    The cache is *strictly* per-process and never persisted. A sidecar
    restart means a fresh fetch.
    """

    def __init__(self, *, ttl_seconds: int = 600) -> None:
        # ``ttl_seconds`` is the *refresh* cadence, not the token validity
        # (the server enforces that via CREDENTIAL_TOKEN_TTL_SECONDS).
        # Default 10 min — well under the server's default 1h token TTL so
        # cached tokens never outlive the server's view of them.
        self._ttl = ttl_seconds
        self._tokens_fetched_at: float = 0.0
        self._tokens: dict[tuple[str, str], str] = {}

    @property
    def tokens(self) -> dict[tuple[str, str], str]:
        return dict(self._tokens)

    def is_fresh(self, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.time()
        # A never-populated cache is never fresh. Distinguish via the
        # ``_tokens_fetched_at == 0.0`` sentinel that ``__init__`` writes.
        if self._tokens_fetched_at == 0.0:
            return False
        return (ts - self._tokens_fetched_at) < self._ttl

    def refresh_tokens(self, api_url: str) -> int:
        """Re-fetch ``/fleet/config`` and replace the cached tokens.

        Returns the count of (provider_id, account_id) pairs now in the
        cache.
        """
        self._tokens = fetch_credential_tokens(api_url)
        self._tokens_fetched_at = time.time()
        return len(self._tokens)

    def replace_tokens(self, tokens: dict[tuple[str, str], str]) -> int:
        """Bulk-set the token cache from an externally-fetched mapping.

        Used by ``run_collection`` when it fetches tokens directly (rather
        than going through ``refresh_tokens``). Same semantics.
        """
        self._tokens = dict(tokens)
        self._tokens_fetched_at = time.time()
        return len(self._tokens)

    def provider_accounts(self) -> dict[str, list[str]]:
        """Return ``{provider_id: [account_id, ...]}`` for all cached tokens.

        One entry per (provider_id, account_id) pair seen in the server's
        per-account config. Used by ``run_collection`` to drive the
        per-account event-extraction loop (issue #272).
        """
        out: dict[str, list[str]] = {}
        for pid, aid in self._tokens.keys():
            out.setdefault(pid, []).append(aid)
        return out
