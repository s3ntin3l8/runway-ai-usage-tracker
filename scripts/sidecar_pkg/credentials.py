"""Sidecar credential pipeline (issue #272).

Reads the server's ``/fleet/config`` to discover what
``(provider_id, account_id)`` pairs have stored credentials, fetches the
``credential_token`` the server attaches to each row, and redeems those
tokens via ``POST /api/v1/fleet/credentials/redeem`` to obtain the
decrypted credentials the sidecar needs to scope its events to the
right account.

The flow is:

  1. ``fetch_credential_tokens(api_url)`` → ``dict[(provider_id, account_id), token]``
     from the public ``/fleet/config`` endpoint. No HMAC — the tokens
     themselves are useless without the sidecar's shared
     ``INGEST_API_KEY``.
  2. ``redeem_credential(api_url, api_key, token)`` → decrypted credential
     dict (``api_key`` / ``session_cookie`` / ``oai_sc_cookie``) via the
     HMAC-authenticated ``/fleet/credentials/redeem`` endpoint.

A ``CredentialCache`` keeps a per-process snapshot of the fetched tokens
and redeemed credentials with a TTL so a sidecar restart doesn't
re-fetch on every collection cycle. The TTL is shorter than the server's
``CREDENTIAL_TOKEN_TTL_SECONDS`` so we always re-fetch before our cached
tokens would expire.

This module is the sidecar half of the pair that mirrors
``app/services/credential_token.py`` on the server. The sidecar does
*not* need to verify the token signature — it only redeems opaque
references via the HMAC-authenticated redeem endpoint — so this module
only handles the HTTP plumbing.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from scripts.sidecar_pkg.tls import build_context

logger = logging.getLogger(__name__)


def fetch_credential_tokens(
    api_url: str,
    *,
    timeout: int = 10,
) -> dict[tuple[str, str], str]:
    """Fetch per-account credential tokens from ``GET /api/v1/fleet/config``.

    Returns a ``{(provider_id, account_id): token}`` map. Providers with no
    configured credentials are simply absent. The endpoint is
    intentionally unauthenticated; tokens it returns are useless without
    the sidecar's shared ingest secret.

    On HTTP error / non-200 response / malformed JSON / non-dict ``config``,
    returns an empty dict — sidecar collection must keep working when the
    server is unreachable (e.g. before the first heartbeat). The caller
    decides whether to fall back to legacy ``account_id="default"``
    stamping.
    """
    from urllib import error, request

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


def redeem_credential(
    api_url: str,
    api_key: str,
    token: str,
    *,
    timeout: int = 10,
) -> dict[str, Any] | None:
    """Redeem a single credential token and return its decrypted credentials.

    Returns ``None`` on any failure (HTTP error, expired token, missing
    row) — callers fall back to local credential discovery in that case.
    """
    if not api_key or not token:
        return None
    from urllib import error, request

    url = f"{api_url.rstrip('/')}/api/v1/fleet/credentials/redeem"
    body = json.dumps({"token": token}, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    import hashlib
    import hmac

    signature = hmac.new(
        api_key.encode("utf-8"), timestamp.encode() + body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": timestamp,
    }

    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout, context=build_context(url)) as resp:
            if resp.getcode() != 200:
                logger.debug("redeem_credential: %s returned %s", url, resp.getcode())
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("redeem_credential: %s failed: %s", url, exc)
        return None

    if not isinstance(payload, dict):
        return None
    credentials = payload.get("credentials")
    return credentials if isinstance(credentials, dict) else None


class CredentialCache:
    """In-memory cache of server-issued credential tokens + redeemed credentials.

    The sidecar refreshes tokens on a heartbeat cadence. Between refreshes,
    cached values are reused so the collection cycle doesn't round-trip the
    redeem endpoint per provider per account.

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
        self._credentials: dict[tuple[str, str], dict[str, Any]] = {}

    @property
    def tokens(self) -> dict[tuple[str, str], str]:
        return dict(self._tokens)

    @property
    def credentials(self) -> dict[tuple[str, str], dict[str, Any]]:
        return dict(self._credentials)

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
        cache. Cached credentials for pairs that no longer appear in the
        server config are dropped — they likely were deleted/disabled.
        """
        self._tokens = fetch_credential_tokens(api_url)
        self._tokens_fetched_at = time.time()
        # Drop credentials whose pair no longer exists in the server's view.
        self._credentials = {k: v for k, v in self._credentials.items() if k in self._tokens}
        return len(self._tokens)

    def replace_tokens(self, tokens: dict[tuple[str, str], str]) -> int:
        """Bulk-set the token cache from an externally-fetched mapping.

        Used by ``run_collection`` when it fetches tokens directly (rather
        than going through ``refresh_tokens``). Same staleness semantics
        as ``refresh_tokens``.
        """
        self._tokens = dict(tokens)
        self._tokens_fetched_at = time.time()
        self._credentials = {k: v for k, v in self._credentials.items() if k in self._tokens}
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

    def redeem(self, api_url: str, api_key: str, pair: tuple[str, str]) -> dict[str, Any] | None:
        """Redeem the cached token for ``pair`` and cache the credentials.

        Returns the credentials (cached) or ``None`` if no token is cached
        for that pair or the redeem failed. The redeem is skipped when
        fresh credentials for the pair are already cached.
        """
        if pair in self._credentials:
            return self._credentials[pair]
        token = self._tokens.get(pair)
        if not token:
            return None
        creds = redeem_credential(api_url, api_key, token)
        if creds is None:
            return None
        self._credentials[pair] = creds
        return creds

    def forget(self, pair: tuple[str, str]) -> None:
        """Drop a cached credential pair (used when the server disables
        the row or otherwise invalidates the token)."""
        self._credentials.pop(pair, None)
        self._tokens.pop(pair, None)
