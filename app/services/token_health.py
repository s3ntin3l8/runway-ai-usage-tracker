"""Token health inspection — expiry parsing, status classification."""

import asyncio
import logging
from typing import Any

from sqlmodel import Session, col
from sqlmodel import select as sqlselect

from app.core.config import settings
from app.core.db import engine
from app.core.registry import registry
from app.core.utils import scrub_log
from app.models.db import CredentialSource, ProviderConfig, SidecarRegistry
from app.services.account_identity import canonical_account_id
from app.services.credential_provider import CredentialProvider
from app.services.credential_tags import archived_account_pairs_from_configs
from app.services.token_cache import token_cache
from app.services.token_health_rows import (
    append_server_rows,
    assemble_health_rows,
    finalize_health_rows,
    pending_claude_sources,
)
from app.services.token_health_status import (
    EXPIRY_WARNING_SECS,
    FAILING_AFTER_ATTEMPTS,
    FAILING_MAX_AGE_SECS,
    FAILING_MIN_DURATION_SECS,
    SOURCE_STALE_SECS,
    apply_failure,
    apply_invalid,
    apply_rejection,
    build_accounts_by_provider,
    classify_status,
    credential_status,
    is_durably_rejected,
    is_failing,
    is_flagged,
    is_redundancy_sibling,
    is_stale_source,
    is_synthetic,
    redundancy_sibling,
    underlying_account,
    utc,
)

# Public status rules live in ``token_health_status`` and are re-exported here so
# existing imports (and tests that mutate ``settings`` on this module) keep working.
__all__ = [
    "EXPIRY_WARNING_SECS",
    "FAILING_AFTER_ATTEMPTS",
    "FAILING_MAX_AGE_SECS",
    "FAILING_MIN_DURATION_SECS",
    "SOURCE_STALE_SECS",
    "TokenHealthService",
    "apply_invalid",
    "build_accounts_by_provider",
    "classify_status",
    "_collect_server_credentials",
    "is_stale_source",
    "is_synthetic",
    "redundancy_sibling",
    "_scan_server_credentials",
    "underlying_account",
    "utc",
    "apply_failure",
    "apply_rejection",
    "credential_status",
    "is_durably_rejected",
    "is_failing",
    "is_flagged",
    "is_redundancy_sibling",
    "settings",
    "token_health_service",
]

logger = logging.getLogger(__name__)


def _load_sidecar_context() -> tuple[dict[str, str], list[CredentialSource]]:
    """Sidecar display names and machine-owned credential sources."""
    sidecar_names: dict[str, str] = {}
    durable_sources: list[CredentialSource] = []
    try:
        with Session(engine) as _s:
            for sc in _s.exec(sqlselect(SidecarRegistry)).all():
                sidecar_names[sc.sidecar_id] = sc.custom_name or sc.hostname or sc.sidecar_id
            durable_sources = list(
                _s.exec(
                    sqlselect(CredentialSource).where(col(CredentialSource.sidecar_id).is_not(None))
                ).all()
            )
            durable_sources = [
                item
                for item in durable_sources
                # Prevent future CredentialSource subclasses with different
                # field contracts from leaking rows without required strings.
                if type(item) is CredentialSource
                and isinstance(item.provider_id, str)
                and isinstance(item.source_id, str)
                and isinstance(item.sidecar_id, str)
            ]
    except Exception as e:
        logger.warning(f"Could not load sidecar names for token health: {e}")
    return sidecar_names, durable_sources


def _load_provider_configs() -> list[ProviderConfig]:
    """Dashboard-saved credentials, and the retired-account rows derived from them.

    Its own query, so a failure here is reported as what it is rather than as a
    missing sidecar name.
    """
    configs: list[ProviderConfig] = []
    try:
        with Session(engine) as _s:
            configs = list(_s.exec(sqlselect(ProviderConfig)).all())
    except Exception as e:
        logger.warning(f"Could not load ProviderConfig credentials for token health: {e}")
    return configs


async def _load_source_candidates(
    stats: dict[str, Any], durable_sources: list[CredentialSource]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    candidate_accounts = sorted(
        {(provider, account_id) for provider, accounts in stats.items() for account_id in accounts}
        | {(item.provider_id, item.account_id) for item in durable_sources}
    )
    candidate_rows = await asyncio.gather(
        *(
            token_cache.get_source_candidates(provider, account_id)
            for provider, account_id in candidate_accounts
        )
    )
    return dict(zip(candidate_accounts, candidate_rows, strict=True))


async def _load_cache_tokens(
    stats: dict[str, Any], retired_accounts: set[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Live bundles for cache accounts Token Health still lists.

    Retired accounts are skipped, matching the row walk: a push that raced the
    archive can still sit in the cache, but it is not a live credential.
    """
    loaded: dict[tuple[str, str], dict[str, Any]] = {}
    for provider, accounts in stats.items():
        for acc_id in accounts:
            if (provider, canonical_account_id(acc_id)) in retired_accounts:
                continue
            loaded[(provider, acc_id)] = await token_cache.get(provider, acc_id) or {}
    return loaded


def _collect_server_credentials() -> dict[str, dict[str, Any]]:
    """Seam for :func:`_scan_server_credentials` (tests isolate the host through it)."""
    return _scan_server_credentials()


def _scan_server_credentials() -> dict[str, dict[str, Any]]:
    """Credentials the *server itself* discovered (env vars, Runway-owned files) per provider.

    Blocking (file + DB reads); call through ``asyncio.to_thread``.
    """
    found: dict[str, dict[str, Any]] = {}
    for provider_id in registry.get_all_providers():
        try:
            creds = CredentialProvider.get_credentials(provider_id)
        except Exception as e:
            logger.debug(f"Server credential scan failed for {scrub_log(provider_id)}: {e}")
            continue
        # "config" also covers files inside Runway's own config dir (e.g.
        # github_oauth.json); values that are really a dashboard-saved
        # ProviderConfig key are dropped by the caller's dedup.
        server_tokens = {
            k: v for k, v in creds.items() if v and creds.sources.get(k) in ("server", "config")
        }
        if server_tokens:
            found[provider_id] = server_tokens
    return found


class TokenHealthService:
    async def get_health(self) -> list[dict[str, Any]]:
        """Return a health record for each known credential."""
        stats = await token_cache.get_all_stats()
        claude_sources = pending_claude_sources(
            await token_cache.get_source_credentials("anthropic")
        )
        sidecar_names, durable_sources = _load_sidecar_context()
        # Two readers, one query: the retired accounts (a retired account holds
        # no live credential — see #895) and the dashboard-saved credentials
        # further down.
        configs = _load_provider_configs()
        # A retired account holds no live credential: its source record stays
        # visible in the credential inventory, but nothing live is held for it,
        # so Token Health has nothing to report about it.
        retired_accounts = archived_account_pairs_from_configs(configs)
        candidates_by_account = await _load_source_candidates(stats, durable_sources)
        cache_tokens = await _load_cache_tokens(stats, retired_accounts)
        result, seen_token_values = assemble_health_rows(
            stats,
            claude_sources,
            sidecar_names,
            durable_sources,
            configs,
            retired_accounts,
            candidates_by_account,
            cache_tokens,
        )

        # Credentials the server discovered itself (env vars, Runway-owned files).
        # They never enter token_cache, so this is the only place they show up.
        try:
            server_creds = await asyncio.to_thread(_collect_server_credentials)
            append_server_rows(result, seen_token_values, server_creds)
        except Exception as e:
            logger.debug(f"Server credential scan for token health failed: {e}")

        finalize_health_rows(result)
        return result


token_health_service = TokenHealthService()
