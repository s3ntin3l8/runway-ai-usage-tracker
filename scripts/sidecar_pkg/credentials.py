"""Sidecar identity cache (issue #272).

Reads the server's ``/fleet/config`` and exposes two decoupled views:

- ``provider_accounts()`` returns ``{provider_id: [account_id, ...]}`` for
  every account the server has registered, **independent of whether a
  credential_token is attached**. Identity hints live in the public
  ``accounts[*].account_id`` field; tokens are conditional on
  ``INGEST_API_KEY`` being configured and the row having at least one
  credential. Tying the per-account event iteration to token *presence*
  would silently no-op the #272 attribution fix in any configuration
  that doesn't have a token yet — see PR #283 review.
- The token map (``tokens`` property) is the supplementary view that
  the future redeem endpoint will consume. Until then it's unused.

This module deliberately does NOT redeem credentials — the redeem endpoint
lands with the first production caller in the follow-up PR. Until then,
the sidecar's local-credential path (JWT discovery for chatgpt/anthropic,
SQLite for opencode, etc.) keeps working.

The sidecar ships as a frozen PyInstaller binary, so this module is
stdlib-only.
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)


def fetch_identity_hints(
    api_url: str,
    *,
    timeout: int = 10,
) -> dict[str, list[str]]:
    """Fetch per-account identity hints from ``GET /api/v1/fleet/config``.

    Returns ``{provider_id: [account_id, ...]}`` for every enabled row in
    the server's provider_configs. **Decoupled from token issuance** —
    rows without credentials and configurations with empty
    ``INGEST_API_KEY`` still contribute their ``account_id`` to this map,
    so the per-account event iteration that fixes #272 is not silently
    disabled by either condition (PR #283 review).

    On HTTP error / non-200 response / malformed JSON / non-dict
    ``config``, returns an empty dict — sidecar collection must keep
    working when the server is unreachable (e.g. before the first
    heartbeat). The caller decides whether to fall back to legacy
    single-account stamping.
    """
    from urllib import error, request

    from scripts.sidecar_pkg.tls import build_context

    url = f"{api_url.rstrip('/')}/api/v1/fleet/config"
    req = request.Request(url)
    try:
        with request.urlopen(req, timeout=timeout, context=build_context(url)) as resp:
            if resp.getcode() != 200:
                logger.debug("fetch_identity_hints: %s returned %s", url, resp.getcode())
                return {}
            payload = json.loads(resp.read().decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("fetch_identity_hints: %s failed: %s", url, exc)
        return {}

    cfg = payload.get("config") if isinstance(payload, dict) else None
    providers = cfg.get("providers") if isinstance(cfg, dict) else None
    if not isinstance(providers, dict):
        return {}

    out: dict[str, list[str]] = {}
    for provider_id, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        for acct in entry.get("accounts") or []:
            if not isinstance(acct, dict):
                continue
            aid = acct.get("account_id")
            if isinstance(aid, str) and aid:
                out.setdefault(provider_id, []).append(aid)
    return out


def fetch_credential_tokens(
    api_url: str,
    *,
    timeout: int = 10,
) -> dict[tuple[str, str], str]:
    """Fetch per-account credential tokens from ``GET /api/v1/fleet/config``.

    Returns a ``{(provider_id, account_id): token}`` map. Tokens are only
    issued by the server when ``INGEST_API_KEY`` is configured and the row
    has at least one credential — so this dict is *strictly a subset* of
    the identity hints from :func:`fetch_identity_hints`. The wider
    identity map is what drives per-account event iteration; this is the
    supplementary view reserved for the future redeem handler.

    On HTTP error / non-200 response / malformed JSON / non-dict ``config``,
    returns an empty dict.
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
    """In-memory cache of per-account identity hints and credential tokens.

    The sidecar refreshes the upstream view on a heartbeat cadence.
    Between refreshes, cached values are reused so the collection cycle
    doesn't round-trip ``/fleet/config`` per cycle.

    The cache is *strictly* per-process and never persisted. A sidecar
    restart means a fresh fetch.
    """

    def __init__(self, *, ttl_seconds: int = 600) -> None:
        # ``ttl_seconds`` is the *refresh* cadence, not the token validity
        # (the server enforces that via CREDENTIAL_TOKEN_TTL_SECONDS).
        # Default 10 min — well under the server's default 1h token TTL so
        # cached tokens never outlive the server's view of them.
        self._ttl = ttl_seconds
        self._identities_fetched_at: float = 0.0
        self._tokens_fetched_at: float = 0.0
        self._accounts: dict[str, list[str]] = {}
        # ``str | None`` because ``fetch_credential_tokens`` omits pairs
        # where the server didn't issue a token (empty INGEST_API_KEY,
        # row has no credentials). Future redeem code will skip pairs
        # whose value is ``None``.
        self._tokens: dict[tuple[str, str], str | None] = {}

    @property
    def tokens(self) -> dict[tuple[str, str], str | None]:
        return dict(self._tokens)

    def is_fresh(self, *, now: float | None = None) -> bool:
        """Return True when the cache was refreshed within ``ttl_seconds``.

        A never-populated cache is never fresh. Distinguish via the
        ``_identities_fetched_at == 0.0`` sentinel that ``__init__``
        writes.
        """
        ts = now if now is not None else time.time()
        if self._identities_fetched_at == 0.0:
            return False
        return (ts - self._identities_fetched_at) < self._ttl

    def refresh_from_config(
        self,
        api_url: str,
        *,
        fetch_tokens: bool = False,
    ) -> tuple[int, int]:
        """Re-fetch ``/fleet/config`` and replace the cached identity hints
        (and, optionally, tokens).

        Returns ``(account_count, token_count)``. Both fetches go to the
        same endpoint — when ``fetch_tokens`` is False (the common path),
        we only deserialize the identity view we actually need.
        """
        accounts = fetch_identity_hints(api_url)
        self._accounts = accounts
        self._identities_fetched_at = time.time()
        if fetch_tokens:
            self._tokens = fetch_credential_tokens(api_url)
            self._tokens_fetched_at = time.time()
        else:
            self._tokens = {}
            self._tokens_fetched_at = 0.0
        return (
            sum(len(v) for v in accounts.values()),
            len(self._tokens),
        )

    def replace(
        self,
        accounts: dict[str, list[str]],
        *,
        tokens: dict[tuple[str, str], str | None] | None = None,
    ) -> None:
        """Bulk-set the cache from an externally-fetched mapping.

        Used by ``run_collection`` when it fetches the config directly.
        Pass ``tokens=None`` to keep the existing token cache; pass
        ``tokens={}`` to clear it.
        """
        self._accounts = {k: list(v) for k, v in accounts.items()}
        self._identities_fetched_at = time.time()
        if tokens is not None:
            self._tokens = dict(tokens)
            self._tokens_fetched_at = time.time()

    def provider_accounts(self) -> dict[str, list[str]]:
        """Return ``{provider_id: [account_id, ...]}`` for every account
        the server has registered — independent of whether a credential
        token was issued.

        **Order is arbitrary** (it mirrors the server's JSON
        serialization of ``provider_configs`` rows). Callers must not
        rely on position when iterating; use a value lookup instead.
        """
        return {pid: list(aids) for pid, aids in self._accounts.items()}
