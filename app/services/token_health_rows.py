"""Per-source Token Health row assembly.

Functions here receive data ``TokenHealthService.get_health`` has already
loaded. They do not open a ``Session`` and do not read ``token_cache``.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.core.utils import IdentityExtractor, has_refresh_credential
from app.models.db import CredentialSource, ProviderConfig
from app.services.account_identity import canonical_account_id
from app.services.token_cache import OAUTH_CREDENTIAL_KEYS
from app.services.token_health_status import (
    apply_invalid,
    classify_status,
    is_durably_rejected,
    is_failing,
    is_stale_source,
    redundancy_sibling,
)
from app.services.token_refresher import (
    REFRESH_ENDPOINTS,
    ROTATING_REFRESH_PROVIDERS,
    machine_owns_credential,
)

logger = logging.getLogger(__name__)

# Origins the dashboard/server manages itself. Their rows are re-seeded every
# collection cycle, so evicting them from the cache would not stick — they are
# changed in Settings → Providers or the server environment instead.
_NON_REMOVABLE_SOURCES = frozenset({"config", "manual_config", "server"})
# Credential fields that share one expiry (the OAuth token family).
_OAUTH_FAMILY_KEYS = OAUTH_CREDENTIAL_KEYS | {"access_token"}


def _row(  # noqa: PLR0913 - one flat record builder; every field is a distinct row column
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
    machine_renewed: bool = False,
) -> dict[str, Any]:
    """Assemble one health record (internal ``_``-prefixed keys are stripped later)."""
    status = classify_status(
        exp, is_opaque=(exp is None) and bool(token_types), can_refresh=rollable
    )
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
        # A rotating provider's login that a machine's CLI renews (the server must not).
        "machine_renewed": machine_renewed,
        "removable": source not in _NON_REMOVABLE_SOURCES,
        # Synthetic rows (config / env) with no expiry are only *assumed* valid.
        "_assumed": source in _NON_REMOVABLE_SOURCES and exp is None,
        "_rollable": rollable,
    }


def pending_claude_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude OAuth bundles still waiting for an account assignment."""
    return [
        source
        for source in sources
        if source["tokens"].get("oauth_token")
        and source["metadata"].get("sidecar_id")
        and source["metadata"].get("identity_pending") is True
    ]


def _append_cache_rows(
    result: list[dict[str, Any]],
    seen_token_values: set[str],
    stats: dict[str, Any],
    cache_tokens: dict[tuple[str, str], dict[str, Any]],
    retired_accounts: set[tuple[str, str]],
    candidates_by_account: dict[tuple[str, str], list[dict[str, Any]]],
    durable_sources: list[CredentialSource],
    sidecar_names: dict[str, str],
    claude_sources: list[dict[str, Any]],
) -> None:
    source_oauth_values = {source["tokens"]["oauth_token"] for source in claude_sources}

    for provider, accounts in stats.items():
        for acc_id, info in accounts.items():
            if (provider, canonical_account_id(acc_id)) in retired_accounts:
                # A retired account holds no live credential — but the cache
                # can still hold one for a cycle (a push that raced the
                # archive). Token Health only lists what is live.
                continue
            tokens = cache_tokens[(provider, acc_id)]
            if provider == "anthropic" and tokens.get("oauth_token") in source_oauth_values:
                tokens = {
                    key: value
                    for key, value in tokens.items()
                    if key not in _OAUTH_FAMILY_KEYS | {"client_id", "expiry_date"}
                }
                if not tokens:
                    continue
            logger.debug(f"Token health check for {provider}/{acc_id}: {list(tokens.keys())}")

            # If we have any tokens, track their values to deduplicate later
            for val in tokens.values():
                if val:
                    seen_token_values.add(f"{provider}:{val}")

            source_val = info.get("source")
            has_refresh_token = has_refresh_credential(tokens)
            source_candidates = candidates_by_account.get((provider, acc_id), [])
            machine_renewed = machine_owns_credential(
                provider, tokens, source_candidates, merged_source=source_val
            )
            durable_source_ids = {
                item.source_id
                for item in durable_sources
                if item.provider_id == provider and item.account_id == acc_id
            }
            if any(
                candidate.get("source_id") in durable_source_ids for candidate in source_candidates
            ):
                # The source-specific durable row below carries this exact
                # live credential, so suppress the compatibility aggregate.
                continue
            result.append(
                _row(
                    provider,
                    acc_id,
                    label=info.get("account_label"),
                    source=source_val,
                    source_name=(sidecar_names.get(source_val, source_val) if source_val else None),
                    token_types=list(tokens.keys()),
                    exp=IdentityExtractor.exp_from_tokens(tokens),
                    # The manual Refresh button only works where an endpoint exists;
                    # classification still treats any refresh_token as rollable
                    # (a local agent re-pushes e.g. antigravity's short-lived token).
                    can_refresh=has_refresh_token
                    and provider in REFRESH_ENDPOINTS
                    and not machine_renewed,
                    ttl_remaining=info.get("ttl_remaining", 0),
                    rollable=has_refresh_token,
                    machine_renewed=machine_renewed and has_refresh_token,
                )
            )


def _append_claude_rows(
    result: list[dict[str, Any]],
    seen_token_values: set[str],
    claude_sources: list[dict[str, Any]],
    sidecar_names: dict[str, str],
) -> None:
    for source in claude_sources:
        tokens = source["tokens"]
        metadata = source["metadata"]
        pending = bool(metadata.get("identity_pending"))
        sidecar_id = str(metadata["sidecar_id"])
        account_id = source["account_id"]
        identity_hint = metadata.get("identity_hint")
        pending_label = (
            f"Unassigned ({identity_hint})"
            if isinstance(identity_hint, str) and "@" in identity_hint
            else "Unassigned"
        )
        for value in tokens.values():
            if value:
                seen_token_values.add(f"anthropic:{value}")
        row = _row(
            "anthropic",
            f"unassigned:{source['source_id']}" if pending else account_id,
            label=pending_label if pending else None,
            source=sidecar_id,
            source_name=sidecar_names.get(sidecar_id, sidecar_id),
            token_types=[key for key in tokens if key in _OAUTH_FAMILY_KEYS],
            exp=IdentityExtractor.exp_from_tokens(tokens),
            can_refresh=False,
            ttl_remaining=source["ttl_remaining"],
            rollable=has_refresh_credential(tokens),
            machine_renewed=has_refresh_credential(tokens),
        )
        row.update(
            source_id=source["source_id"],
            assignment_pending=pending,
            removable=False,
        )
        result.append(row)


def _append_durable_rows(
    result: list[dict[str, Any]],
    durable_sources: list[CredentialSource],
    retired_accounts: set[tuple[str, str]],
    candidates_by_account: dict[tuple[str, str], list[dict[str, Any]]],
    sidecar_names: dict[str, str],
) -> None:
    durable_by_account: dict[tuple[str, str], list[CredentialSource]] = {}
    for item in durable_sources:
        durable_by_account.setdefault((item.provider_id, item.account_id), []).append(item)

    for durable_source in durable_sources:
        provider_id = durable_source.provider_id
        account_id = durable_source.account_id
        if (provider_id, canonical_account_id(account_id)) in retired_accounts:
            # A retired account holds no live credential: the credential
            # inventory keeps the source visible under the archived
            # account, but Token Health only lists what is live.
            continue
        candidates = candidates_by_account.get((provider_id, account_id), [])
        live = next(
            (item for item in candidates if item.get("source_id") == durable_source.source_id),
            None,
        )
        # Aggregate compatibility rows intentionally have no source_id;
        # this exact-source check only coalesces rows created above.
        if live and any(
            row["provider"] == provider_id and row.get("source_id") == durable_source.source_id
            for row in result
        ):
            # A source-specific row (including pending Claude OAuth) already
            # represents this exact durable source.
            continue
        token_types: list[str] = []
        exp: float | None = None
        durable_rollable = False
        if live:
            live_tokens = live.get("tokens") or {}
            token_types = list(live_tokens)
            exp = IdentityExtractor.exp_from_tokens(live_tokens)
            durable_rollable = has_refresh_credential(live_tokens)
        else:
            try:
                token_types = json.loads(durable_source.token_types_json or "[]")
            except (TypeError, ValueError):
                token_types = []
            if isinstance(durable_source.credential_expires_at, datetime):
                exp = durable_source.credential_expires_at.timestamp()
        row = _row(
            provider_id,
            account_id,
            label="Pending identity" if account_id == "default" else None,
            source=durable_source.source_type,
            source_name=sidecar_names.get(
                durable_source.sidecar_id or "", durable_source.sidecar_id
            ),
            token_types=token_types,
            exp=exp,
            # A refresh token on a live bundle means the server rolls it before it
            # lapses (and a manual refresh works); without these a 2h-valid Gemini
            # credential read "expiring" — the auto-refresh allowance never applied.
            can_refresh=durable_rollable
            and provider_id in REFRESH_ENDPOINTS
            and not (provider_id in ROTATING_REFRESH_PROVIDERS and durable_source.sidecar_id),
            rollable=durable_rollable,
            machine_renewed=durable_rollable
            and provider_id in ROTATING_REFRESH_PROVIDERS
            and bool(durable_source.sidecar_id),
        )
        row["_rejected"] = is_durably_rejected(
            durable_source, durable_by_account.get((provider_id, account_id), [])
        )
        row["_failing"] = is_failing(
            durable_source, durable_by_account.get((provider_id, account_id), [])
        )
        if live is None and is_stale_source(durable_source.last_seen):
            # Not in the live cache and not re-reported recently: the stored
            # token types/expiry are history, not evidence. Without this a
            # removed machine's row reads "valid" forever (or "expired" forever,
            # re-firing alerts); ``stale`` is neither healthy nor alert-worthy.
            row["status"] = "stale"
        row["removable"] = False
        row["source_id"] = durable_source.source_id
        row["sidecar_id"] = durable_source.sidecar_id
        row["identity_pending"] = account_id == "default"
        result.append(row)


def _append_config_rows(
    result: list[dict[str, Any]],
    seen_token_values: set[str],
    configs: list[ProviderConfig],
) -> None:
    # Also surface API keys / session cookies configured in Settings → Providers.
    # These are stored encrypted in ProviderConfig but never flow through token_cache
    # under their own row, so they would otherwise be invisible to the panel.
    try:
        for cfg in configs:
            if not cfg.enabled:
                continue
            account = cfg.account_id or "default"
            for value, prefix, token_type in (
                (cfg.api_key, "config", "api_key"),
                (cfg.session_cookie, "config-cookie", "session_cookie"),
            ):
                # Skip if this exact value is already in the live session cache
                # (and remember it, so the server-file scan below doesn't
                # re-list a dashboard-saved key as a file credential).
                if not value:
                    continue
                seen_key = f"{cfg.provider_id}:{value}"
                already_listed = seen_key in seen_token_values
                seen_token_values.add(seen_key)
                if already_listed:
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


def assemble_health_rows(
    stats: dict[str, Any],
    claude_sources: list[dict[str, Any]],
    sidecar_names: dict[str, str],
    durable_sources: list[CredentialSource],
    configs: list[ProviderConfig],
    retired_accounts: set[tuple[str, str]],
    candidates_by_account: dict[tuple[str, str], list[dict[str, Any]]],
    cache_tokens: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Build every row except the server-discovered credentials."""
    result: list[dict[str, Any]] = []
    seen_token_values: set[str] = set()
    _append_cache_rows(
        result,
        seen_token_values,
        stats,
        cache_tokens,
        retired_accounts,
        candidates_by_account,
        durable_sources,
        sidecar_names,
        claude_sources,
    )
    _append_claude_rows(result, seen_token_values, claude_sources, sidecar_names)
    _append_durable_rows(
        result, durable_sources, retired_accounts, candidates_by_account, sidecar_names
    )
    _append_config_rows(result, seen_token_values, configs)
    return result, seen_token_values


def append_server_rows(
    result: list[dict[str, Any]],
    seen_token_values: set[str],
    server_creds: dict[str, dict[str, Any]],
) -> None:
    """One row per credential family the server discovered (env vars, owned files)."""
    for provider, creds in server_creds.items():
        fresh = {k: v for k, v in creds.items() if f"{provider}:{v}" not in seen_token_values}
        # One row per credential family: a dead OAuth JWT must not mark an
        # independent API key beside it expired.
        oauth = {k: v for k, v in fresh.items() if k in _OAUTH_FAMILY_KEYS}
        other = {k: v for k, v in fresh.items() if k not in _OAUTH_FAMILY_KEYS}
        for family in (oauth, other):
            if not family:
                continue
            result.append(
                _row(
                    provider,
                    "server",
                    label=None,
                    source="server",
                    source_name="server",
                    token_types=list(family.keys()),
                    exp=IdentityExtractor.exp_from_tokens(family),
                    can_refresh=False,
                    rollable=has_refresh_credential(family),
                )
            )


def finalize_health_rows(result: list[dict[str, Any]]) -> None:
    """Apply rejection/failure upgrades and the redundancy flag, then strip internals."""
    apply_invalid(result)

    # Mark expired, unrefreshable entries as "redundant" when another credential
    # for that account is still healthy. Such an entry can't be auto-rolled and
    # isn't blocking collection — so the dashboard banner should not raise a hard
    # alarm on it alone. See `redundancy_sibling` for what counts as a sibling;
    # rows that are merely assumed valid (config / env, no expiry) never do.
    healthy = [r for r in result if r["status"] in ("valid", "expiring") and not r["_assumed"]]
    for r in result:
        r["redundant"] = (
            r["status"] == "expired"
            and not r["_rollable"]
            and any(
                h is not r and h["provider"] == r["provider"] and redundancy_sibling(h, r)
                for h in healthy
            )
        )
    for r in result:
        r.pop("_assumed", None)
        r.pop("_rollable", None)
