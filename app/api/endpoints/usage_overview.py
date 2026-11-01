import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.api.endpoints._shared import (
    _FLEET_CACHE_KEY,
    _FLEET_CACHE_TTL,
    _USAGE_SOURCES_CACHE_KEY,
    _USAGE_SOURCES_CACHE_TTL,
    _local_period_anchors,
)

if TYPE_CHECKING:
    from app.models.db import ProviderConfig

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, func, or_
from sqlmodel import Session, col, select

from app.core.cache import cache_clear, cache_get, cache_set
from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.core.utils import is_error_card_dict, resolve_user_tz
from app.models.schemas import (
    LimitCard,
    LimitsResponse,
)
from app.services.collector_manager import manager
from app.services.event_query import (
    query_window_aggregation,
)
from app.services.queries import (
    query_cumulative_live,
)

# Window type rank for selecting the "longest" window among multiple cards.
# Higher rank = longer / more authoritative window.
WINDOW_RANK: dict[str, int] = {"monthly": 4, "weekly": 3, "daily": 2, "session": 1}

router = APIRouter()


@router.get("/sources")
@limiter.limit("30/minute")
async def get_usage_sources(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, list[str]]:
    """Return distinct sources that have contributed billable usage events.

    "Sources" here are the hosts that pushed the events (``sidecar_ids``).
    The client apps that produced them are reported separately as
    ``source_apps`` — several apps share one provider, so the provider id
    alone can't say which app spent the quota.
    """
    cached = cache_get(_USAGE_SOURCES_CACHE_KEY)
    if cached is not None:
        return cached
    from app.models.db import UsageEvent

    ids = session.exec(
        select(UsageEvent.sidecar_id)
        .where(UsageEvent.kind == "message")
        .distinct()
        .order_by(UsageEvent.sidecar_id)
    ).all()
    apps = session.exec(
        select(UsageEvent.source_app)
        .where(UsageEvent.kind == "message", col(UsageEvent.source_app).is_not(None))
        .distinct()
        .order_by(UsageEvent.source_app)
    ).all()
    result = {
        "sidecar_ids": [str(sidecar_id) for sidecar_id in ids if sidecar_id],
        "source_apps": [str(app) for app in apps if app],
    }
    cache_set(_USAGE_SOURCES_CACHE_KEY, result, _USAGE_SOURCES_CACHE_TTL)
    return result


@router.get("/limits")
@limiter.limit("10/minute")
async def fetch_all_limits(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Fetch all AI service usage limits from the LatestUsage database table."""
    from app.models.db import LatestUsage

    records = session.exec(select(LatestUsage)).all()
    results = []
    for r in records:
        try:
            results.append(json.loads(r.card_json))
        except (json.JSONDecodeError, TypeError):
            continue

    if not results:
        # Bootstrap fallback: table not yet populated
        results = await manager.collect_all()

    # Validate and serialize with None values included
    limit_cards = [LimitCard(**item) for item in results]
    response = LimitsResponse(limits=limit_cards)

    # Return dict with None values included (needed for tier field)
    return response.model_dump(exclude_none=False)


@router.get("/fleet")
@limiter.limit("30/minute")
async def fetch_fleet_view(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Fleet HUD aggregation: one Fleet Commander per (provider_id, account_id).

    For each account group:
    - critical_gauge: the LatestUsage card with the highest pct_used (or
      first card with a quota gauge if pct_used is unavailable). This is
      the spec's "Most Restrictive Wins" gauge.
    - secondary_limits: every other card in the same group, used for the
      LED row.
    - sidecar_contributions: per-sidecar token totals for the current local
      month, aggregated live from usage_events, used by the Fuel Dump bar.

    The body below is synchronous SQLite work (several `session.exec` scans
    plus per-group aggregation); it's offloaded to a worker thread via
    `asyncio.to_thread` so it doesn't block the event loop while other
    dashboard queries are in flight (they'd otherwise serialize behind it —
    FastAPI only threadpools `def` routes, never `async def` ones calling
    sync code inline).

    Response is cached for `_FLEET_CACHE_TTL` seconds — matches the client's
    own 30s poll cadence, so a full recompute only happens once per cadence
    instead of once per dashboard load/tab.
    """
    cached = cache_get(_FLEET_CACHE_KEY)
    if cached is not None:
        return cached
    result = await asyncio.to_thread(_fetch_fleet_view_sync, session)
    cache_set(_FLEET_CACHE_KEY, result, _FLEET_CACHE_TTL)
    return result


def _suppress_residual_error_cards(
    groups: dict[tuple[str, str], list[dict[str, Any]]],
) -> None:
    """Suppress residual error cards when an account group has at least one healthy card."""
    for key, gcards in list(groups.items()):
        has_healthy = any(not is_error_card_dict(c) for c in gcards)
        if has_healthy:
            groups[key] = [c for c in gcards if not is_error_card_dict(c)]


def _synthesize_passive_fleet_entries(
    session: Session,
    groups: dict[tuple[str, str], list[dict[str, Any]]],
    provider_configs: Sequence["ProviderConfig"],
    billing_types: dict[tuple[str, str], str],
) -> None:
    """Synthesize fleet entries for (provider_id, account_id) pairs that have
    ingested events but no LatestUsage card — typical case is OpenCode Free,
    where the sidecar pushes events tagged provider_id="opencode-free" but
    there's no quota-bearing card to scrape. Render as PAYG-style entries.
    Query usage_events directly (not the rollup) so the card survives a
    rollup rebuild.
    """
    from app.models.db import UsageEvent

    events_pairs = session.exec(
        select(UsageEvent.provider_id, UsageEvent.account_id)
        .where(UsageEvent.kind == "message")
        .distinct()
    ).all()
    disabled_pairs = {
        (r.provider_id, r.account_id) for r in provider_configs if not r.enabled or r.archived
    }

    # Providers that already have a non-"default" account identity in events.
    # Used to suppress stale "default" synthetics left over from before the
    # sidecar was fixed to emit the real account email.
    pids_with_real_identity = {pid for pid, aid in events_pairs if aid and aid != "default"}
    for pid, aid in events_pairs:
        if not pid:
            continue
        if (pid, aid) in disabled_pairs:
            continue  # archived or disabled provider account; do not synthesize a fleet entry
        if aid == "default" and pid in pids_with_real_identity:
            continue  # orphan from pre-fix sidecar; real-identity card covers it
        if (pid, aid) in groups:
            continue
        synthetic: dict[str, Any] = {
            "provider_id": pid,
            "account_id": aid,
            "billing_type": billing_types.get((pid, aid), "unknown"),
            "service_name": pid.replace("-", " ").title(),
            "icon": "⚡" if pid.startswith("opencode") else "✦",
            "variant": "default",
            "model_id": "",
            "window_type": "lifetime",
            "is_unlimited": True,
            "data_source": "local",
            "input_source": "sidecar",
            "account_label": aid,
        }
        # Back-fill lifetime token totals so the dashboard token card shows real
        # usage instead of "0". We query from the Unix epoch so we get every
        # ingested event for this passive provider. The provider_id/account_id
        # filters keep the scan small (only this provider's rows).
        _epoch = datetime(1970, 1, 1, tzinfo=UTC)
        _life = query_cumulative_live(session, since=_epoch, provider_id=pid, account_id=aid).get(
            (pid, aid)
        )
        if _life:
            _ti = _life["tokens_input"]
            _to = _life["tokens_output"]
            _tr = _life["tokens_reasoning"]
            _tcr = _life["tokens_cache_read"]
            _tcc = _life["tokens_cache_create"]
            synthetic["token_usage"] = {
                "input": _ti,
                "output": _to,
                "reasoning": _tr,
                "cache_read": _tcr,
                "cache_create": _tcc,
                # total = input + output + reasoning; cache excluded (matches Go card
                # convention). Frontend derives its own cache-inclusive lifetime total
                # from the per-component fields above via tokenUsageTotal().
                "total": _ti + _to + _tr,
            }
            synthetic["used_value"] = _ti + _to + _tr
            synthetic["msgs"] = _life["msgs"]
        groups[(pid, aid)] = [synthetic]


def _fetch_fleet_view_sync(session: Session) -> dict[str, Any]:
    from app.models.db import LatestUsage, ProviderConfig

    records = session.exec(select(LatestUsage)).all()
    cards: list[dict[str, Any]] = []
    for r in records:
        try:
            cards.append(json.loads(r.card_json))
        except (json.JSONDecodeError, TypeError):
            continue

    # Filter out archived and disabled providers — their latest_usage rows are
    # preserved (no deletion) but they should not appear in the active fleet view.
    provider_configs = session.exec(select(ProviderConfig)).all()
    disabled_pairs = {
        (r.provider_id, r.account_id) for r in provider_configs if not r.enabled or r.archived
    }
    # Only saved configurations express an opt-out. Providers with no config
    # rows retain their existing sidecar auto-discovery behavior.
    configured_provider_ids = {r.provider_id for r in provider_configs}
    active_config_provider_ids = {
        r.provider_id for r in provider_configs if r.enabled and not r.archived
    }
    inactive_config_provider_ids = configured_provider_ids - active_config_provider_ids
    billing_types = {
        (r.provider_id, r.account_id): r.billing_type or "unknown" for r in provider_configs
    }

    # Group cards by (provider_id, account_id)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in cards:
        pid = c.get("provider_id") or ""
        aid = c.get("account_id") or ""
        if not pid:
            continue
        if (pid, aid) in disabled_pairs:
            continue
        # A stale card under an unresolved/discovered account can outlive the
        # account that was archived. Keep its durable row for restoration and
        # history, but don't surface a collection-failure warning when the
        # operator has disabled every configured account for this provider.
        if pid in inactive_config_provider_ids and (
            c.get("stale") is True or c.get("collection_failing") is True
        ):
            continue
        groups.setdefault((pid, aid), []).append(c)

    _suppress_residual_error_cards(groups)
    _synthesize_passive_fleet_entries(
        session,
        groups,
        provider_configs,
        billing_types,
    )

    now = datetime.now(UTC)
    # Per-sidecar contribution for the user-local current month, computed live
    # from usage_events. The UTC-keyed rollup lags the local month boundary
    # (e.g. until 02:00 in Berlin summer), so a live range scan keeps the split
    # aligned with the "This period" gauge.
    month_start_utc = _local_period_anchors(resolve_user_tz(session))["month_start_utc"]
    live_month = query_cumulative_live(session, since=month_start_utc)
    contrib: dict[tuple[str, str], dict[str, dict[str, float]]] = {
        ident: bucket["by_sidecar"] for ident, bucket in live_month.items() if bucket["by_sidecar"]
    }

    fleet = []
    for (pid, aid), gcards in sorted(groups.items()):
        critical_orig = _pick_critical_card(gcards)
        sc = manager.smart_collectors.get(f"{pid}:{aid}") or manager.smart_collectors.get(
            f"{pid}:default"
        )
        if sc and sc.last_success_time:
            fetched_at = datetime.fromtimestamp(sc.last_success_time, tz=UTC).isoformat()
            next_poll_at = datetime.fromtimestamp(sc.last_success_time + sc.ttl, tz=UTC).isoformat()
            critical = dict(critical_orig)
            critical["fetched_at"] = fetched_at
            critical["next_poll_at"] = next_poll_at
            critical["cache_ttl_seconds"] = sc.ttl
        else:
            critical = critical_orig
        secondary = [c for c in gcards if c is not critical_orig]

        # Pick the longest-window default card that has a reset_at, then
        # compute a live aggregation over usage_events for that window.
        window_aggregations: dict[str, Any] = {}
        longest = _longest_window_card(gcards)
        if longest:
            raw_reset = longest.get("reset_at", "")
            try:
                reset_at = parse_iso8601_utc(raw_reset)
                window_aggregations["longest"] = query_window_aggregation(
                    session,
                    provider_id=pid,
                    account_id=aid,
                    window_type=longest["window_type"],
                    reset_at=reset_at,
                )
            except (ValueError, KeyError):
                pass  # Malformed reset_at — leave window_aggregations empty

        fleet.append(
            {
                "provider_id": pid,
                "account_id": aid,
                "billing_type": billing_types.get((pid, aid), "unknown"),
                "server_collector_available": pid in manager.collector_registry,
                "critical_gauge": critical,
                "secondary_limits": secondary,
                "sidecar_contributions": contrib.get((pid, aid), {}),
                "window_aggregations": window_aggregations,
            }
        )

    from app.services.blocked_sources import source_block_rows, source_blocks

    # Credential sources a machine found but could not read (a locked or app-bound
    # browser cookie store). The provider page reads this to say why a provider has
    # no data instead of showing an unexplained empty state.
    blocked_sources = [
        view.model_dump() for view in source_blocks(session, source_block_rows(session))
    ]

    # The accounts that were merged away, and where each of them went. A
    # merged-away account holds no credential and has no quota card, so it is
    # not a fleet entry — it would need a gauge it does not have. The fleet
    # carries the merge as a note beside the entries instead, so the operator
    # watching usage learns that "alice" became "bob" instead of watching her
    # disappear silently.
    from app.services.account_merges import merged_accounts

    return {
        "fleet": fleet,
        "generated_at": now.isoformat(),
        "blocked_sources": blocked_sources,
        "merged_accounts": [
            {
                "provider_id": merged.provider_id,
                "account_id": merged.account_id,
                "destination_account_id": merged.destination_account_id,
                "merged_at": merged.merged_at,
            }
            for merged in merged_accounts(session)
        ],
    }


def _longest_window_card(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the best window-anchor card from the provider's quota cards.

    Prefers a model-agnostic (aggregate) card so the window boundary is shared
    across all models. Falls back to model-specific cards when no aggregate card
    is emitted — e.g. Gemini, where each model has its own daily quota.

    Candidate filter: default-variant cards are always eligible; aggregate cards
    (model_id empty / "default") are eligible regardless of variant, so providers
    like Antigravity that carry pool identity in variant (e.g. variant="gemini")
    are not accidentally excluded.

    The picked card only supplies the (window_type, reset_at) anchor;
    query_window_aggregation still groups events by model_id, so the response
    carries a real by_model map regardless of which card was chosen.
    """

    def _is_aggregate(c: dict[str, Any]) -> bool:
        mid = (c.get("model_id") or "").lower()
        return not mid or mid == "default"

    candidates = [
        c
        for c in cards
        if (c.get("variant", "default") == "default" or _is_aggregate(c)) and c.get("reset_at")
    ]
    if not candidates:
        return None

    pool = [c for c in candidates if _is_aggregate(c)] or candidates
    return max(
        pool,
        key=lambda c: WINDOW_RANK.get((c.get("window_type") or "").lower(), -1),
    )


def _pick_critical_card(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Spec §5.1 'Most Restrictive Wins' — return the card most exhausted.

    Uses pct_used when available, else falls back to (used_value / limit_value).
    Cards without quota signal are deprioritized but not dropped.
    """

    def score(c: dict[str, Any]) -> float:
        pct = c.get("pct_used")
        if pct is not None:
            return float(pct)
        used = c.get("used_value")
        limit = c.get("limit_value")
        if used is not None and limit and limit > 0:
            return (used / limit) * 100.0
        # Unlimited / no-quota cards score below any real bar
        return -1.0

    return max(cards, key=score)


_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


@router.get("/archived-providers")
@limiter.limit("30/minute")
async def get_archived_providers(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Lifetime stats for archived providers — hidden from the main fleet view."""
    from app.models.db import ProviderConfig, UsageEvent

    archived_rows = session.exec(select(ProviderConfig).where(ProviderConfig.archived)).all()
    archived_pairs = {(r.provider_id, r.account_id) for r in archived_rows}
    if not archived_pairs:
        return {"archived": []}

    # Single bulk cumulative query filtered to archived pairs only.
    all_life = query_cumulative_live(session, since=_EPOCH, identity_pairs=archived_pairs)

    # Single bulk last-activity query filtered to archived pairs.
    last_events = session.exec(
        select(
            UsageEvent.provider_id,
            UsageEvent.account_id,
            func.max(UsageEvent.ts).label("last_ts"),
        )
        .where(UsageEvent.kind == "message")
        .where(
            or_(
                *[
                    and_(
                        UsageEvent.provider_id == pid,  # type: ignore[arg-type]
                        UsageEvent.account_id == aid,  # type: ignore[arg-type]
                    )
                    for pid, aid in archived_pairs
                ]
            )
        )
        .group_by(UsageEvent.provider_id, UsageEvent.account_id)
    ).all()
    last_ts_map = {(r[0], r[1]): r[2] for r in last_events}

    results: list[dict[str, Any]] = []
    for pid, aid in archived_pairs:
        life = all_life.get((pid, aid))
        last_ts = last_ts_map.get((pid, aid))
        results.append(
            {
                "provider_id": pid,
                "account_id": aid,
                "lifetime": life if life else None,
                "last_activity_ts": last_ts.isoformat() if last_ts else None,
            }
        )

    return {"archived": results}


@router.post("/reset/{provider}")
@limiter.limit("10/minute")
async def reset_provider(
    request: Request,
    provider: str,
    account_id: str | None = None,
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Reset terminal failure state for a provider.

    Admin-gated: lets a caller force a specific collector out of its error
    state on demand.
    """
    if provider not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    await manager.reset_collector(provider, account_id)
    cache_clear()  # card status changed — don't serve a stale fleet/forecast cache
    return {"status": "reset", "provider": provider, "account_id": account_id}


@router.post("/collect/{provider}")
@limiter.limit("6/minute")
async def collect_provider(
    request: Request,
    provider: str,
    account_id: str | None = None,
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Force an immediate re-collection for a specific provider.

    Admin-gated: triggers real network calls to the provider on demand.
    """
    if provider not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    cards = await manager.collect_one(provider, account_id)
    cache_clear()  # the whole point of a manual "collect now" is to see it immediately
    return {"status": "ok", "provider": provider, "cards": len(cards)}


__all__ = [
    "_fetch_fleet_view_sync",
    "_longest_window_card",
    "_pick_critical_card",
    "_suppress_residual_error_cards",
    "_synthesize_passive_fleet_entries",
    "collect_provider",
    "fetch_all_limits",
    "fetch_fleet_view",
    "get_archived_providers",
    "get_usage_sources",
    "reset_provider",
]
