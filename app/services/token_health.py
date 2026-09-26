"""Token health inspection — expiry parsing, status classification."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session
from sqlmodel import select as sqlselect

from app.core.config import settings
from app.core.db import engine
from app.core.registry import registry
from app.core.utils import IdentityExtractor, scrub_log
from app.models.db import ProviderConfig, SidecarRegistry
from app.services import auth_failures
from app.services.account_identity import canonical_account_id
from app.services.credential_provider import CredentialProvider
from app.services.token_cache import is_foreign_account_entry, token_cache
from app.services.token_refresher import _REFRESH_ENDPOINTS

logger = logging.getLogger(__name__)

EXPIRY_WARNING_SECS = 86400  # 24 hours — for tokens that require manual re-auth


def _classify_status(
    exp: float | None,
    is_opaque: bool = False,
    can_refresh: bool = False,
) -> str:
    if exp is None:
        # If we have no JWT expiry, but we have a token value (is_opaque),
        # it's considered "valid" (READY).
        return "valid" if is_opaque else "unknown"
    now = time.time()
    if exp < now:
        return "expired"
    seconds_left = exp - now

    # Tokens with a refresh_token are auto-rolled by TokenAutoRefresher every
    # TOKEN_AUTO_REFRESH_INTERVAL_SECONDS. Short-lived access tokens (Gemini's
    # 60-min JWT) would otherwise sit permanently in the 24h "expiring" bucket.
    # Only warn when the next auto-refresh tick is too late to save the token.
    if can_refresh and settings.TOKEN_AUTO_REFRESH_ENABLED:
        if seconds_left < settings.TOKEN_AUTO_REFRESH_INTERVAL_SECONDS:
            return "expiring"
        return "valid"

    if seconds_left < EXPIRY_WARNING_SECS:
        return "expiring"
    return "valid"


# Origins the dashboard/server manages itself. Their rows are re-seeded every
# collection cycle, so evicting them from the cache would not stick — they are
# changed in Settings → Providers or the server environment instead.
_NON_REMOVABLE_SOURCES = frozenset({"config", "manual_config", "server"})


class CredentialNotRemovableError(Exception):
    """The credential is managed outside the cache (Settings → Providers / env)."""


def _underlying_account(account_id: str) -> str:
    """Map a synthetic Token Health row id back to the account it stands for."""
    if account_id == "server":
        return "default"
    for prefix in ("config-cookie:", "config:"):
        if account_id.startswith(prefix):
            return account_id[len(prefix) :]
    return account_id


def _is_flagged_invalid(provider: str, account_id: str) -> bool:
    """True when a collector recently got 401/403 for an account this row could serve.

    Collectors flag under their own id (``default``, an email, …) while cache
    rows may be keyed by an opaque hash, so match with the same identity
    semantics fallbacks use: a flagged id matches unless one side is a
    *different identified* account.
    """
    row = _underlying_account(account_id)
    return any(
        not is_foreign_account_entry(flagged, row) or not is_foreign_account_entry(row, flagged)
        for flagged in auth_failures.flagged_accounts(provider)
    )


def _collect_server_credentials() -> dict[str, dict[str, Any]]:
    """Credentials the *server itself* discovered (env vars / local files) per provider.

    Blocking (file + DB reads); call through ``asyncio.to_thread``.
    """
    found: dict[str, dict[str, Any]] = {}
    for provider_id in registry.get_all_providers():
        try:
            creds = CredentialProvider.get_credentials(provider_id)
        except Exception as e:
            logger.debug(f"Server credential scan failed for {scrub_log(provider_id)}: {e}")
            continue
        server_tokens = {k: v for k, v in creds.items() if v and creds.sources.get(k) == "server"}
        if server_tokens:
            found[provider_id] = server_tokens
    return found


def _row(
    provider: str,
    account_id: str,
    *,
    label: str | None,
    source: str | None,
    source_name: str | None,
    token_types: list[str],
    exp: float | None,
    can_refresh: bool,
    ttl_remaining: int = 0,
    rollable: bool = False,
) -> dict[str, Any]:
    """Assemble one health record (internal ``_``-prefixed keys are stripped later)."""
    status = _classify_status(
        exp, is_opaque=(exp is None) and bool(token_types), can_refresh=rollable
    )
    if status in ("valid", "unknown") and _is_flagged_invalid(provider, account_id):
        status = "invalid"
    return {
        "provider": provider,
        "account_id": account_id,
        "account_label": label,
        "source": source,
        "source_name": source_name,
        "token_types": token_types,
        "status": status,
        "expires_at": (
            datetime.fromtimestamp(exp, tz=UTC).isoformat() if exp is not None else None
        ),
        "ttl_remaining_seconds": ttl_remaining,
        "can_refresh": can_refresh,
        "removable": source not in _NON_REMOVABLE_SOURCES,
        # Synthetic rows (config / env) with no expiry are only *assumed* valid.
        "_assumed": source in _NON_REMOVABLE_SOURCES and exp is None,
        "_rollable": rollable,
    }


class TokenHealthService:
    async def get_health(self) -> list[dict[str, Any]]:
        """Return a health record for each known credential."""
        stats = await token_cache.get_all_stats()
        result: list[dict[str, Any]] = []
        seen_token_values: set[str] = set()

        sidecar_names = {}
        try:
            with Session(engine) as _s:
                for sc in _s.exec(sqlselect(SidecarRegistry)).all():
                    sidecar_names[sc.sidecar_id] = sc.custom_name or sc.hostname or sc.sidecar_id
        except Exception as e:
            logger.warning(f"Could not load sidecar names for token health: {e}")

        for provider, accounts in stats.items():
            for acc_id, info in accounts.items():
                tokens = await token_cache.get(provider, acc_id) or {}
                logger.debug(f"Token health check for {provider}/{acc_id}: {list(tokens.keys())}")

                # If we have any tokens, track their values to deduplicate later
                for val in tokens.values():
                    if val:
                        seen_token_values.add(f"{provider}:{val}")

                source_val = info.get("source")
                has_refresh_token = "refresh_token" in tokens
                result.append(
                    _row(
                        provider,
                        acc_id,
                        label=info.get("account_label"),
                        source=source_val,
                        source_name=(
                            sidecar_names.get(source_val, source_val) if source_val else None
                        ),
                        token_types=list(tokens.keys()),
                        exp=IdentityExtractor.exp_from_tokens(tokens),
                        # The manual Refresh button only works where an endpoint exists;
                        # classification still treats any refresh_token as rollable
                        # (a local agent re-pushes e.g. antigravity's short-lived token).
                        can_refresh=has_refresh_token and provider in _REFRESH_ENDPOINTS,
                        ttl_remaining=info.get("ttl_remaining", 0),
                        rollable=has_refresh_token,
                    )
                )

        # Also surface API keys / session cookies configured in Settings → Providers.
        # These are stored encrypted in ProviderConfig but never flow through token_cache
        # under their own row, so they would otherwise be invisible to the panel.
        try:
            with Session(engine) as _s:
                configs = _s.exec(
                    sqlselect(ProviderConfig).where(ProviderConfig.enabled == True)  # noqa: E712
                ).all()

            for cfg in configs:
                account = cfg.account_id or "default"
                for value, prefix, token_type in (
                    (cfg.api_key, "config", "api_key"),
                    (cfg.session_cookie, "config-cookie", "session_cookie"),
                ):
                    # Skip if this exact value is already in the live session cache
                    if not value or f"{cfg.provider_id}:{value}" in seen_token_values:
                        continue
                    result.append(
                        _row(
                            cfg.provider_id,
                            f"{prefix}:{account}",
                            label=cfg.account_label,
                            source="config",
                            source_name="config",
                            token_types=[token_type],
                            exp=None,
                            can_refresh=False,
                        )
                    )
        except Exception as e:
            logger.warning(f"Could not load ProviderConfig credentials for token health: {e}")

        # Credentials the server discovered itself (env vars, local files).
        # They never enter token_cache, so this is the only place they show up.
        try:
            server_creds = await asyncio.to_thread(_collect_server_credentials)
            for provider, creds in server_creds.items():
                fresh = {
                    k: v for k, v in creds.items() if f"{provider}:{v}" not in seen_token_values
                }
                if not fresh:
                    continue
                result.append(
                    _row(
                        provider,
                        "server",
                        label=None,
                        source="server",
                        source_name="server",
                        token_types=list(fresh.keys()),
                        exp=IdentityExtractor.exp_from_tokens(fresh),
                        can_refresh=False,
                        rollable="refresh_token" in fresh,
                    )
                )
        except Exception as e:
            logger.debug(f"Server credential scan for token health failed: {e}")

        # Mark expired, unrefreshable entries as "redundant" when another credential
        # this account could fall back on is still healthy. Such an entry can't be
        # auto-rolled and isn't blocking collection — so the dashboard banner should
        # not raise a hard alarm on it alone. Siblings must be for the *same* account
        # (or carry no identity of their own — the borrowing rule collectors use), and
        # rows that are merely assumed valid (config / env, no expiry) don't count.
        healthy = [r for r in result if r["status"] in ("valid", "expiring") and not r["_assumed"]]
        for r in result:
            wanted = _underlying_account(r["account_id"])
            r["redundant"] = (
                r["status"] == "expired"
                and not r["_rollable"]
                and any(
                    h is not r
                    and h["provider"] == r["provider"]
                    and not is_foreign_account_entry(_underlying_account(h["account_id"]), wanted)
                    for h in healthy
                )
            )
        for r in result:
            r.pop("_assumed", None)
            r.pop("_rollable", None)

        return result

    async def delete_credential(self, provider: str, account_id: str) -> bool:
        """
        Manually remove a credential from the in-memory cache.

        Credentials managed outside the cache — dashboard-saved keys/cookies and
        server env/file discoveries — are re-seeded on the next collection, so
        removing them here would only *look* like it worked. Those raise
        :class:`CredentialNotRemovableError`; change them in Settings → Providers or
        the server environment.

        Returns true if removed.
        """
        if account_id == "server" or account_id.startswith(("config:", "config-cookie:")):
            raise CredentialNotRemovableError(account_id)

        for entry in await token_cache.get_accounts(provider):
            if (
                entry["account_id"] == canonical_account_id(account_id)
                and entry.get("source") in _NON_REMOVABLE_SOURCES
            ):
                raise CredentialNotRemovableError(account_id)

        removed = await token_cache.remove(provider, account_id)
        if removed:
            auth_failures.clear(provider, account_id)
        return removed


token_health_service = TokenHealthService()
