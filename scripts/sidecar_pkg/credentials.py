"""Sidecar identity cache (issue #272).

Reads the server's ``/fleet/config`` and exposes two decoupled views:

- ``provider_accounts()`` returns ``{provider_id: [account_id, ...]}`` for
  every **enabled** account the server has registered, **independent of
  whether a credential_token is attached**. Identity hints live in the
  public ``accounts[*].account_id`` field; tokens are conditional on
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
from typing import Any

logger = logging.getLogger(__name__)


def _fetch_config_payload(
    api_url: str,
    *,
    timeout: int = 10,
) -> dict[str, Any] | None:
    """Single ``GET /api/v1/fleet/config`` round-trip.

    Returns the parsed JSON payload (``{"config": {...}}``) on success,
    or ``None`` on any failure — HTTP error, non-200 status, malformed
    JSON, missing ``config`` key.

    The two public fetchers share this so they don't double-round-trip
    when both are needed by ``refresh_from_config(fetch_tokens=True)``
    (PR #283 round-3 review).
    """
    from urllib import error, request

    from scripts.sidecar_pkg.tls import build_context

    url = f"{api_url.rstrip('/')}/api/v1/fleet/config"
    req = request.Request(url)
    try:
        with request.urlopen(req, timeout=timeout, context=build_context(url)) as resp:
            if resp.getcode() != 200:
                logger.debug("fetch_config: %s returned %s", url, resp.getcode())
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("fetch_config: %s failed: %s", url, exc)
        return None


def _parse_identity_hints(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Extract ``{provider_id: [account_id, ...]}`` from a /fleet/config payload.

    Only **enabled** rows contribute (matches /fleet/config's behavior of
    omitting tokens for disabled rows; PR #283 round-3 review).
    """
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
            if not acct.get("enabled", False):
                continue
            aid = acct.get("account_id")
            if isinstance(aid, str) and aid:
                out.setdefault(provider_id, []).append(aid)
    return out


def _parse_credential_tokens(payload: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Extract ``{(provider_id, account_id): token}`` from a /fleet/config payload.

    Disabled rows produce no token regardless of credential content.
    """
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
            if not acct.get("enabled", False):
                continue
            aid = acct.get("account_id")
            token = acct.get("credential_token")
            if isinstance(aid, str) and isinstance(token, str) and aid and token:
                out[(provider_id, aid)] = token
    return out


def _parse_account_tag_hints(payload: dict[str, Any]) -> dict[str, dict[str, str]] | None:
    """Extract ``{provider_id: {credential_origin: account_id, ...}}`` from a
    ``/fleet/config`` response.

    Used by the sidecar's silent-listener block guard (PR #288): when a
    token card can't be stamped via local discovery, the sidecar
    consults this map for any operator-set tag matching the credential's
    ``origin_descriptor``. Returns ``None`` when the payload omits the
    field — older server versions (pre-PR #288) don't carry it and
    ``None`` lets the caller distinguish "field missing" from
    "field present and empty", which is what the outage-tolerant
    refresh logic in ``run_collection`` keys off.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("account_tag_hints")
    if not isinstance(raw, dict):
        return None
    out: dict[str, dict[str, str]] = {}
    for provider_id, by_origin in raw.items():
        if not isinstance(by_origin, dict) or not isinstance(provider_id, str):
            continue
        cleaned = {
            str(origin): str(account_id)
            for origin, account_id in by_origin.items()
            if isinstance(origin, str) and origin and isinstance(account_id, str) and account_id
        }
        if cleaned:
            out[provider_id] = cleaned
    return out


def fetch_account_tag_hints(
    api_url: str,
    *,
    timeout: int = 10,
) -> dict[str, dict[str, str]] | None:
    """Read the operator-resolved tag-hint map from ``GET /api/v1/fleet/config``.

    Reuses ``_fetch_config_payload`` so this never does a second round-trip
    per cycle (the sidecar's ``run_collection`` typically calls
    :func:`fetch_identity_hints` first anyway; pair them with the same
    response).

    Returns the parsed hint map, or ``None`` when the fetch fails
    (network error, non-200, malformed JSON) **or** the payload omits
    the ``account_tag_hints`` field. The caller is expected to skip the
    update on ``None`` so the prior snapshot survives and ``is_fresh()``
    returns False on the next cycle (mirror of the outage-tolerance in
    PR #283 round-3 review).
    """
    payload = _fetch_config_payload(api_url, timeout=timeout)
    if payload is None:
        return None
    return _parse_account_tag_hints(payload)


def fetch_identity_hints(
    api_url: str,
    *,
    timeout: int = 10,
) -> dict[str, list[str]] | None:
    """Fetch per-account identity hints from ``GET /api/v1/fleet/config``.

    Returns ``{provider_id: [account_id, ...]}`` for every **enabled**
    account row in the server's provider_configs. **Decoupled from
    token issuance** — rows without credentials and configurations with
    empty ``INGEST_API_KEY`` still contribute their ``account_id`` here,
    so the per-account event iteration that fixes #272 is not silently
    disabled by either condition (PR #283 review).

    Returns ``None`` when the fetch fails (network error, non-200,
    malformed JSON). Callers must distinguish this from an empty dict —
    an empty dict is a successful response with no enabled rows; ``None``
    is an outage where the prior cache should be retained and the next
    cycle should retry (PR #283 round-3 review).
    """
    payload = _fetch_config_payload(api_url, timeout=timeout)
    if payload is None:
        return None
    return _parse_identity_hints(payload)


def fetch_credential_tokens(
    api_url: str,
    *,
    timeout: int = 10,
) -> dict[tuple[str, str], str] | None:
    """Fetch per-account credential tokens from ``GET /api/v1/fleet/config``.

    Returns ``None`` on a fetch failure (caller should keep the prior
    cache, retry on the next cycle). Returns an empty dict on success
    with no tokens issued.
    """
    payload = _fetch_config_payload(api_url, timeout=timeout)
    if payload is None:
        return None
    return _parse_credential_tokens(payload)


class CredentialCache:
    """In-memory cache of per-account identity hints and credential tokens.

    The sidecar refreshes the upstream view on a heartbeat cadence.
    Between refreshes, cached values are reused so the collection cycle
    doesn't round-trip ``/fleet/config`` per cycle.

    The cache is *strictly* per-process and never persisted. A sidecar
    restart means a fresh fetch.

    On a fetched outage, ``replace()`` and ``refresh_from_config()`` skip
    the cache write entirely — the prior snapshot is retained and the
    next cycle retries. This is the round-3 review fix.
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
        self._tokens: dict[tuple[str, str], str] = {}

    @property
    def tokens(self) -> dict[tuple[str, str], str]:
        return dict(self._tokens)

    def is_fresh(self, *, now: float | None = None) -> bool:
        """Return True when the cache was refreshed within ``ttl_seconds``.

        A never-populated cache is never fresh. Distinguish via the
        ``_identities_fetched_at == 0.0`` sentinel that ``__init__``
        writes. Critically, a fetch that returned ``None`` does NOT set
        this sentinel — the next cycle keeps trying.
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
    ) -> tuple[int, int] | None:
        """Re-fetch ``/fleet/config`` and replace the cached snapshot.

        Single round-trip — when ``fetch_tokens`` is True, both views
        deserialize from the same payload (PR #283 round-3 review).

        Returns ``(account_count, token_count)`` on a successful fetch,
        or ``None`` on a fetch failure. ``None`` is the canonical signal
        to the caller that the prior snapshot is still in the cache and
        should be reused as-is.
        """
        payload = _fetch_config_payload(api_url)
        if payload is None:
            # Outage — leave the cache untouched. ``is_fresh`` stays at
            # its prior value (likely False), so the next cycle retries.
            return None

        accounts = _parse_identity_hints(payload)
        tokens = _parse_credential_tokens(payload) if fetch_tokens else {}

        self._accounts = accounts
        self._identities_fetched_at = time.time()
        if fetch_tokens:
            self._tokens = tokens
            self._tokens_fetched_at = time.time()
        # Report cache state, not just this call's fetch. With
        # ``fetch_tokens=False`` no token fetch happens here, but the
        # cache may already hold tokens from an earlier call; the
        # returned tuple should describe what callers will see when they
        # read ``self.tokens`` (PR #283 round-4 review).
        return (
            sum(len(v) for v in accounts.values()),
            len(self._tokens),
        )

    def replace(
        self,
        *,
        accounts: dict[str, list[str]] | None = None,
        tokens: dict[tuple[str, str], str] | None = None,
    ) -> None:
        """Bulk-set the cache from an externally-fetched mapping.

        Pass ``accounts=None`` to keep the existing identity view; pass
        ``accounts={}`` to clear it. ``tokens`` follows the same pattern
        for the token view.

        Used by ``run_collection`` when it fetches the config directly.
        On a fetch failure (returned ``None`` from the fetcher), the
        caller should skip this call entirely so the prior snapshot is
        retained.
        """
        if accounts is not None:
            self._accounts = {k: list(v) for k, v in accounts.items()}
            self._identities_fetched_at = time.time()
        if tokens is not None:
            self._tokens = dict(tokens)
            self._tokens_fetched_at = time.time()

    def provider_accounts(self) -> dict[str, list[str]]:
        """Return ``{provider_id: [account_id, ...]}`` for every enabled
        account the server has registered — independent of whether a
        credential token was issued.

        **Order is arbitrary** (it mirrors the server's JSON
        serialization of ``provider_configs`` rows). Callers must not
        rely on position when iterating; use a value lookup instead.
        """
        return {pid: list(aids) for pid, aids in self._accounts.items()}
