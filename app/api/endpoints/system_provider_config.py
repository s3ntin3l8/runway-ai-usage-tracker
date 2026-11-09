import logging
from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_
from sqlmodel import Session, col, select

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.core.utils import scrub_log
from app.models.db import (
    CredentialSource,
    LatestUsage,
    LatestUsageContribution,
    ProviderAccountLabel,
    ProviderConfig,
    SystemConfig,
    UsageEvent,
)
from app.services import audit_log, auth_failures, source_controls
from app.services.account_identity import (
    EMAIL_RE,
    HASH_RE,
    account_usage_provider_ids,
    canonical_account_id,
)
from app.services.account_merges import (
    is_merged_away,
    merged_accounts,
)
from app.services.billing_type import UNKNOWN, BillingType, detect_billing_type
from app.services.collector_manager import manager
from app.services.collectors.anthropic_console import (
    ConsoleAPIError,
    console_get,
    console_organizations,
    parse_console_balance,
)
from app.services.credential_sources import (
    describe_origin,
    describe_origin_full,
    effective_health,
    is_server_source_id,
)
from app.services.credential_tags import CredentialTagRepo
from app.services.token_cache import OAUTH_TOKEN_VALUE_KEYS, token_cache

logger = logging.getLogger(__name__)
router = APIRouter()


class _CredentialSourcePreference(BaseModel):
    source_id: str
    enabled: bool
    # Zero-based ordering rank; duplicate ranks are resolved by source_id.
    priority: int = Field(ge=0)


class _CredentialSourcesUpdate(BaseModel):
    sources: list[_CredentialSourcePreference]
    all_machines: bool = False


class _ProviderConfigUpdate(BaseModel):
    enabled: bool | None = None
    archived: bool | None = None
    api_key: str | None = None  # empty string = clear, None = no change
    session_cookie: str | None = None  # empty string = clear, None = no change
    # Explicit clear flags (PR #287). Mirrors the implicit "empty string = clear"
    # contract above but lets the UI show a dedicated "Clear stored credential"
    # button without ambiguous blank-string semantics. When set to True the
    # corresponding `api_key` / `session_cookie` field is ignored and the
    # stored credential is wiped. Always wins over a same-field write.
    clear_api_key: bool | None = None
    clear_session_cookie: bool | None = None
    account_label: str | None = None
    poll_interval_seconds: int | None = None
    collection_strategies: list[dict] | None = None  # [{"id": "web", "enabled": true}, ...]
    opencode_workspace_id: str | None = None  # empty string clears; None = no change
    billing_type: Literal["subscription", "pay_as_you_go", "unknown"] | None = None


class _AccountPreviewRequest(BaseModel):
    """Body for POST /provider-config/preview-account (#287).

    The wizard's step 2 debounces keystrokes into this endpoint, derives the
    canonical ``account_id`` from the credential, and surfaces "this
    identity already exists" via 409.
    """

    provider_id: str
    api_key: str | None = None
    session_cookie: str | None = None
    organization_id: str | None = None
    # Ask the server to prove how the account behind this credential bills.
    # Off by default: the wizard debounces keystrokes into this endpoint, and
    # a proof costs a round trip to Claude. The operator asks for it
    # explicitly ("Detect billing model"), so the cost is paid once.
    detect_billing: bool = False


def _supported_saved_strategies(
    saved: list[dict] | None, supported: list[dict]
) -> list[dict] | None:
    if saved is None:
        return None
    allowed = {entry.get("id") for entry in supported}
    return [entry for entry in saved if entry.get("id") in allowed]


class _ConsoleOrganizationsRequest(BaseModel):
    session_cookie: str


@router.get("/provider-configs", dependencies=[Depends(require_admin_key)])
@limiter.limit("30/minute")
async def list_provider_configs(request: Request, session: Session = Depends(get_session)) -> dict:  # noqa: PLR0915
    """Return all known providers merged with their DB configuration."""
    from app.core.registry import registry

    # Load DB configs grouped by provider_id so the response can expose every
    # row under the new `accounts` field without silently collapsing N rows
    # to one (the multi-account hardening). The legacy top-level fields stay
    # byte-identical for single-row users by sourcing from the canonical row
    # (the `account_id="default"` row when present, else the first row).
    db_rows = session.exec(select(ProviderConfig)).all()
    rows_by_provider: dict[str, list[ProviderConfig]] = {}
    for r in db_rows:
        rows_by_provider.setdefault(r.provider_id, []).append(r)
    archived_account_usage = _archived_account_usage_flags(
        session, [row for row in db_rows if row.archived]
    )

    # The accounts that were merged away, and where each of them went. They
    # hold no credential any more, so they are not among the accounts below —
    # this is what tells the operator "alice was merged into bob" instead of
    # letting alice disappear without a trace.
    merged_by_provider: dict[str, list[dict[str, str]]] = {}
    merged_into: dict[tuple[str, str], str] = {}
    for merged in merged_accounts(session):
        merged_by_provider.setdefault(merged.provider_id, []).append(
            {
                "account_id": merged.account_id,
                "destination_account_id": merged.destination_account_id,
                "merged_at": merged.merged_at,
            }
        )
        merged_into[(merged.provider_id, merged.account_id)] = merged.destination_account_id

    credential_sources_by_account: dict[tuple[str, str], list[CredentialSource]] = {}
    for source in session.exec(select(CredentialSource)).all():
        credential_sources_by_account.setdefault(
            (source.provider_id, source.account_id), []
        ).append(source)

    async def _source_summaries(provider_id: str, account_id: str) -> list[dict[str, Any]]:
        candidates = await token_cache.get_source_candidates(provider_id, account_id)
        live_by_id = {item["source_id"]: item for item in candidates}
        # Server env/file sources are shown by the credentials inventory; they aren't
        # failover candidates, so they must not appear in the reorder/enable editor.
        rows = [
            row
            for row in credential_sources_by_account.get((provider_id, account_id), [])
            if not is_server_source_id(row.source_id)
        ]
        summaries = []
        for row in sorted(rows, key=lambda item: (item.priority, item.id or 0)):
            # Describe the origin instead of trusting the stored type: an older
            # ingest stored "sidecar" for a cookie, and the row must say it is a
            # browser session, not another credential file. A row that already
            # names its kind keeps the label the ingest gave it.
            described = (
                describe_origin_full(row.credential_origin) if row.credential_origin else None
            )
            origin_kind = described.kind if described else row.source_type
            origin_label = described.label if described else row.source_label
            stale_kind = row.source_type == "sidecar" and origin_kind != "sidecar"
            summaries.append(
                {
                    "source_id": row.source_id,
                    "source_type": origin_kind if stale_kind else row.source_type,
                    "source_label": origin_label if stale_kind else row.source_label,
                    "source_detail": described.detail if described else None,
                    "sidecar_id": row.sidecar_id,
                    "enabled": row.enabled,
                    "priority": row.priority,
                    "last_seen": row.last_seen.isoformat() if row.last_seen else None,
                    "health": effective_health(row)
                    if row.source_id in live_by_id
                    else "unavailable",
                    "available": row.source_id in live_by_id,
                }
            )
        known_ids = {row.source_id for row in rows}
        # Sidecar manifests may already have supplied live credentials while
        # the durable source summary has not yet been written. Include those
        # candidates so settings can show/tag OpenCode auth.json sources.
        for item in candidates:
            if item["source_id"] in known_ids:
                continue
            origin = item.get("credential_origin") or ""
            described = describe_origin_full(origin) if origin else None
            summaries.append(
                {
                    "source_id": item["source_id"],
                    "source_type": (
                        described.kind
                        if described and described.kind != "sidecar"
                        else item.get("source_type", "sidecar")
                    ),
                    # A native Anthropic cookie names its client ("Chrome cookie");
                    # every other origin is named by what it is.
                    "source_label": describe_origin(origin)[1],
                    "source_detail": described.detail if described else None,
                    "sidecar_id": item.get("sidecar_id"),
                    "enabled": item.get("enabled", True),
                    "priority": item.get("priority", 0),
                    "last_seen": None,
                    "health": "untried",
                    "available": True,
                }
            )
        return summaries

    # Pre-compute the set of (provider_id, account_id) pairs that have at
    # least one row in latest_usage — drives the per-account `is_orphaned`
    # flag in the response. Single batched DISTINCT query instead of one
    # subquery per row; O(1) lookup when building each entry. Also used
    # below as a durable fallback for passive providers whose credentials
    # live only in token_cache / latest_usage (no provider_configs row).
    from app.models.db import LatestUsage

    live_keys: set[tuple[str, str]] = set()
    live_rows = session.exec(
        select(LatestUsage.provider_id, LatestUsage.account_id)
        # Exclude rows that are already stale — staleness lives inside
        # card_json as {"stale": true}. json_extract returns 1 (SQLite's
        # true) for stale rows; NULL means the key is absent, which means
        # not stale. coalesce(..., 0) != 1 treats absent and false as
        # non-stale without requiring an explicit boolean comparison.
        .where(func.coalesce(func.json_extract(LatestUsage.card_json, "$.stale"), 0) != 1)
        .distinct()
    ).all()
    for provider_id, account_id in live_rows:
        if account_id:
            live_keys.add((provider_id, account_id))

    account_label_overrides = {
        (row.provider_id, row.account_id): row.account_label
        for row in session.exec(select(ProviderAccountLabel)).all()
    }

    # Active in-memory credentials (sidecar-discovered / server-local).
    # Passive providers (antigravity, opencode-free, …) never get a
    # provider_configs row, so the v2 list must union these in or they
    # render as "unconfigured" while collecting fine (token health shows
    # them working).
    cache_accounts: dict[str, list[tuple[str, str | None]]] = {}
    for c_pid, c_aid, c_name in await token_cache.get_all_active_accounts():
        cache_accounts.setdefault(c_pid, []).append((c_aid, c_name))

    cache_details: dict[tuple[str, str], dict[str, Any]] = {}
    for cached_pid in manager.collector_registry:
        for cached in await token_cache.get_accounts(cached_pid):
            cache_details[(cached_pid, cached["account_id"])] = cached

    def _discovery_labels(provider_id: str, account_id: str) -> list[str]:
        cached = cache_details.get((provider_id, account_id))
        if not cached or cached.get("source") in (None, "config", "manual_config", "server"):
            return []
        provider_rules = (registry.get_provider(provider_id) or {}).get("rules", [])
        token_names = set((cached.get("tokens") or {}).keys())
        labels: set[str] = set()
        for rule in provider_rules:
            rule_type = rule.get("type")
            if rule_type not in ("env", "file", "keychain", "cookie"):
                continue
            mapping = rule.get("mapping", {})
            matching_token_types = token_names.intersection(mapping.values())
            if not matching_token_types:
                continue
            kind = _discovery_credential_kind(rule_type, matching_token_types)
            paths = rule.get("paths", [])
            if rule_type == "file" and paths:
                import os

                labels.add(f"{os.path.basename(paths[0])} · {kind}")
            elif rule_type == "env":
                labels.add(f"{rule.get('variable', 'Environment')} · {kind}")
            else:
                labels.add(f"Sidecar · {kind}")
        return sorted(labels)

    def _canonical_row(rows: list[ProviderConfig]) -> ProviderConfig | None:
        for r in rows:
            if r.account_id == "default":
                return r
        return rows[0] if rows else None

    # Fetch global default interval
    sys_cfg = session.exec(select(SystemConfig)).first()
    global_poll_interval = sys_cfg.default_poll_interval_seconds if sys_cfg else None

    results = []
    for p_id, (_, name, default_ttl) in manager.collector_registry.items():
        provider_def = registry.get_provider(p_id) or {}
        icon = provider_def.get("icon", _PROVIDER_ICONS.get(p_id, "🔌"))
        provider_rows = rows_by_provider.get(p_id, [])
        db = _canonical_row(provider_rows)
        rules = provider_def.get("rules", [])
        # The xAI OAuth bearer is mirrored into the `xai_access` slot so the
        # existing manual API-key field can configure the Grok collector.
        supports_api_key = any(
            any(
                k in rule.get("mapping", {}).values()
                for k in ("api_key", "oauth_token", "xai_access")
            )
            for rule in rules
            if rule.get("type") in ("env", "file", "keychain")
        )
        supports_session_cookie = bool(provider_def.get("supports_session_cookie")) or any(
            any(
                k in rule.get("mapping", {}).values()
                for k in (
                    "session_cookie",
                    "cookie_session",
                    "cookie_sessionKey",
                    "cookie___Secure-next-auth.session-token",
                    "sessionKey",
                    "console_session",
                )
            )
            for rule in rules
            if rule.get("type") in ("env", "file", "keychain", "cookie")
        )

        poll_source = "default"
        effective_interval = default_ttl
        if db and db.poll_interval_seconds:
            poll_source = "provider_override"
            effective_interval = db.poll_interval_seconds
        elif global_poll_interval:
            poll_source = "global_override"
            effective_interval = global_poll_interval

        # Per-provider: does any *non-default* account have a `latest_usage`
        # row? Used to gate the `is_orphaned` flag — without a live sibling,
        # the default row's absence from `latest_usage` is equally explained
        # by "just configured" or "collection currently failing", neither of
        # which is a safe-to-remove condition (see flag docs in the response
        # builder below).
        provider_has_live_sibling = any(
            (p_id, r.account_id) in live_keys and r.account_id != "default" for r in provider_rows
        )

        # Union DB rows with cache-only / latest_usage-only identities so
        # passive providers (no provider_configs row by design) still show
        # as configured. DB entries keep `source="config"`; synthetic ones
        # are `source="discovered"`.
        accounts_out: list[dict[str, Any]] = [
            {
                "account_id": r.account_id,
                # Which provider this account belongs to. The Claude family
                # groups several providers under one card, so an account that
                # is listed under the family is still addressed by its own
                # provider — an edit must never land on the family's
                # representative (#723).
                "provider_id": p_id,
                "enabled": r.enabled,
                "archived": r.archived,
                # Where this account went, when it was merged away. The row
                # stays behind (archived, no credential) — the merge record is
                # what says the account is gone, not this row.
                "merged_into": merged_into.get((p_id, canonical_account_id(r.account_id))),
                "has_usage_events": archived_account_usage.get((p_id, r.account_id), False),
                "api_key_set": bool(r.api_key_encrypted),
                "session_cookie_set": bool(r.session_cookie_encrypted),
                "account_label": r.account_label,
                "poll_interval_seconds": r.poll_interval_seconds,
                "collection_strategies": _supported_saved_strategies(
                    r.strategies, manager.get_supported_strategies(p_id)
                ),
                "opencode_workspace_id": r.opencode_workspace_id,
                "claude_organization_id": r.claude_organization_id,
                "billing_type": r.billing_type,
                # `is_orphaned` surfaces the orphaned-bookkeeping-row
                # bug in the settings UI: #286 highlights
                # `account_id="default"` rows that have been shadowed
                # by another account on the same provider. The flag
                # only fires when:
                #   1. the row is the default sentinel
                #   2. the row itself has no live data (missing
                #      from `latest_usage`)
                #   3. AND at least one *other* account on this
                #      provider IS in `latest_usage` — without a
                #      replacement, "just-configured" and
                #      "collection currently failing" rows would be
                #      flagged too, and pairing that with the
                #      destructive Remove button would be a
                #      data-loss prompt on the user's only credential.
                "is_orphaned": (
                    r.account_id == "default"
                    and (p_id, r.account_id) not in live_keys
                    and provider_has_live_sibling
                ),
                "source": "config",
                "credential_source_labels": _discovery_labels(p_id, r.account_id),
                "credential_sources": await _source_summaries(p_id, r.account_id),
            }
            for r in provider_rows
        ]
        seen_account_ids = {r.account_id for r in provider_rows}
        for c_aid, c_name in cache_accounts.get(p_id, []):
            if c_aid in seen_account_ids:
                continue
            seen_account_ids.add(c_aid)
            accounts_out.append(
                {
                    "account_id": c_aid,
                    "provider_id": p_id,
                    "enabled": True,
                    "archived": False,
                    "merged_into": None,
                    "has_usage_events": False,
                    "api_key_set": False,
                    "session_cookie_set": False,
                    "account_label": account_label_overrides.get((p_id, c_aid)) or c_name,
                    "poll_interval_seconds": None,
                    "collection_strategies": None,
                    "opencode_workspace_id": None,
                    "claude_organization_id": None,
                    "is_orphaned": False,
                    "source": "discovered",
                    "credential_source_labels": _discovery_labels(p_id, c_aid),
                    "credential_sources": await _source_summaries(p_id, c_aid),
                }
            )
        # Durable fallback for passive providers only (no config rows ever):
        # identities that produced usage but are no longer in the (30-min TTL)
        # token cache — keeps passive status across a server restart until the
        # sidecar re-pushes credentials. Skipped when config rows exist so a
        # deleted account is not resurrected from historical latest_usage.
        if not provider_rows:
            for live_pid, live_aid in sorted(live_keys):
                if live_pid != p_id or live_aid in seen_account_ids:
                    continue
                seen_account_ids.add(live_aid)
                accounts_out.append(
                    {
                        "account_id": live_aid,
                        "provider_id": p_id,
                        "enabled": True,
                        "archived": False,
                        "merged_into": None,
                        "has_usage_events": False,
                        "api_key_set": False,
                        "session_cookie_set": False,
                        "account_label": account_label_overrides.get((live_pid, live_aid)),
                        "poll_interval_seconds": None,
                        "collection_strategies": None,
                        "opencode_workspace_id": None,
                        "claude_organization_id": None,
                        "is_orphaned": False,
                        "source": "discovered",
                        "credential_source_labels": [],
                        "credential_sources": [],
                    }
                )

        results.append(
            {
                "provider_id": p_id,
                "name": name,
                "icon": icon,
                "enabled": db.enabled if db else True,
                "archived": db.archived if db else False,
                "api_key_set": bool(db and db.api_key_encrypted),
                "session_cookie_set": bool(db and db.session_cookie_encrypted),
                "account_label": db.account_label if db else None,
                "poll_interval_seconds": db.poll_interval_seconds if db else None,
                "default_ttl_seconds": default_ttl,
                "effective_poll_interval": effective_interval,
                "poll_interval_source": poll_source,
                "supports_api_key": supports_api_key,
                "supports_session_cookie": supports_session_cookie,
                "api_key_label": provider_def.get("api_key_label"),
                "api_key_help": provider_def.get("api_key_help"),
                "session_cookie_label": provider_def.get("session_cookie_label"),
                "session_cookie_help": provider_def.get("session_cookie_help"),
                # Strategy configuration
                "supported_strategies": manager.get_supported_strategies(p_id),
                "collection_strategies": _supported_saved_strategies(
                    db.strategies if db else None, manager.get_supported_strategies(p_id)
                ),
                "opencode_workspace_id": db.opencode_workspace_id if db else None,
                "claude_organization_id": db.claude_organization_id if db else None,
                # Per-account breakdown: DB rows first (config-backed), then
                # cache/latest_usage-only identities (discovered). The
                # canonical row above is the first entry whose account_id is
                # "default", or the first entry overall when no default exists.
                "accounts": accounts_out,
                "account_count": sum(not a.get("archived", False) for a in accounts_out),
                "archived_count": sum(bool(a.get("archived", False)) for a in accounts_out),
                # The accounts that were merged away, and where each of them
                # went. A merged-away account holds no credential and is not an
                # account any more: the row the merge left behind stays in
                # `accounts` (archived, no credential) and is marked with
                # `merged_into`, while this says where the account went.
                "merged_accounts": merged_by_provider.get(p_id, []),
            }
        )

    return {"providers": results}


@router.put("/provider-config/{provider_id}/{account_id}")
@limiter.limit("20/minute")
async def upsert_provider_config_for_account(  # noqa: PLR0915 — known-debt: per-field validation + persistence, refactor tracked separately
    request: Request,
    provider_id: str,
    account_id: str,
    body: _ProviderConfigUpdate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Create or update provider configuration for a specific account.

    Multi-account canonical endpoint. The path parameter ``account_id`` is the
    authoritative identity under which credentials are stored and propagated to
    the in-memory token cache; the body may include an ``account_label`` to set
    a human-readable name. Use ``GET /provider-configs`` to discover existing
    ``account_id`` values per provider.
    """
    if provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    # Store under the canonical form every other write path uses, so a
    # typed ``Alice@X.com`` lines up with the cards/events for alice@x.com.
    original_account_id = account_id
    account_id = canonical_account_id(account_id)
    # If this is a generic or derived hash account_id and the caller supplied a valid
    # email label, adopt the email as the authoritative account_id on creation so
    # API key accounts don't linger under opaque hashes.
    rewritten_from: str | None = None
    if body.account_label and EMAIL_RE.match(body.account_label.strip()):
        existing = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == provider_id,
                ProviderConfig.account_id == account_id,
            )
        ).first()
        if existing is None and (account_id == "default" or bool(HASH_RE.match(account_id))):
            rewritten_from = original_account_id
            account_id = canonical_account_id(body.account_label)

    # A model something proved, as opposed to one a person picked. The source
    # column tells the two apart, so the repair that prices the accounts
    # predating detection never mistakes a proof for a choice.
    detected_billing_type: BillingType | None = None

    # A merged-away account is gone: the merge moved its history and its
    # credential to the destination, so the source id no longer names an
    # account. Any write that would make it one again — restoring it, creating
    # the row, or giving it a credential — is refused; only an explicit
    # archive is allowed, because that keeps the account inert.
    merged_destination = is_merged_away(session, provider_id, account_id)
    if merged_destination:
        writes_a_credential = (
            body.api_key is not None
            or body.session_cookie is not None
            or bool(body.clear_api_key)
            or bool(body.clear_session_cookie)
        )
        if body.archived is not True or writes_a_credential:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This account was merged into {merged_destination} and no longer exists; "
                    "it cannot be restored or given a credential."
                ),
            )

    if provider_id == "anthropic_console":
        try:
            account_id = str(UUID(account_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Select a Console organization") from exc
        existing_console = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == provider_id,
                ProviderConfig.account_id == account_id,
            )
        ).first()
        console_billing_type: BillingType | None = None
        if (
            body.session_cookie
            and not body.clear_session_cookie
            and (existing_console is None or body.session_cookie != existing_console.session_cookie)
        ):
            label, console_billing_type = await _validate_console_connection(
                body.session_cookie, account_id
            )
            if body.account_label is None:
                body.account_label = label
        if not existing_console and (not body.session_cookie or body.clear_session_cookie):
            raise HTTPException(status_code=422, detail="Enter a Console session cookie")
        if not existing_console and body.billing_type is None and console_billing_type is not None:
            # The balance this account publishes is prepaid credit, so the
            # account spends — proven by the detector, not assumed per
            # provider. It is a proof, not a choice, and the source column
            # says which it is.
            detected_billing_type = console_billing_type

    await _apply_provider_config_update(
        session,
        provider_id,
        account_id,
        body,
        detected_billing_type=detected_billing_type,
    )
    response_data: dict[str, str] = {
        "status": "saved",
        "provider_id": provider_id,
        "account_id": account_id,
    }
    if rewritten_from is not None:
        response_data["original_account_id"] = rewritten_from
    return response_data


@router.patch("/provider-config/{provider_id}/{account_id}/credential-sources")
@limiter.limit("20/minute")
async def update_credential_sources(
    request: Request,
    provider_id: str,
    account_id: str,
    body: _CredentialSourcesUpdate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Update enabled state and priority for sources on any account, discovered or configured."""
    if provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")
    account_id = canonical_account_id(account_id)
    source_preferences = await _update_credential_source_preferences(
        session, provider_id, account_id, body.sources, all_machines=body.all_machines
    )
    session.commit()
    manager.set_credential_source_preferences(provider_id, account_id, source_preferences)
    manager.last_sync_time = 0.0
    try:
        await manager.reset_collector(provider_id, account_id)
    except Exception:
        logger.exception(
            "Could not reset collector after credential source preferences changed: %s/%s",
            scrub_log(provider_id),
            scrub_log(account_id),
        )
    # audit_log.record commits its row in its own savepoint transaction.
    audit_log.record(
        session,
        request,
        action="credential.sources_update",
        target_id=f"{provider_id}/{account_id}",
        payload={
            "all_machines": body.all_machines,
            "sources": [
                {"source_id": item.source_id, "enabled": item.enabled, "priority": item.priority}
                for item in body.sources
            ],
        },
    )
    from app.core.cache import cache_clear
    from app.services.poller import poller

    cache_clear()
    poller.wake()
    return {"status": "saved", "provider_id": provider_id, "account_id": account_id}


@router.delete("/provider-config/{provider_id}/{account_id}")
@limiter.limit("20/minute")
async def delete_provider_config_for_account(
    request: Request,
    provider_id: str,
    account_id: str,
    permanent: bool = False,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Archive an account, or permanently remove an empty archived account.

    The ordinary Remove action soft-archives and clears stored credentials,
    retaining the row for restoration and keeping usage history out of the
    active fleet. ``?permanent=true`` is the explicit cleanup action for an
    already archived account with no usage events; it deletes the account row
    and its non-secret source metadata. OpenCode history checks cover Go,
    Zen, and Free event provider IDs because they share one account identity.
    """
    if provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == provider_id,
            ProviderConfig.account_id == account_id,
        )
    ).first()
    if row is None and canonical_account_id(account_id) != account_id:
        # Accept a non-canonical spelling of a canonical row (PUT stores
        # canonical ids); exact match above still wins for legacy rows.
        account_id = canonical_account_id(account_id)
        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == provider_id,
                ProviderConfig.account_id == account_id,
            )
        ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No provider_config for {provider_id}/{account_id}",
        )

    if permanent and not row.archived:
        raise HTTPException(
            status_code=409,
            detail="Only archived accounts can be permanently deleted.",
        )
    if permanent:
        if _has_account_usage(provider_id, account_id, session):
            raise HTTPException(
                status_code=409,
                detail="Accounts with usage history cannot be permanently deleted.",
            )

    # Evict matching LatestUsage rows so the dashboard doesn't show ghost cards
    # for an account the operator just removed. usage_events / rollups are
    # intentionally left in place — they're event-sourced history and stay
    # readable via /usage/cumulative even without a live card.
    session.exec(
        delete(LatestUsage).where(
            col(LatestUsage.provider_id) == provider_id,
            col(LatestUsage.account_id) == account_id,
        )
    )

    # Drop every credential_tags row tied to this account_id. Otherwise
    # list_pending_payload (app/services/credential_tags.py:124) keeps
    # shipping the hint to sidecars and the /fleet/ingest path stamps
    # every new event with the now-removed account — effectively
    # resurrecting the pair on the dashboard via the synthetic loop.

    tags_cleared = CredentialTagRepo.delete_by_account(
        session, provider_id=provider_id, account_id=account_id
    )

    if permanent:
        # Configured credential-source metadata is scoped to this account as
        # well. Remove it with the final row so a deleted account cannot be
        # offered stale source preferences if it is later recreated.
        session.exec(
            delete(LatestUsageContribution).where(
                col(LatestUsageContribution.provider_id) == provider_id,
                col(LatestUsageContribution.account_id) == account_id,
            )
        )
        session.exec(
            delete(CredentialSource).where(
                col(CredentialSource.provider_id) == provider_id,
                col(CredentialSource.account_id) == account_id,
            )
        )
        # A legacy row that was archived before this path retired the secret
        # still has it at rest — and the cache would reload it. A deleted
        # account holds no credential either.
        source_controls.retire_contributions_for_account(session, provider_id, account_id)
        session.delete(row)
    else:
        # Soft-archive instead of hard-delete (see docstring). Remove is
        # destructive to credentials, but keeps the row for easy restoration.
        row.archived = True
        row.enabled = False
        row.api_key = None
        row.session_cookie = None
        row.oai_sc_cookie = None
        session.add(row)
        # A retired account holds no credential: retire the secret this
        # account still holds at rest for it (the cache reloads every durable
        # bundle on the next revision change, so the retired account would get
        # it back without this).
        source_controls.retire_contributions_for_account(session, provider_id, account_id)
    session.commit()

    # Drop the in-memory token cache entry. The async lock is held inside
    # ``remove()``; safe to call from an async endpoint because FastAPI runs
    # the handler on a loop and the cache uses asyncio.Lock.
    await token_cache.remove(provider_id, account_id)
    auth_failures.clear(provider_id, account_id)
    if permanent:
        manager.clear_credential_source_preferences(provider_id, account_id)

    audit_log.record(
        session,
        request,
        action="provider_config.purge" if permanent else "provider_config.delete",
        target_id=f"{provider_id}/{account_id}",
        payload={"tags_cleared": tags_cleared, "permanent": permanent},
    )

    # Invalidate cached fleet/limits responses so the next dashboard poll
    # reflects the deletion immediately.
    from app.core.cache import cache_clear

    cache_clear()

    # Trigger an immediate collector sync so the just-removed account's
    # SmartCollector instance (if any) drops out of
    # ``manager.smart_collectors`` — otherwise the next poll could re-
    # write a ``LatestUsage`` card and undo the eviction above. Mirrors
    # the PUT helper's post-commit sync (the ``try/except`` swallows
    # any background error so the user's mutation still succeeds even
    # if the sync itself flakes).
    try:
        await manager.sync_collectors(force=True)
    except Exception as e:
        logger.warning(
            f"Failed to trigger sync after provider_config delete for "
            f"{scrub_log(provider_id)}/{scrub_log(account_id)}: {e}"
        )

    return {
        "status": "permanently_deleted" if permanent else "deleted",
        "provider_id": provider_id,
        "account_id": account_id,
        "tags_cleared": tags_cleared,
    }


def _has_account_usage(provider_id: str, account_id: str, session: Session) -> bool:
    return _account_usage_flags(session, [(provider_id, account_id)]).get(
        (provider_id, account_id), False
    )


def _discovery_credential_kind(rule_type: str, token_types: set[str]) -> str:
    if rule_type == "cookie":
        return "Cookie"
    if token_types & OAUTH_TOKEN_VALUE_KEYS:
        return "OAuth token"
    return "API key"


def _archived_account_usage_flags(
    session: Session, archived_rows: list[ProviderConfig]
) -> dict[tuple[str, str], bool]:
    archived_rows = [row for row in archived_rows if row.provider_id in manager.collector_registry]
    return _account_usage_flags(
        session, [(row.provider_id, row.account_id) for row in archived_rows]
    )


def _account_usage_flags(
    session: Session, account_keys: list[tuple[str, str]]
) -> dict[tuple[str, str], bool]:
    account_keys = list(dict.fromkeys(account_keys))
    usage_terms = [
        and_(
            col(UsageEvent.provider_id).in_(account_usage_provider_ids(provider_id)),
            col(UsageEvent.account_id) == account_id,
        )
        for provider_id, account_id in account_keys
    ]
    if not usage_terms:
        return {}
    usage_event_pairs = set(
        session.exec(
            select(UsageEvent.provider_id, UsageEvent.account_id)
            .where(or_(*usage_terms))
            .distinct()
        ).all()
    )
    return {
        (provider_id, account_id): any(
            (event_provider_id, account_id) in usage_event_pairs
            for event_provider_id in account_usage_provider_ids(provider_id)
        )
        for provider_id, account_id in account_keys
    }


@router.post("/provider-config/console-organizations")
@limiter.limit("20/minute")
async def discover_console_organizations(
    request: Request,
    body: _ConsoleOrganizationsRequest,
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Read Console organizations without persisting the supplied credential."""
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            organizations = await console_organizations(client, body.session_cookie)
        return {"organizations": organizations}
    except ConsoleAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


async def _validate_console_connection(
    cookie: str, organization_id: str
) -> tuple[str, BillingType]:
    """Validate membership and billing access; the UUID stays the saved identity.

    Returns the organization's name and how the account bills. The balance
    card this already reads is the evidence the detector reads — Console
    publishes prepaid credit, so the account spends.
    """
    try:
        org_id = str(UUID(organization_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Select a Console organization") from exc
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            organizations = await console_organizations(client, cookie)
            org = next((o for o in organizations if o["id"] == org_id), None)
            if org is None:
                raise HTTPException(
                    status_code=403, detail="Console organization is not accessible"
                )
            balance = await console_get(
                client, f"/api/organizations/{org_id}/prepaid/credits", cookie
            )
            card = parse_console_balance(balance)
        return org["name"], detect_billing_type([card])
    except ConsoleAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=502, detail="Invalid Console billing response") from exc


async def _detect_claude_billing_type(
    provider: str, cookie: str, token: str | None = None
) -> BillingType | None:
    """How the Claude account behind a credential bills, proven like the collectors prove it.

    The same verification the automatic path uses reads the account's quota
    windows; the detector reads the shape of the card they produce.

    ``None`` means the proof was inconclusive — several organizations are
    accessible but disagree, or Claude answered only partly. That is the absence of a
    proof, not a proof of "unknown": the operator (or the server's own
    later proof) decides, and an inconclusive answer is never stored as a
    decision.

    An OAuth token has no Claude organization to read, so it is priced from
    the usage its own strategy publishes.
    """
    from app.services.claude_verification import verify_claude_cookie

    if not cookie and token:
        # An OAuth token has no Claude organization to read: it is priced from
        # the usage its own strategy publishes.
        from app.services.claude_oauth_usage import billing_model_from_oauth_usage

        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await billing_model_from_oauth_usage(client, token)

    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            result = await verify_claude_cookie(client, provider, cookie)
    except ConsoleAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if result.billing_type and result.billing_type != UNKNOWN:
        # A model was proved. Several organizations can be reachable without
        # making the model ambiguous (every accessible organization describes
        # the same way of billing), and which organization the account is bound
        # to is the operator's choice, not the model's.
        return result.billing_type
    if result.selection_required or not result.complete:
        return None
    return result.billing_type


@router.post("/provider-config/preview-account")
@limiter.limit("30/minute")
async def preview_account_identity(
    request: Request,
    body: _AccountPreviewRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Derive the canonical ``account_id`` (and a friendly ``account_label``)
    from a credential the user is about to save. Powers the wizard's step-2
    debounced preview (PR #287).

    Returns ``{suggested_account_id, suggested_label, label_source,
    already_exists}``. On collision returns 409 with the same payload under
    ``detail`` so the wizard can render an inline error.

    ``label_source`` values:
      - ``"email"``: extracted from a JWT claim or pasted email-shaped string.
      - ``"credential_hash"``: PBKDF2-HMAC-SHA256 of the credential
        (fallback when no email is extractable; see
        ``app/services/account_identity.py:resolve_account_id``).
      - ``"default"``: neither email nor hash — falls back to the canonical
        ``"default"`` sentinel.
    """
    import re as _re

    from app.core.utils import IdentityExtractor
    from app.services.account_identity import resolve_account_id

    if body.provider_id not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {body.provider_id}")

    if body.provider_id == "anthropic_console":
        if not body.session_cookie or not body.organization_id:
            raise HTTPException(
                status_code=422, detail="Enter a Console cookie and select an organization"
            )
        try:
            organization_id = str(UUID(body.organization_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Select a Console organization") from exc
        label, console_billing_type = await _validate_console_connection(
            body.session_cookie, organization_id
        )
        already_exists = _row_exists(session, body.provider_id, organization_id)
        result = {
            "suggested_account_id": organization_id,
            "suggested_label": label,
            "label_source": "organization",
            "suggested_billing_type": console_billing_type,
            "already_exists": already_exists,
        }
        if already_exists:
            raise HTTPException(status_code=409, detail=result)
        return result

    # The operator asks for the billing model explicitly ("Detect billing
    # model"): the credential is proven against the Claude API, and the
    # account's own quota windows decide how it bills. The debounced preview
    # never asks for it — a proof costs a round trip to Claude.
    billing_type: BillingType | None = None
    if body.detect_billing:
        if body.provider_id != "anthropic":
            raise HTTPException(
                status_code=422,
                detail="Billing detection is available for Claude accounts only",
            )
        if not body.session_cookie and not body.api_key:
            raise HTTPException(
                status_code=422,
                detail="Enter a Claude web session or an OAuth token to detect the billing model",
            )
        billing_type = await _detect_claude_billing_type(
            body.provider_id, body.session_cookie or "", body.api_key
        )

    credential_hint = body.api_key or body.session_cookie
    if not credential_hint:
        # No credential typed yet — return the canonical "default" so the
        # wizard can render the preview block without a 400.
        return {
            "suggested_account_id": "default",
            "suggested_label": None,
            "label_source": "default",
            "suggested_billing_type": None,
            "already_exists": _row_exists(session, body.provider_id, "default"),
        }

    email_pattern = r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$"
    email_candidate: str | None = None
    jwt_email = IdentityExtractor.get_email_from_jwt(credential_hint)
    if jwt_email:
        email_candidate = jwt_email.lower()
    elif _re.match(email_pattern, credential_hint):
        email_candidate = credential_hint.lower()

    if email_candidate:
        already_exists = _row_exists(session, body.provider_id, email_candidate)
        if already_exists:
            raise HTTPException(
                status_code=409,
                detail={
                    "suggested_account_id": email_candidate,
                    "suggested_label": email_candidate,
                    "label_source": "email",
                    "suggested_billing_type": billing_type,
                    "already_exists": True,
                },
            )
        return {
            "suggested_account_id": email_candidate,
            "suggested_label": email_candidate,
            "label_source": "email",
            "suggested_billing_type": billing_type,
            "already_exists": False,
        }

    suggested_account_id = resolve_account_id(
        provider_id=body.provider_id,
        raw_account_id=None,
        account_label=None,
        credential_hint=credential_hint,
    )

    label_source = "credential_hash"
    if _re.match(email_pattern, suggested_account_id):
        label_source = "email"
    if suggested_account_id == "default":
        label_source = "default"

    already_exists = _row_exists(session, body.provider_id, suggested_account_id)
    if already_exists:
        raise HTTPException(
            status_code=409,
            detail={
                "suggested_account_id": suggested_account_id,
                "suggested_label": suggested_account_id if label_source == "email" else None,
                "label_source": label_source,
                "suggested_billing_type": billing_type,
                "already_exists": True,
            },
        )

    return {
        "suggested_account_id": suggested_account_id,
        "suggested_label": suggested_account_id if label_source == "email" else None,
        "label_source": label_source,
        "suggested_billing_type": billing_type,
        "already_exists": False,
    }


def _row_exists(session: Session, provider_id: str, account_id: str) -> bool:
    """Cheap EXISTS check used by the preview endpoint. Single-row
    SELECT with LIMIT 1 — faster than loading the full ProviderConfig
    row when we only need to know whether one is there."""
    from sqlmodel import func

    stmt = (
        select(func.count())
        .select_from(ProviderConfig)
        .where(
            ProviderConfig.provider_id == provider_id,
            ProviderConfig.account_id == account_id,
        )
        .limit(1)
    )
    return session.exec(stmt).one() > 0


async def _update_credential_source_preferences(
    session: Session,
    provider_id: str,
    account_id: str,
    preferences: list[_CredentialSourcePreference],
    *,
    all_machines: bool = False,
) -> dict[str, tuple[bool, int]]:
    by_id = {item.source_id: item for item in preferences}
    if len(by_id) != len(preferences):
        raise HTTPException(status_code=422, detail="Duplicate credential source id")
    known = {
        source.source_id: source
        for source in session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.account_id == account_id,
            )
        ).all()
    }
    if set(by_id) - set(known):
        raise HTTPException(status_code=404, detail="Unknown credential source")
    if set(known) - set(by_id):
        raise HTTPException(
            status_code=422,
            detail="Credential source preferences must include every known source",
        )
    sibling_groups: dict[str, list[CredentialSource]] = {}
    if all_machines:
        for source in known.values():
            if source.credential_origin and source.sidecar_id:
                sibling_groups.setdefault(source.credential_origin, []).append(source)

    applied_origins: set[str] = set()
    for item in preferences:
        source = known[item.source_id]
        siblings: Sequence[CredentialSource] = [source]
        if all_machines and source.credential_origin and source.sidecar_id:
            if source.credential_origin in applied_origins:
                continue
            applied_origins.add(source.credential_origin)
            siblings = sibling_groups[source.credential_origin]
        for sibling in siblings:
            sibling_preference = by_id.get(sibling.source_id)
            if sibling_preference is None:
                continue
            sibling.enabled = sibling_preference.enabled
            sibling.priority = sibling_preference.priority
            session.add(sibling)
    return {source_id: (item.enabled, item.priority) for source_id, item in by_id.items()}


async def _store_manual_config_source(
    session: Session, provider_id: str, account_id: str, tokens: dict[str, str]
) -> None:
    """Keep dashboard credentials in their durable and in-memory source bundle."""
    from app.services.credential_sources import touch_source

    source_id = f"config:{provider_id}:{account_id}"
    source = touch_source(
        session,
        provider_id=provider_id,
        account_id=account_id,
        source_id=source_id,
        source_type="config",
        source_label="Manual configuration",
    )
    await token_cache.store(
        provider_id,
        tokens,
        account_id=account_id,
        source="config",
        source_id=source_id,
        source_metadata={
            "source_type": "config",
            "source_label": "Manual configuration",
            "enabled": source.enabled,
            "priority": source.priority,
        },
    )


async def _drop_pasted_key_from_cache(provider_id: str, account_id: str) -> None:
    """Forget a dashboard-pasted key without disturbing sidecar-discovered credentials.

    ``token_cache.remove`` would drop the whole account — every sidecar source bundle
    included, which then sit missing until each machine's next push. Only the pasted
    key's own ``config:`` source and the merged key fields go.
    """
    await token_cache.remove_tokens(provider_id, account_id, {"api_key", "oauth_token"})
    await token_cache.remove_source(provider_id, account_id, f"config:{provider_id}:{account_id}")


async def _apply_provider_config_update(  # noqa: PLR0915 — known-debt: per-field validation + persistence, refactor tracked separately
    session: Session,
    provider_id: str,
    account_id: str,
    body: _ProviderConfigUpdate,
    *,
    detected_billing_type: BillingType | None = None,
) -> None:
    """Upsert the ``(provider_id, account_id)`` row and propagate credentials
    to the in-memory token cache under the same ``account_id``.

    Shared core of the provider-config PUT. Callers are responsible for any
    row-count / account_id guards before invoking this helper.

    ``detected_billing_type`` is a model something *proved* (the account's own
    published cards), not one a person picked. It is recorded as a detection, so
    the repair that prices the accounts predating detection never mistakes it
    for a choice.
    """
    # Only a credential write/clear earns a fresh auth verdict; a label / enabled /
    # archive-only save must not dismiss a live "provider rejected it" flag (during
    # error backoff that flag is the only signal).
    credential_changed = (
        body.api_key is not None
        or body.session_cookie is not None
        or bool(body.clear_api_key)
        or bool(body.clear_session_cookie)
    )
    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == provider_id,
            ProviderConfig.account_id == account_id,
        )
    ).first()
    if row is None:
        row = ProviderConfig(
            provider_id=provider_id,
            account_id=account_id,
            enabled=body.enabled if body.enabled is not None else True,
        )
        session.add(row)
        session.flush()

    if body.enabled is not None:
        row.enabled = body.enabled
    if body.archived is not None:
        row.archived = body.archived
        # Archiving a provider should stop collection — the user explicitly
        # chose to hide it, so no point wasting poll cycles.
        if body.archived and row.enabled:
            row.enabled = False
        # Unarchiving restores collection so the provider is usable again,
        # but only when the caller did not explicitly state enabled (the
        # UI's Restore action sends ``{"archived": false}`` alone).
        elif body.enabled is None and not body.archived and not row.enabled:
            row.enabled = True
    # PR #317 round-2 re-review warning: enforce the archive invariant AFTER
    # both field assignments. A plain ``{"enabled": true}`` PUT on an
    # archived row (exactly what the dialog's master switch sends for every
    # disabled account) used to leave the row ``archived=True,
    # enabled=True`` — re-caching its credential and respawning a collector
    # while ``archived_pairs`` kept hiding it from the fleet view (invisible
    # collection). An archived row must never be enabled implicitly; the
    # only way out is an explicit ``archived: false`` un-archive, which the
    # assignment above handles before this invariant runs.
    if row.archived and row.enabled:
        row.enabled = False
    if body.account_label is not None:
        row.account_label = body.account_label or None
    if body.poll_interval_seconds is not None:
        row.poll_interval_seconds = (
            body.poll_interval_seconds if body.poll_interval_seconds > 0 else None
        )
    if body.collection_strategies is not None:
        # None list = reset to defaults; empty list = no strategies (disabled all)
        supported = {entry.get("id") for entry in manager.get_supported_strategies(provider_id)}
        normalized = [entry for entry in body.collection_strategies if entry.get("id") in supported]
        row.strategies = normalized if normalized else None
    if body.opencode_workspace_id is not None and provider_id == "opencode":
        row.opencode_workspace_id = body.opencode_workspace_id.strip() or None
    if body.billing_type is not None:
        row.billing_type = body.billing_type
        # A person wrote this value, including a deliberate "unknown": it is
        # theirs, and a later proof never overwrites it.
        row.billing_type_source = "operator"
    elif detected_billing_type is not None:
        # Something proved this, nobody chose it: the account's own cards
        # decided. The source column says so, so the repair that prices the
        # accounts predating detection never mistakes it for a choice.
        row.billing_type = detected_billing_type
        row.billing_type_source = "detected"
    if body.clear_api_key is True:
        # Explicit-clear flag wins over any same-field write in the body
        # (the UI sends one or the other, not both). Wipe the stored encrypted
        # blob and invalidate the token-cache entry so stale creds don't linger
        # in collectors that hot-path from cache (PR #287).
        row.api_key = None
        await _drop_pasted_key_from_cache(provider_id, account_id)
    if body.api_key is not None and body.clear_api_key is not True:
        # Empty string = clear the stored key; non-empty = encrypt and store
        val = body.api_key
        if val:
            if val.lower().startswith("bearer "):
                val = val[7:].strip()
            elif "sessionKey=" in val:
                # Only truncate if it's NOT a full multi-cookie header.
                if not ("cf_clearance" in val or "__cf_bm" in val or val.count(";") > 2):
                    for part in val.split(";"):
                        part = part.strip()
                        if part.startswith("sessionKey="):
                            val = part[11:].strip()
                            break
            elif "__Secure-next-auth.session-token=" in val:
                for part in val.split(";"):
                    part = part.strip()
                    if part.startswith("__Secure-next-auth.session-token="):
                        val = part[32:].strip()
                        break

        row.api_key = val if val else None

        if not val:
            # Documented empty-string clear (the API/script path — the UI
            # sends clear_api_key) must invalidate the cache mirror too, or
            # collectors keep using the removed key until its TTL expires.
            # Same shape as the clear_api_key branch above (PR #287).
            await _drop_pasted_key_from_cache(provider_id, account_id)

        # Propagate to token_cache if this is also mapped as an OAuth token.
        # Stamp under the resolved account_id (no longer hard-coded "default")
        # so the new per-account endpoint keeps credentials and identity aligned.
        if row.api_key and provider_id in (
            "chatgpt",
            "anthropic",
            "gemini",
            "ollama",
            "kimi_coding",
            "opencode",
        ):
            tokens = {"oauth_token": row.api_key}

            # For ChatGPT, try to extract the account_id from the token if it's a JWT
            if provider_id == "chatgpt":
                from app.core.utils import IdentityExtractor

                acc_id = IdentityExtractor.get_openai_account_id_from_jwt(row.api_key)
                if acc_id:
                    tokens["account_id"] = acc_id

            # If this looks like a Claude session key or bundle, also map it to cookie slots
            if provider_id == "anthropic" and (
                "sessionKey=" in row.api_key or row.api_key.startswith("sk-ant-sid")
            ):
                tokens["session_cookie"] = row.api_key
                tokens["cookie_sessionKey"] = row.api_key

            # Ollama reads the API key under the "api_key" token-cache slot.
            if provider_id == "ollama":
                tokens["api_key"] = row.api_key

            # Kimi Coding is the same pattern: the collector resolves a
            # dashboard paste from the api_key slot (issue #343) — without
            # this mirror an account-keyed row never reaches the collector.
            if provider_id == "kimi_coding":
                tokens["api_key"] = row.api_key

            if provider_id == "opencode":
                tokens["api_key"] = row.api_key

            await _store_manual_config_source(session, provider_id, account_id, tokens)
        elif row.api_key and provider_id == "xai":
            # A dashboard paste is an access bearer, not a refresh token.
            await _store_manual_config_source(
                session, provider_id, account_id, {"xai_access": row.api_key}
            )
    oai_sc_val: str | None = None  # may be extracted from pasted cookie string below
    if body.clear_session_cookie is True:
        # Mirror of the clear_api_key path above — wipe both session_cookie
        # and the oai-sc companion (ChatGPT-only) and invalidate cache.
        row.session_cookie = None
        row.oai_sc_cookie = None
        await token_cache.remove_tokens(
            provider_id,
            account_id,
            {
                "session_cookie",
                "cookie_session",
                "cookie_sessionKey",
                "cookie___Secure-next-auth.session-token",
                "console_session",
                "cookie_oai-sc",
            },
        )
        await token_cache.remove_source_tokens(
            provider_id,
            account_id,
            f"config:{provider_id}:{account_id}",
            {
                "session_cookie",
                "cookie_session",
                "cookie_sessionKey",
                "cookie___Secure-next-auth.session-token",
                "console_session",
                "cookie_oai-sc",
            },
        )
    if body.session_cookie is not None and body.clear_session_cookie is not True:
        val = body.session_cookie
        if val and (";" in val or "=" in val):
            # Attempt to extract common session tokens from a full cookie string
            found = None
            if provider_id == "anthropic":
                # Only truncate to sessionKey if it's NOT a full multi-cookie header.
                # If there are many cookies (like Cloudflare's cf_clearance), we need the whole string.
                if "cf_clearance" in val or "__cf_bm" in val or val.count(";") > 2:
                    found = None  # Keep the whole string
                else:
                    # Extract sessionKey for a cleaner storage if it's just a couple of cookies
                    for part in val.split(";"):
                        part = part.strip()
                        if part.startswith("sessionKey="):
                            found = part[11:].strip()
                            break
            elif provider_id == "chatgpt":
                # Handle both monolithic and NextAuth.js chunked (.0 / .1) session tokens.
                # Also extract oai-sc if present — it is required by /api/auth/session.
                chunk0: str | None = None
                chunk1: str | None = None
                for part in val.split(";"):
                    part = part.strip()
                    if part.startswith("__Secure-next-auth.session-token.0="):
                        # len("__Secure-next-auth.session-token.0=") == 35
                        chunk0 = part[35:]
                    elif part.startswith("__Secure-next-auth.session-token.1="):
                        chunk1 = part[35:]
                    elif part.startswith("__Secure-next-auth.session-token="):
                        # len("__Secure-next-auth.session-token=") == 33
                        found = part[33:]
                    elif part.startswith("oai-sc="):
                        oai_sc_val = part[7:]
                if chunk0:
                    found = chunk0 + (chunk1 or "")
            elif provider_id == "opencode":
                # If the pasted string contains __Host-console_session,
                # keep the FULL string in the DB column — the split below
                # pulls both cookies out of it. Otherwise (bare auth value
                # or only the auth cookie) collapse to the bare auth value
                # for backwards compatibility.
                if "__Host-console_session=" in val:
                    found = val
                else:
                    for part in val.split(";"):
                        part = part.strip()
                        if part.startswith("auth="):
                            found = part[5:].strip()
                            break

            if found:
                val = found

        row.session_cookie = val if val else None

        # Persist oai-sc alongside session cookie (ChatGPT only)
        if provider_id == "chatgpt":
            row.oai_sc_cookie = oai_sc_val  # None clears an existing value

        # Propagate to token_cache so collectors can find it immediately.
        # Stamp under the resolved account_id so dashboard-saved credentials
        # and the collector's resolved identity stay aligned in the cache.
        if row.session_cookie and provider_id != "opencode":
            # Map generic session_cookie to all common provider-specific keys
            # to ensure the manual override works across various collector implementations.
            tokens = {
                "session_cookie": row.session_cookie,
                "cookie_session": row.session_cookie,
                "cookie_sessionKey": row.session_cookie,
                "cookie___Secure-next-auth.session-token": row.session_cookie,
            }
            if provider_id == "chatgpt" and oai_sc_val:
                tokens["cookie_oai-sc"] = oai_sc_val

            await _store_manual_config_source(session, provider_id, account_id, tokens)

    if body.archived:
        # Retire the secret the server still holds at rest for this account:
        # an enrolled machine's credential lives in a durable contribution, and
        # the cache reloads every durable bundle on the next revision change —
        # so the retired account would get it back without this. Part of the
        # archive transaction: a failed commit leaves the account and its
        # credential alone.
        source_controls.retire_contributions_for_account(session, provider_id, account_id)
    session.commit()
    # A replaced/removed credential deserves a fresh verdict: drop any stale
    # "provider rejected it" flag so the next collection re-evaluates.
    if credential_changed:
        auth_failures.clear(provider_id, account_id)
    if body.archived:
        # An archived account holds no live credential: evict its cache entry
        # the way the Remove path does. Otherwise the retired account keeps a
        # bundle that only reads "expired" and stays refreshable. Idempotent —
        # every write that asserts the archived state re-asserts it.
        #
        # This runs after the commit and after the credential mirroring above:
        # a request that archives *and* stores a key would otherwise re-cache
        # the credential it just archived.
        await token_cache.remove(provider_id, account_id)
        auth_failures.clear(provider_id, account_id)
    # Invalidate server-side fleet/limits caches so archived/restored
    # providers appear or disappear immediately on the next dashboard poll.
    from app.core.cache import cache_clear

    cache_clear()
    # Trigger immediate sync and collection to reflect changes in dashboard
    # instantly. `force=True` bypasses the 60s throttle so a disable takes
    # effect now; `collect_one` is skipped when no collector remains for
    # this provider (every account disabled / archived) so we don't refresh
    # cards for a provider the user just turned off.
    try:
        await manager.sync_collectors(force=True)
        if any(key.startswith(f"{provider_id}:") for key in manager.smart_collectors):
            await manager.collect_one(provider_id)
    except Exception as e:
        logger.warning(
            f"Failed to trigger sync after config update for {scrub_log(provider_id)}: {e}"
        )

    # Wake poller so a per-provider interval change applies on the next tick.
    from app.services.poller import poller

    poller.wake()


__all__ = [
    "_AccountPreviewRequest",
    "_ConsoleOrganizationsRequest",
    "_CredentialSourcePreference",
    "_CredentialSourcesUpdate",
    "_ProviderConfigUpdate",
    "_account_usage_flags",
    "_apply_provider_config_update",
    "_archived_account_usage_flags",
    "_detect_claude_billing_type",
    "_discovery_credential_kind",
    "_drop_pasted_key_from_cache",
    "_has_account_usage",
    "_row_exists",
    "_store_manual_config_source",
    "_supported_saved_strategies",
    "_update_credential_source_preferences",
    "_validate_console_connection",
    "delete_provider_config_for_account",
    "discover_console_organizations",
    "list_provider_configs",
    "preview_account_identity",
    "update_credential_sources",
    "upsert_provider_config_for_account",
]
_PROVIDER_ICONS: dict[str, str] = {
    "anthropic": "🟠",
    "gemini": "🔵",
    "github": "🐙",
    "chatgpt": "💬",
    "openrouter": "🚀",
    "minimax": "🤖",
    "kimi_api": "🌙",
    "kimi_coding": "🌙",
    "zai": "🌐",
    "opencode": "⚡",
    "antigravity": "🛸",
    "ollama": "🦙",
}
