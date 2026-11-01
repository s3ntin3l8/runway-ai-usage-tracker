import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.api.endpoints._shared import (
    _GLOBAL_STATS_CACHE_KEY,
    _GLOBAL_STATS_CACHE_TTL,
    _TOP_N_CACHE_TTL,
    _normalize_filter,
    _resolve_ranking_since,
)

if TYPE_CHECKING:
    pass

from fastapi import APIRouter, Depends, Query, Request
from sqlmodel import Session

from app.core.cache import cache_get, cache_set
from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.utils import resolve_user_tz
from app.models._datetime import iso_utc
from app.models.schemas import (
    GlobalStatsResponse,
    SourceAppsResponse,
    TopHostsResponse,
    TopModelsResponse,
    TopProjectsResponse,
    TopSessionsResponse,
    TopToolsResponse,
)
from app.services.event_query import (
    query_anomalies,
)
from app.services.queries import (
    query_global_stats,
    query_projects,
    query_source_apps,
    rank_top_hosts,
    rank_top_models,
    rank_top_projects,
    rank_top_sessions,
    rank_top_tools,
)

# Window type rank for selecting the "longest" window among multiple cards.
# Higher rank = longer / more authoritative window.
WINDOW_RANK: dict[str, int] = {"monthly": 4, "weekly": 3, "daily": 2, "session": 1}

router = APIRouter()


@router.get("/top-models")
@limiter.limit("30/minute")
async def get_top_models(
    request: Request,
    metric: str = Query(default="tokens", pattern="^(tokens|cost)$"),
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    exclude_cache: bool = Query(default=False),
    sidecar_id: str | None = None,
    limit: int = Query(default=15, ge=1, le=50),
    session: Session = Depends(get_session),
) -> TopModelsResponse:
    """Rank models by tokens or cost across ALL providers/accounts.

    Range resolution mirrors `/history/chart`: an explicit `since`/`until`
    (exclusive) pair wins; else the last `days`; else the current month on the
    user's local calendar (the UTC-keyed rollup lags that boundary, so this
    aggregates the event log live like the cumulative gauges do).

    Offloaded to a worker thread via `asyncio.to_thread` (see
    `fetch_fleet_view` for why) — this is a `GROUP BY` scan over usage_events.
    Response is cached (keyed by every query param) for `_TOP_N_CACHE_TTL`.
    """
    # The key carries every filter the total depends on, so a cached entry never mixes counts.
    cache_key = f"top-models:{metric}:{days}:{since}:{until}:{exclude_cache}:{sidecar_id}:{limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    def _work() -> TopModelsResponse:
        since_utc = _resolve_ranking_since(session, since, days)
        # One query, so the rows and the count they describe cannot disagree.
        ranked = rank_top_models(
            session,
            since=since_utc,
            until=parse_iso8601_utc(until) if until else None,
            metric=metric,
            exclude_cache=exclude_cache,
            sidecar_id=sidecar_id,
            limit=limit,
        )
        return TopModelsResponse.model_validate(
            {
                "models": ranked.rows,
                "metric": metric,
                "total": ranked.total,
                "limit": limit,
                "generated_at": iso_utc(datetime.now(UTC)),
            }
        )

    result = await asyncio.to_thread(_work)
    cache_set(cache_key, result, _TOP_N_CACHE_TTL)
    return result


@router.get("/source-apps")
@limiter.limit("30/minute")
async def get_source_apps(  # noqa: PLR0913, PLR0917 — known-debt: 11 query filters; collapse into a Pydantic params model in a follow-up
    request: Request,
    metric: str = Query(default="tokens", pattern="^(tokens|cost)$"),
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    exclude_cache: bool = Query(default=False),
    provider_id: str | None = None,
    account_id: str | None = None,
    sidecar_id: str | None = None,
    limit: int = Query(default=15, ge=1, le=50),
    session: Session = Depends(get_session),
) -> SourceAppsResponse:
    """Rank the client apps that produced usage events.

    ``provider_id`` names the upstream provider a request was billed to, and
    several client apps share one provider (MiniMax Code, OpenCode and a
    plain API script can all spend the same MiniMax plan), so the provider
    alone can't say which app spent the quota. This answers that.

    Range resolution mirrors `/top-models`: an explicit `since`/`until`
    (exclusive) pair wins; else the last `days`; else the current month on the
    user's local calendar.

    Offloaded to a worker thread via `asyncio.to_thread` (see
    `fetch_fleet_view` for why) — this is a `GROUP BY` scan over usage_events.
    Response is cached (keyed by every query param) for `_TOP_N_CACHE_TTL`.
    """
    cache_key = (
        f"source-apps:{metric}:{days}:{since}:{until}:{exclude_cache}:"
        f"{provider_id}:{account_id}:{sidecar_id}:{limit}"
    )
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    def _work() -> SourceAppsResponse:
        since_utc = _resolve_ranking_since(session, since, days)
        apps = query_source_apps(
            session,
            since=since_utc,
            until=parse_iso8601_utc(until) if until else None,
            metric=metric,
            exclude_cache=exclude_cache,
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
            limit=limit,
        )
        return SourceAppsResponse.model_validate(
            {"apps": apps, "metric": metric, "generated_at": iso_utc(datetime.now(UTC))}
        )

    result = await asyncio.to_thread(_work)
    cache_set(cache_key, result, _TOP_N_CACHE_TTL)
    return result


@router.get("/global-stats")
@limiter.limit("30/minute")
async def get_global_stats(
    request: Request,
    sidecar_id: str | None = None,
    exclude_cache: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> GlobalStatsResponse:
    """Global cross-provider snapshot: lifetime totals, session economics,
    cache savings, model/provider diversity, and peak day/hour.

    Offloaded to a worker thread via `asyncio.to_thread` (see
    `fetch_fleet_view` for why) — includes a full `usage_events` session-economics
    scan (no index on session_id). Response is cached for `_GLOBAL_STATS_CACHE_TTL`.
    """
    cache_key = f"{_GLOBAL_STATS_CACHE_KEY}:{sidecar_id}:{exclude_cache}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    result = await asyncio.to_thread(
        lambda: GlobalStatsResponse(
            **query_global_stats(
                session,
                tz=resolve_user_tz(session),
                sidecar_id=sidecar_id,
                exclude_cache=exclude_cache,
            )
        )
    )
    cache_set(cache_key, result, _GLOBAL_STATS_CACHE_TTL)
    return result


@router.get("/top-projects")
@limiter.limit("30/minute")
async def get_top_projects(
    request: Request,
    metric: str = Query(default="tokens", pattern="^(tokens|cost|sessions)$"),
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    exclude_cache: bool = Query(default=False),
    provider_id: str | None = Query(default=None),
    sidecar_id: str | None = None,
    limit: int = Query(default=15, ge=1, le=50),
    session: Session = Depends(get_session),
) -> TopProjectsResponse:
    """Rank projects by tokens, cost, or session count. Without `provider_id`
    the ranking spans every provider (one repo's total across all tools).

    Offloaded to a worker thread via `asyncio.to_thread` (see
    `fetch_fleet_view` for why) — this is a `GROUP BY` scan over usage_events.
    Response is cached (keyed by every query param) for `_TOP_N_CACHE_TTL`.
    """
    provider_id = _normalize_filter(provider_id)
    # The key carries every filter the total depends on, so a cached entry never mixes counts.
    cache_key = f"top-projects:{metric}:{days}:{since}:{until}:{exclude_cache}:{provider_id}:{sidecar_id}:{limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    def _work() -> TopProjectsResponse:
        # One query, so the rows and the count they describe cannot disagree.
        ranked = rank_top_projects(
            session,
            since=_resolve_ranking_since(session, since, days),
            until=parse_iso8601_utc(until) if until else None,
            metric=metric,
            exclude_cache=exclude_cache,
            provider_id=provider_id,
            sidecar_id=sidecar_id,
            limit=limit,
        )
        return TopProjectsResponse.model_validate(
            {
                "projects": ranked.rows,
                "metric": metric,
                "total": ranked.total,
                "limit": limit,
                "generated_at": iso_utc(datetime.now(UTC)),
            }
        )

    result = await asyncio.to_thread(_work)
    cache_set(cache_key, result, _TOP_N_CACHE_TTL)
    return result


@router.get("/top-sessions")
@limiter.limit("30/minute")
async def get_top_sessions(
    request: Request,
    metric: str = Query(default="tokens", pattern="^(tokens|cost)$"),
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    exclude_cache: bool = Query(default=False),
    provider_id: str | None = Query(default=None),
    sidecar_id: str | None = None,
    limit: int = Query(default=15, ge=1, le=50),
    session: Session = Depends(get_session),
) -> TopSessionsResponse:
    """Rank sessions by tokens or cost across ALL providers/accounts.

    The session analog of `/top-projects`: it answers "which session drove
    that spike" in one glance, without scoping to a single provider+account
    the way `/usage/sessions` does. Each row carries the session's project,
    cwd and pushing host (`sidecar_id`).

    Range resolution mirrors `/top-models`: an explicit `since`/`until`
    (exclusive) pair wins; else the last `days`; else the current month on the
    user's local calendar.

    Offloaded to a worker thread via `asyncio.to_thread` (see
    `fetch_fleet_view` for why) — this is a `GROUP BY` scan over usage_events.
    Response is cached (keyed by every query param) for `_TOP_N_CACHE_TTL`.
    """
    provider_id = _normalize_filter(provider_id)
    # The key carries every filter the total depends on, so a cached entry never mixes counts.
    cache_key = (
        f"top-sessions:{metric}:{days}:{since}:{until}:{exclude_cache}:"
        f"{provider_id}:{sidecar_id}:{limit}"
    )
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    def _work() -> TopSessionsResponse:
        # One query, so the rows and the count they describe cannot disagree.
        ranked = rank_top_sessions(
            session,
            since=_resolve_ranking_since(session, since, days),
            until=parse_iso8601_utc(until) if until else None,
            metric=metric,
            exclude_cache=exclude_cache,
            provider_id=provider_id,
            sidecar_id=sidecar_id,
            limit=limit,
        )
        return TopSessionsResponse.model_validate(
            {
                "sessions": ranked.rows,
                "metric": metric,
                "total": ranked.total,
                "limit": limit,
                "generated_at": iso_utc(datetime.now(UTC)),
            }
        )

    result = await asyncio.to_thread(_work)
    cache_set(cache_key, result, _TOP_N_CACHE_TTL)
    return result


@router.get("/top-hosts")
@limiter.limit("30/minute")
async def get_top_hosts(
    request: Request,
    metric: str = Query(default="tokens", pattern="^(tokens|cost)$"),
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    exclude_cache: bool = Query(default=False),
    provider_id: str | None = Query(default=None),
    sidecar_id: str | None = None,
    limit: int = Query(default=15, ge=1, le=50),
    session: Session = Depends(get_session),
) -> TopHostsResponse:
    """Rank the hosts that pushed events by tokens or cost, across ALL
    providers/accounts.

    The host analog of `/top-projects`: it answers "which machine drove that
    spike". A host is the `sidecar_id` of the events — the hostname that
    pushed them — so the ranking follows the same identity the Fleet page
    shows. Events with no pushing host are not attributable and are excluded.

    Range resolution mirrors `/top-models`: an explicit `since`/`until`
    (exclusive) pair wins; else the last `days`; else the current month on the
    user's local calendar.

    Offloaded to a worker thread via `asyncio.to_thread` (see
    `fetch_fleet_view` for why) — this is a `GROUP BY` scan over usage_events.
    Response is cached (keyed by every query param) for `_TOP_N_CACHE_TTL`.
    """
    provider_id = _normalize_filter(provider_id)
    # The key carries every filter the total depends on, so a cached entry never mixes counts.
    cache_key = (
        f"top-hosts:{metric}:{days}:{since}:{until}:{exclude_cache}:"
        f"{provider_id}:{sidecar_id}:{limit}"
    )
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    def _work() -> TopHostsResponse:
        # One query, so the rows and the count they describe cannot disagree.
        ranked = rank_top_hosts(
            session,
            since=_resolve_ranking_since(session, since, days),
            until=parse_iso8601_utc(until) if until else None,
            metric=metric,
            exclude_cache=exclude_cache,
            provider_id=provider_id,
            sidecar_id=sidecar_id,
            limit=limit,
        )
        return TopHostsResponse.model_validate(
            {
                "hosts": ranked.rows,
                "metric": metric,
                "total": ranked.total,
                "limit": limit,
                "generated_at": iso_utc(datetime.now(UTC)),
            }
        )

    result = await asyncio.to_thread(_work)
    cache_set(cache_key, result, _TOP_N_CACHE_TTL)
    return result


@router.get("/top-tools")
@limiter.limit("30/minute")
async def get_top_tools(
    request: Request,
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    provider_id: str | None = Query(default=None),
    sidecar_id: str | None = None,
    limit: int = Query(default=15, ge=1, le=50),
    session: Session = Depends(get_session),
) -> TopToolsResponse:
    """Most-used tools by invocation count (Anthropic tool_use names today).

    Offloaded to a worker thread via `asyncio.to_thread` (see
    `fetch_fleet_view` for why) — includes a `json_each` unnest over
    usage_events, inherently unindexable. Response is cached (keyed by every
    query param) for `_TOP_N_CACHE_TTL`.
    """
    provider_id = _normalize_filter(provider_id)
    # The key carries every filter the total depends on, so a cached entry never mixes counts.
    cache_key = f"top-tools:{days}:{since}:{until}:{provider_id}:{sidecar_id}:{limit}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    def _work() -> TopToolsResponse:
        # One query, so the rows and the count they describe cannot disagree.
        ranked = rank_top_tools(
            session,
            since=_resolve_ranking_since(session, since, days),
            until=parse_iso8601_utc(until) if until else None,
            provider_id=provider_id,
            sidecar_id=sidecar_id,
            limit=limit,
        )
        return TopToolsResponse.model_validate(
            {
                "tools": ranked.rows,
                "total": ranked.total,
                "limit": limit,
                "generated_at": iso_utc(datetime.now(UTC)),
            }
        )

    result = await asyncio.to_thread(_work)
    cache_set(cache_key, result, _TOP_N_CACHE_TTL)
    return result


@router.get("/projects")
@limiter.limit("30/minute")
async def get_projects(
    request: Request,
    provider_id: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    sidecar_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Distinct project labels (for the sessions filter dropdown)."""
    return {
        "projects": query_projects(
            session,
            provider_id=provider_id,
            since=parse_iso8601_utc(since) if since else None,
            until=parse_iso8601_utc(until) if until else None,
            sidecar_id=sidecar_id,
        )
    }


@router.get("/anomalies")
@limiter.limit("30/minute")
async def get_anomalies(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    sidecar_id: str | None = None,
    lookback_days: int = Query(default=30, ge=7, le=90),
    z_threshold: float = Query(default=2.0, ge=0.5),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Anomaly detection: per-(provider, account, model) token spikes vs historical mean.

    Uses z-score comparison of today's token usage against the last lookback_days
    of daily rollup history. Returns anomalies where z > z_threshold. An omitted
    or empty sidecar_id selects the all-source rollup; a non-empty ID selects
    that sidecar's rollup.
    """
    return query_anomalies(
        session,
        provider_id=provider_id,
        account_id=account_id,
        sidecar_id=sidecar_id,
        lookback_days=lookback_days,
        z_threshold=z_threshold,
    )


__all__ = [
    "get_anomalies",
    "get_global_stats",
    "get_projects",
    "get_source_apps",
    "get_top_hosts",
    "get_top_models",
    "get_top_projects",
    "get_top_sessions",
    "get_top_tools",
]
