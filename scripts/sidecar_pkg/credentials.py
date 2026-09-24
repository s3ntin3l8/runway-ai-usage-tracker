"""Sidecar identity cache (issue #272).

Reads the server's ``/fleet/config`` and exposes three views:

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
- ``provider_tag_hints()`` returns ``{provider_id: {credential_origin:
  account_id}}`` for every operator-resolved silent-listener tag the
  server has shipped via ``account_tag_hints`` (PR #288 / #290). When
  local credential discovery can't stamp a token card with an
  ``account_id``, ``GenericCollector.collect_provider`` consults this
  map as the fallback before blocking the card.

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
    sidecar_id: str | None = None,
) -> dict[str, Any] | None:
    """Single ``GET /api/v1/fleet/config`` round-trip.

    Returns the parsed JSON payload (``{"config": {...}}``) on success,
    or ``None`` on any failure — HTTP error, non-200 status, malformed
    JSON, missing ``config`` key.

    The two public fetchers share this so they don't double-round-trip
    when both are needed by ``refresh_from_config(fetch_tokens=True)``
    (PR #283 round-3 review).

    ``sidecar_id`` (#319) is appended as ``?sidecar_id=`` when given, so
    the server can scope ``account_tag_hints`` to this machine's
    credential tags. Omitting it (old call sites) keeps the
    deployment-wide view for single-host deployments.
    """
    from urllib import error, request

    from scripts.sidecar_pkg.tls import build_context

    url = f"{api_url.rstrip('/')}/api/v1/fleet/config"
    if sidecar_id:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sidecar_id={sidecar_id}"
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


def fetch_identity_hints(
    api_url: str,
    *,
    timeout: int = 10,
    sidecar_id: str | None = None,
) -> tuple[dict[str, list[str]], dict[str, dict[str, str]] | None] | None:
    """Fetch per-account identity hints + operator tag-hint map from ``GET /api/v1/fleet/config``.

    Single round-trip — both views deserialize from the same payload
    (PR #288 silent-listener model; PR #290 round-2 review). The
    account-identity view is decoupled from token issuance — rows
    without credentials and configurations with empty ``INGEST_API_KEY``
    still contribute their ``account_id`` here, so the per-account
    event iteration that fixes #272 is not silently disabled by either
    condition (PR #283 review).

    Returns ``(accounts, tag_hints)`` on success. ``tag_hints`` is
    ``None`` when the payload omits the ``account_tag_hints`` field
    (older server versions pre-PR #288) — that sentinel preserves the
    prior tag-hint snapshot via ``cache.replace(tag_hints=None)``'s
    "None means keep prior" contract, so a server downgrade doesn't
    silently clear a previously-cached hint map.

    Returns ``None`` when the fetch fails (network error, non-200,
    malformed JSON). Callers must distinguish this from a successful
    empty payload — an empty ``accounts`` dict is a successful response
    with no enabled rows; ``None`` is an outage where the prior cache
    should be retained and the next cycle should retry (PR #283
    round-3 review).

    ``sidecar_id`` (#319) identifies this machine so the server scopes
    ``account_tag_hints`` to machine-local credential tags. Keyword-only
    for backward compatibility with positional callers.
    """
    payload = _fetch_config_payload(api_url, timeout=timeout, sidecar_id=sidecar_id)
    if payload is None:
        return None
    accounts = _parse_identity_hints(payload)
    tag_hints = _parse_account_tag_hints(payload)
    return accounts, tag_hints


def fetch_credential_tokens(
    api_url: str,
    *,
    timeout: int = 10,
    sidecar_id: str | None = None,
) -> dict[tuple[str, str], str] | None:
    """Fetch per-account credential tokens from ``GET /api/v1/fleet/config``.

    Returns ``None`` on a fetch failure (caller should keep the prior
    cache, retry on the next cycle). Returns an empty dict on success
    with no tokens issued.

    ``sidecar_id`` (#319) is forwarded for symmetry with
    :func:`fetch_identity_hints` — tokens are not machine-scoped yet,
    but sending it keeps both fetchers on the same request shape.
    """
    payload = _fetch_config_payload(api_url, timeout=timeout, sidecar_id=sidecar_id)
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
        self._tag_hints: dict[str, dict[str, str]] = {}

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
        sidecar_id: str | None = None,
    ) -> tuple[int, int] | None:
        """Re-fetch ``/fleet/config`` and replace the cached snapshot.

        Single round-trip — when ``fetch_tokens`` is True, both views
        deserialize from the same payload (PR #283 round-3 review).
        Tag hints are always parsed from the same payload (they're a
        top-level field, not gated on token issuance), so the cache
        picks them up for free on every refresh.

        Returns ``(account_count, token_count)`` on a successful fetch,
        or ``None`` on a fetch failure. ``None`` is the canonical signal
        to the caller that the prior snapshot is still in the cache and
        should be reused as-is.

        ``sidecar_id`` (#319) forwards this machine's identity for
        machine-scoped account_tag_hints.
        """
        payload = _fetch_config_payload(api_url, sidecar_id=sidecar_id)
        if payload is None:
            # Outage — leave the cache untouched. ``is_fresh`` stays at
            # its prior value (likely False), so the next cycle retries.
            return None

        accounts = _parse_identity_hints(payload)
        tokens = _parse_credential_tokens(payload) if fetch_tokens else {}
        tag_hints = _parse_account_tag_hints(payload)

        self._accounts = accounts
        self._identities_fetched_at = time.time()
        if fetch_tokens:
            self._tokens = tokens
            self._tokens_fetched_at = time.time()
        if tag_hints is not None:
            # Field present in the payload — overwrite. The fetch itself
            # never returns ``None`` for a successful round-trip, so this
            # only ever fires when the server actually emits the field.
            self._tag_hints = tag_hints
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
        tag_hints: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """Bulk-set the cache from an externally-fetched mapping.

        Pass ``accounts=None`` to keep the existing identity view; pass
        ``accounts={}`` to clear it. ``tokens`` and ``tag_hints`` follow
        the same pattern. ``tag_hints=None`` preserves the prior hint
        map — used when the server omits the field (older server
        versions) so a downgrade doesn't silently wipe cached hints.

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
        if tag_hints is not None:
            self._tag_hints = {k: dict(v) for k, v in tag_hints.items()}

    def provider_accounts(self) -> dict[str, list[str]]:
        """Return ``{provider_id: [account_id, ...]}`` for every enabled
        account the server has registered — independent of whether a
        credential token was issued.

        **Order is arbitrary** (it mirrors the server's JSON
        serialization of ``provider_configs`` rows). Callers must not
        rely on position when iterating; use a value lookup instead.
        """
        return {pid: list(aids) for pid, aids in self._accounts.items()}

    def provider_tag_hints(self) -> dict[str, dict[str, str]]:
        """Return ``{provider_id: {credential_origin: account_id}}`` for
        every operator-resolved tag the server has shipped via
        ``/fleet/config``'s ``account_tag_hints`` (PR #288 silent
        listener; PR #290 round-2 review).

        Returns ``{}`` when no hints are cached (server never emitted
        any, or this is the first cycle). Callers — currently
        ``GenericCollector.collect_provider``'s block guard — look up
        ``provider_tag_hints()[provider_id][origin_descriptor]`` and
        fall back to that ``account_id`` when local discovery is empty.
        """
        return {pid: dict(by_origin) for pid, by_origin in self._tag_hints.items()}
