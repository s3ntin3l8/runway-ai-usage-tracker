import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session, select

from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.utils import resolve_user_tz
from app.models._datetime import iso_utc
from app.models.schemas import (
    ForecastResponse,
    GlobalStatsResponse,
    LimitCard,
    LimitsResponse,
    TopModelsResponse,
    TopProjectsResponse,
    TopToolsResponse,
)
from app.services.collector_manager import manager
from app.services.event_query import (
    query_anomalies,
    query_chart,
    query_cost_forecast,
    query_events,
    query_heatmap,
    query_history_deltas,
    query_sessions,
    query_snapshots,
    query_window_aggregation,
    query_window_detail,
    query_window_history,
    query_windows,
)
from app.services.forecast import compute_all_forecasts
from app.services.queries import (
    count_events,
    event_time_range,
    query_cumulative_live,
    query_global_stats,
    query_projects,
    query_sessions_paginated,
    query_top_models,
    query_top_projects,
    query_top_tools,
)

# Window type rank for selecting the "longest" window among multiple cards.
# Higher rank = longer / more authoritative window.
WINDOW_RANK: dict[str, int] = {"monthly": 4, "weekly": 3, "daily": 2, "session": 1}

router = APIRouter()


def _local_period_anchors(tz: ZoneInfo, *, now: datetime | None = None) -> dict[str, Any]:
    """Current month/year boundaries in `tz`, as UTC instants + key strings.

    The "This period" / "Yearly" cumulative gauges reset on the user's local
    calendar, so they are anchored at local-midnight period starts converted
    back to UTC instants for the event range scan. `now` (a tz-aware instant)
    is injectable for deterministic tests; it defaults to the current time.
    """
    now_local = (now or datetime.now(UTC)).astimezone(tz)
    month_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_local = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "month_start_utc": month_local.astimezone(UTC),
        "year_start_utc": year_local.astimezone(UTC),
        "current_month": now_local.strftime("%Y-%m"),
        "current_year": now_local.strftime("%Y"),
    }


_MONTH_KEY_RE = re.compile(r"^\d{4}-\d{2}$")


def _month_bounds_utc(tz: ZoneInfo, period_key: str) -> tuple[datetime, datetime]:
    """[start, end) UTC instants for a 'YYYY-MM' month on the user's local calendar.

    Mirrors the frontend's startOfMonthISO/endOfMonthISO so historical month
    drill-downs share the same tz-correct boundaries the live current-month
    gauge uses — the UTC-keyed rollup can land on the wrong calendar month for
    users far from UTC.
    """
    year, month = (int(part) for part in period_key.split("-"))
    start_local = datetime(year, month, 1, tzinfo=tz)
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end_local = datetime(end_year, end_month, 1, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _resolve_ranking_since(session: Session, since: str | None, days: float | None) -> datetime:
    """Lower bound for the Top-* rankings: explicit `since` wins, else `days`
    back from now, else the start of the current month on the user's calendar
    (the UTC rollup lags that boundary, so these scan events live)."""
    if since:
        return parse_iso8601_utc(since)
    if days is not None:
        return datetime.now(UTC) - timedelta(days=days)
    return _local_period_anchors(resolve_user_tz(session))["month_start_utc"]


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
    """
    from app.models.db import LatestUsage

    records = session.exec(select(LatestUsage)).all()
    cards: list[dict[str, Any]] = []
    for r in records:
        try:
            cards.append(json.loads(r.card_json))
        except (json.JSONDecodeError, TypeError):
            continue

    # Group cards by (provider_id, account_id)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in cards:
        pid = c.get("provider_id") or ""
        aid = c.get("account_id") or ""
        if not pid:
            continue
        groups.setdefault((pid, aid), []).append(c)

    # Synthesize fleet entries for (provider_id, account_id) pairs that have
    # ingested events but no LatestUsage card — typical case is OpenCode Free,
    # where the sidecar pushes events tagged provider_id="opencode-free" but
    # there's no quota-bearing card to scrape. Render as PAYG-style entries.
    # Query usage_events directly (not the rollup) so the card survives a
    # rollup rebuild.
    from app.models.db import UsageEvent

    events_pairs = session.exec(
        select(UsageEvent.provider_id, UsageEvent.account_id)
        .where(UsageEvent.kind == "message")
        .distinct()
    ).all()
    # Providers that already have a non-"default" account identity in events.
    # Used to suppress stale "default" synthetics left over from before the
    # sidecar was fixed to emit the real account email.
    pids_with_real_identity = {pid for pid, aid in events_pairs if aid and aid != "default"}
    for pid, aid in events_pairs:
        if not pid:
            continue
        if aid == "default" and pid in pids_with_real_identity:
            continue  # orphan from pre-fix sidecar; real-identity card covers it
        if (pid, aid) in groups:
            continue
        synthetic = {
            "provider_id": pid,
            "account_id": aid,
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
        groups[(pid, aid)] = [synthetic]

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
                "critical_gauge": critical,
                "secondary_limits": secondary,
                "sidecar_contributions": contrib.get((pid, aid), {}),
                "window_aggregations": window_aggregations,
            }
        )

    return {"fleet": fleet, "generated_at": now.isoformat()}


def _longest_window_card(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the default-variant card with the highest-ranking window that has reset_at.

    Prefers a model-agnostic card (model_id empty / "default") so the window's
    boundary is shared across all models. Falls back to model-specific cards
    when no aggregate card is emitted — this is the Gemini case, where each
    model has its own daily quota and no overall card exists. The picked card
    only supplies the (window_type, reset_at) anchor; query_window_aggregation
    still groups events by model_id, so the response carries a real by_model
    map regardless of which card was chosen.
    """
    candidates = [
        c for c in cards if c.get("variant", "default") == "default" and c.get("reset_at")
    ]
    if not candidates:
        return None

    def _is_aggregate(c: dict[str, Any]) -> bool:
        mid = (c.get("model_id") or "").lower()
        return not mid or mid == "default"

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


@router.get("/cumulative")
@limiter.limit("30/minute")
async def get_cumulative_usage(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    period_type: str | None = None,
    period_key: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Authoritative cumulative usage rolled up across sidecars from usage_period_rollup.

    Default: returns one entry per (provider_id, account_id) with
    `lifetime`, current-year, and current-month totals, plus per-model and
    per-sidecar breakdowns (matches spec §8.6).

    Query params narrow the scope:
    - provider_id / account_id: identity filter
    - period_type: one of 'lifetime' | 'year' | 'month'
    - period_key: a specific bucket (e.g. '2026', '2026-05', 'all')

    Requesting a specific month (period_type='month', period_key='YYYY-MM')
    takes a tz-correct path: the month bucket is aggregated live from
    usage_events on the user's local-calendar boundaries (see
    `_month_bounds_utc`), matching the live current-month gauge. The UTC-keyed
    rollup is intentionally bypassed here because its month boundaries can land
    on the wrong calendar month for users far from UTC. `current_month_key`
    points at the requested month so callers read it the same way as the
    default response.
    """
    from app.models.db import UsagePeriodRollup

    now = datetime.now(UTC)
    is_default = period_type is None and period_key is None
    is_month_live = (
        period_type == "month" and bool(period_key) and bool(_MONTH_KEY_RE.match(period_key or ""))
    )

    stmt = select(UsagePeriodRollup)
    if provider_id:
        stmt = stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsagePeriodRollup.account_id == account_id)
    if period_type:
        stmt = stmt.where(UsagePeriodRollup.period_type == period_type)
    if period_key:
        stmt = stmt.where(UsagePeriodRollup.period_key == period_key)
    # The tz-correct month path ignores the rollup rows in favour of a live
    # aggregation, so don't bother fetching them.
    rows = [] if is_month_live else session.exec(stmt).all()

    # The default (unfiltered) call powers the live "This period" / "Yearly"
    # gauges, which reset on the user's local calendar. Anchor those two
    # buckets at local-tz period starts and compute them live from events;
    # lifetime + historical drill-downs stay on the UTC rollup.
    month_live: dict[tuple[str, str], dict[str, Any]] = {}
    if is_default:
        anchors = _local_period_anchors(resolve_user_tz(session))
        current_year = anchors["current_year"]
        current_month = anchors["current_month"]
        live_month = query_cumulative_live(session, since=anchors["month_start_utc"])
        live_year = query_cumulative_live(session, since=anchors["year_start_utc"])
    elif is_month_live:
        since_utc, until_utc = _month_bounds_utc(resolve_user_tz(session), period_key or "")
        month_live = query_cumulative_live(
            session,
            since=since_utc,
            until=until_utc,
            provider_id=provider_id,
            account_id=account_id,
        )
        current_year = (period_key or "")[:4]
        current_month = period_key or ""
        live_month = {}
        live_year = {}
    else:
        # Filtered (historical drill-down) path: serve the rollup as-is. The
        # returned current_*_key here are UTC-based and only meaningful on the
        # default call — no consumer reads them on a filtered request.
        current_year = now.strftime("%Y")
        current_month = now.strftime("%Y-%m")
        live_month = {}
        live_year = {}

    # Group by (provider_id, account_id) → bucket label → totals + by_model + by_sidecar
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for r in rows:
        ident = (r.provider_id, r.account_id)
        bucket_key = _cumulative_bucket_label(r.period_type, r.period_key)
        bucket = grouped.setdefault(ident, {}).setdefault(
            bucket_key,
            {
                "tokens_input": 0,
                "tokens_output": 0,
                "tokens_cache_read": 0,
                "tokens_cache_create": 0,
                "tokens_reasoning": 0,
                "cost_usd": 0.0,
                "cost_cache": 0.0,
                "msgs": 0,
                "by_model": {},
                "by_sidecar": {},
            },
        )
        # Cache portion of cost (cache_read + cache_create), for exclude-cache views.
        cost_cache = r.cost_cache_read + r.cost_cache_create
        if r.model_id == "" and r.sidecar_id == "":
            # Top-level totals row
            bucket["tokens_input"] = r.tokens_input
            bucket["tokens_output"] = r.tokens_output
            bucket["tokens_cache_read"] = r.tokens_cache_read
            bucket["tokens_cache_create"] = r.tokens_cache_create
            bucket["tokens_reasoning"] = r.tokens_reasoning
            bucket["cost_usd"] = r.cost_usd
            bucket["cost_cache"] = cost_cache
            bucket["msgs"] = r.msgs
        elif r.model_id != "" and r.sidecar_id == "":
            # Per-model grain (all sidecars combined)
            bucket["by_model"][r.model_id] = {
                "tokens_input": r.tokens_input,
                "tokens_output": r.tokens_output,
                "tokens_cache_read": r.tokens_cache_read,
                "tokens_cache_create": r.tokens_cache_create,
                "tokens_reasoning": r.tokens_reasoning,
                "cost_usd": r.cost_usd,
                "cost_cache": cost_cache,
                "msgs": r.msgs,
            }
        elif r.model_id == "" and r.sidecar_id != "":
            # Per-sidecar grain (all models combined)
            bucket["by_sidecar"][r.sidecar_id] = {
                "tokens_input": r.tokens_input,
                "tokens_output": r.tokens_output,
                "tokens_cache_read": r.tokens_cache_read,
                "tokens_cache_create": r.tokens_cache_create,
                "tokens_reasoning": r.tokens_reasoning,
                "cost_usd": r.cost_usd,
                "cost_cache": cost_cache,
                "msgs": r.msgs,
            }
        # Cross-product rows (model_id != '' AND sidecar_id != '') are skipped —
        # those are for detailed analytics, not needed at this endpoint.

    # Stable shape: every entry always exposes lifetime + current year + current month
    # (filled with zero-value buckets when absent).
    month_key_out = f"month_{current_month}"
    year_key_out = f"year_{current_year}"

    def _empty_bucket() -> dict[str, Any]:
        return {
            "tokens_input": 0,
            "tokens_output": 0,
            "tokens_cache_read": 0,
            "tokens_cache_create": 0,
            "tokens_reasoning": 0,
            "cost_usd": 0.0,
            "cost_cache": 0.0,
            "msgs": 0,
            "by_model": {},
            "by_sidecar": {},
        }

    # Union identities from the rollup and the live windows so an account that
    # only has events in the current period (not yet in any historical rollup
    # bucket beyond lifetime) still gets an entry.
    idents = set(grouped) | set(live_month) | set(live_year) | set(month_live)
    cumulative = []
    for pid, aid in sorted(idents):
        buckets = grouped.get((pid, aid), {})
        entry: dict[str, Any] = {"provider_id": pid, "account_id": aid}
        entry["lifetime"] = buckets.get("lifetime", _empty_bucket())
        if is_default:
            entry[year_key_out] = live_year.get((pid, aid), _empty_bucket())
            entry[month_key_out] = live_month.get((pid, aid), _empty_bucket())
        elif is_month_live:
            # tz-correct historical month from the live aggregation; the rollup
            # year bucket isn't fetched on this path, so report it empty.
            entry[year_key_out] = _empty_bucket()
            entry[month_key_out] = month_live.get((pid, aid), _empty_bucket())
        else:
            entry[year_key_out] = buckets.get(year_key_out, _empty_bucket())
            entry[month_key_out] = buckets.get(month_key_out, _empty_bucket())
        # Surface any additional historical buckets the caller didn't filter out
        for k, v in buckets.items():
            if k not in entry:
                entry[k] = v
        cumulative.append(entry)

    return {
        "cumulative": cumulative,
        "current_month_key": month_key_out,
        "current_year_key": year_key_out,
        "generated_at": now.isoformat(),
    }


def _cumulative_bucket_label(period_type: str, period_key: str) -> str:
    """Map (period_type, period_key) into a stable response field name."""
    if period_type == "lifetime":
        return "lifetime"
    return f"{period_type}_{period_key}"


@router.get("/forecast")
@limiter.limit("30/minute")
async def get_usage_forecast(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    window_type: str | None = None,
    include_series: bool = False,
    session: Session = Depends(get_session),
) -> ForecastResponse:
    """Project quota usage to reset time using linear extrapolation in the current window.

    `include_series=true` populates each entry's `series` field with the
    cumulative-pct bucket trajectory for client-side drill-down rendering.
    """
    from app.models.db import LatestUsage
    from app.services.forecast import compute_forecast

    # Push known filters into SQL so we don't materialise the whole table
    # when the caller only wants one card's forecast.
    stmt = select(LatestUsage)
    if provider_id:
        stmt = stmt.where(LatestUsage.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(LatestUsage.account_id == account_id)
    if window_type:
        stmt = stmt.where(LatestUsage.window_type == window_type)

    records = session.exec(stmt).all()
    results = []
    for r in records:
        try:
            results.append(json.loads(r.card_json))
        except (json.JSONDecodeError, TypeError):
            continue

    if not results:
        # Bootstrap fallback — only when the caller asked for everything.
        if not (provider_id or account_id or window_type):
            results = await manager.collect_all()

    cards = [LimitCard(**item) for item in results]

    if include_series:
        # Drill-down path: skip batch optimization, compute per-card with series.
        now = datetime.now(UTC)
        forecasts = []
        summary: dict[str, int] = {}
        for card in cards:
            entry = compute_forecast(card, session, now=now, include_series=True)
            if entry is not None:
                forecasts.append(entry)
                summary[entry.status] = summary.get(entry.status, 0) + 1
        return ForecastResponse(forecasts=forecasts, summary=summary, generated_at=now.isoformat())

    return compute_all_forecasts(cards, session)


@router.get("/history/windows")
@limiter.limit("60/minute")
async def get_history_windows(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=30.0, ge=0.01, le=365.0),
    window_type: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Paginated list of quota windows (closed + open), newest first."""
    return query_windows(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
        window_type=window_type,
        page=page,
        limit=limit,
    )


@router.get("/history/snapshots")
@limiter.limit("60/minute")
async def get_history_snapshots(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=7.0, ge=0.01, le=365.0),
    window_type: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Flat paginated snapshot rows with per-series delta, newest first.

    `provider_id` accepts a single value (`?provider_id=claude`) or a
    comma-separated list (`?provider_id=claude,chatgpt`). Multi-value form
    lets the server paginate the filtered set so pages aren't sparse.
    """
    provider_ids: list[str] | None = None
    if provider_id:
        provider_ids = [p.strip() for p in provider_id.split(",") if p.strip()] or None
    return query_snapshots(
        session,
        provider_ids=provider_ids,
        account_id=account_id,
        days=days,
        window_type=window_type,
        page=page,
        limit=limit,
    )


@router.get("/history/chart")
@limiter.limit("30/minute")
async def get_history_chart(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=30.0, ge=0.01, le=365.0),
    metric: str = Query(default="percent", pattern="^(percent|tokens|cost)$"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Chart data: percent → fill curves; tokens/cost → daily bars.

    Defaults to the last `days`; an explicit `since`/`until` (exclusive) pair
    scopes the chart to a closed period (e.g. a selected past month).
    """
    return query_chart(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
        metric=metric,
        since=parse_iso8601_utc(since) if since else None,
        until=parse_iso8601_utc(until) if until else None,
    )


@router.get("/top-models")
@limiter.limit("30/minute")
async def get_top_models(
    request: Request,
    metric: str = Query(default="tokens", pattern="^(tokens|cost)$"),
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    exclude_cache: bool = Query(default=False),
    limit: int = Query(default=15, ge=1, le=50),
    session: Session = Depends(get_session),
) -> TopModelsResponse:
    """Rank models by tokens or cost across ALL providers/accounts.

    Range resolution mirrors `/history/chart`: an explicit `since`/`until`
    (exclusive) pair wins; else the last `days`; else the current month on the
    user's local calendar (the UTC-keyed rollup lags that boundary, so this
    aggregates the event log live like the cumulative gauges do).
    """
    since_utc = _resolve_ranking_since(session, since, days)

    models = query_top_models(
        session,
        since=since_utc,
        until=parse_iso8601_utc(until) if until else None,
        metric=metric,
        exclude_cache=exclude_cache,
        limit=limit,
    )
    return TopModelsResponse.model_validate(
        {"models": models, "metric": metric, "generated_at": iso_utc(datetime.now(UTC))}
    )


@router.get("/global-stats")
@limiter.limit("30/minute")
async def get_global_stats(
    request: Request,
    session: Session = Depends(get_session),
) -> GlobalStatsResponse:
    """Global cross-provider snapshot: lifetime totals, session economics,
    cache savings, model/provider diversity, and peak day/hour."""
    return GlobalStatsResponse(**query_global_stats(session, tz=resolve_user_tz(session)))


@router.get("/history/window-detail")
@limiter.limit("30/minute")
async def get_history_window_detail(
    request: Request,
    provider_id: str,
    account_id: str,
    window_type: str,
    window_start: str,
    window_end: str,
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Fill-up series and by-model breakdown for one expanded window."""
    try:
        # `.replace(" ", "+")` undoes URL decoding that strips the `+` from
        # timezone offsets in query strings. Helper can't apply this safely.
        ws = parse_iso8601_utc(window_start.replace(" ", "+"))
        we = parse_iso8601_utc(window_end.replace(" ", "+"))
    except ValueError:
        raise HTTPException(status_code=422, detail="window_start/window_end must be ISO datetime")
    return query_window_detail(
        session,
        provider_id=provider_id,
        account_id=account_id,
        window_type=window_type,
        window_start=ws,
        window_end=we,
        days=days,
    )


@router.get("/history/deltas")
@limiter.limit("30/minute")
async def get_usage_history_deltas(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=1.0, ge=0.01, le=90.0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Compute actual consumption deltas from usage_events.

    Returns token_delta_total, cost_delta_total, provider_token_deltas,
    critical_series_count, and series_sampled. Since this uses event-sourced
    data (not gauge readings), no glitch filtering is needed.
    """
    return query_history_deltas(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
    )


@router.get("/events")
@limiter.limit("30/minute")
async def get_usage_events(  # noqa: PLR0913 — known-debt: 12 query filters; collapse into a Pydantic params model in a follow-up
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    model_id: str | None = Query(default=None),
    sidecar_id: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="desc"),
    include_raw: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Recent event tail for a (provider_id, account_id) pair.

    Supports filtering by time range, model, sidecar, and kind, plus
    offset/limit pagination. Returns events newest-first by default. ``total``
    is the full count of matching rows (not the page size), so callers can page.
    raw_json is excluded unless include_raw=true. Use kind=error to retrieve
    only provider failure events.
    """
    since_dt: datetime | None = None
    until_dt: datetime | None = None
    if since:
        since_dt = parse_iso8601_utc(since)
    if until:
        until_dt = parse_iso8601_utc(until)

    events = query_events(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since_dt,
        until=until_dt,
        model_id=model_id,
        sidecar_id=sidecar_id,
        kind=kind,
        limit=limit,
        offset=offset,
        order=order,
    )

    total = count_events(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since_dt,
        until=until_dt,
        model_id=model_id,
        sidecar_id=sidecar_id,
        kind=kind,
    )

    rows = []
    for ev in events:
        d = ev.model_dump()
        if not include_raw:
            d.pop("raw_json", None)
        rows.append(d)

    return {"events": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/events/range")
@limiter.limit("30/minute")
async def get_usage_events_range(
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    session: Session = Depends(get_session),
) -> dict[str, str | None]:
    """Earliest/latest event timestamps for a (provider_id, account_id) pair.

    Bounds the month selector — there's no point paging back before the first
    recorded event. Both fields are null when the pair has no events yet.
    """
    earliest, latest = event_time_range(session, provider_id=provider_id, account_id=account_id)
    return {"earliest": iso_utc(earliest), "latest": iso_utc(latest)}


@router.get("/window-history")
@limiter.limit("30/minute")
async def get_window_history(
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    window_type: str = Query(...),
    limit: int = Query(default=12, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Closed-window history with per-model and per-sidecar splits.

    Returns up to N most recent closed windows for the given
    (provider_id, account_id, window_type) triple, ordered newest-first.
    """
    windows = query_window_history(
        session,
        provider_id=provider_id,
        account_id=account_id,
        window_type=window_type,
        limit=limit,
    )
    return {"windows": windows}


@router.get("/heatmap")
@limiter.limit("30/minute")
async def get_usage_heatmap(
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    days: int = Query(default=14, ge=1, le=90),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    tz: str | None = Query(default=None, description="IANA timezone (e.g. Europe/Berlin)"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """7×24 hour-of-day token activity grid.

    Defaults to the last N `days`; an explicit `since`/`until` (exclusive) pair
    scopes the grid to a closed period (e.g. a selected past month) instead.
    Always returns all 168 cells (7 days × 24 hours). Cells with no events
    have tokens=0. dow follows SQLite convention: 0=Sunday … 6=Saturday.

    When `tz` is a valid IANA name, events are bucketed in that zone (so the
    grid reflects local hour-of-day). Invalid or missing `tz` falls back to
    UTC bucketing.
    """
    since_dt = parse_iso8601_utc(since) if since else None
    until_dt = parse_iso8601_utc(until) if until else None
    cells = query_heatmap(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
        since=since_dt,
        until=until_dt,
        tz=tz,
    )
    return {"cells": cells, "tz": tz or "UTC"}


@router.get("/sessions")
@limiter.limit("30/minute")
async def get_usage_sessions(
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    sort_by: Literal["tokens", "recent"] = Query(default="tokens"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Top-N sessions within the requested time window.

    Defaults to the last 7 days when 'since' is not provided. An optional
    'until' (exclusive) upper bound scopes the window to a closed period (e.g.
    a selected past month). Events without a session_id are excluded.
    sort_by='tokens' (default) orders by token total desc; 'recent' orders by ts_end desc.
    """
    since_dt: datetime | None = None
    until_dt: datetime | None = None
    if since:
        since_dt = parse_iso8601_utc(since)
    if until:
        until_dt = parse_iso8601_utc(until)

    sessions = query_sessions(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since_dt,
        until=until_dt,
        limit=limit,
        sort_by=sort_by,
    )
    return {"sessions": sessions}


@router.get("/sessions/paginated")
@limiter.limit("30/minute")
async def get_usage_sessions_paginated(
    request: Request,
    provider_id: str = Query(...),
    account_id: str = Query(...),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    project: str | None = Query(default=None),
    page: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=50),
    sort_by: Literal["tokens", "recent"] = Query(default="recent"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """One page of sessions (default 25) plus the total count, for the Sessions
    browser tab. Optional `project` filters to one working directory."""
    return query_sessions_paginated(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=parse_iso8601_utc(since) if since else None,
        until=parse_iso8601_utc(until) if until else None,
        page=page,
        page_size=limit,
        sort_by=sort_by,
        project=project,
    )


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
    limit: int = Query(default=15, ge=1, le=50),
    session: Session = Depends(get_session),
) -> TopProjectsResponse:
    """Rank projects by tokens, cost, or session count. Without `provider_id`
    the ranking spans every provider (one repo's total across all tools)."""
    projects = query_top_projects(
        session,
        since=_resolve_ranking_since(session, since, days),
        until=parse_iso8601_utc(until) if until else None,
        metric=metric,
        exclude_cache=exclude_cache,
        provider_id=provider_id,
        limit=limit,
    )
    return TopProjectsResponse.model_validate(
        {"projects": projects, "metric": metric, "generated_at": iso_utc(datetime.now(UTC))}
    )


@router.get("/top-tools")
@limiter.limit("30/minute")
async def get_top_tools(
    request: Request,
    days: float | None = Query(default=None, ge=0.01, le=365.0),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    provider_id: str | None = Query(default=None),
    limit: int = Query(default=15, ge=1, le=50),
    session: Session = Depends(get_session),
) -> TopToolsResponse:
    """Most-used tools by invocation count (Anthropic tool_use names today)."""
    tools = query_top_tools(
        session,
        since=_resolve_ranking_since(session, since, days),
        until=parse_iso8601_utc(until) if until else None,
        provider_id=provider_id,
        limit=limit,
    )
    return TopToolsResponse.model_validate(
        {"tools": tools, "generated_at": iso_utc(datetime.now(UTC))}
    )


@router.get("/projects")
@limiter.limit("30/minute")
async def get_projects(
    request: Request,
    provider_id: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Distinct project labels (for the sessions filter dropdown)."""
    return {
        "projects": query_projects(
            session,
            provider_id=provider_id,
            since=parse_iso8601_utc(since) if since else None,
            until=parse_iso8601_utc(until) if until else None,
        )
    }


@router.post("/reset/{provider}")
@limiter.limit("10/minute")
async def reset_provider(
    request: Request, provider: str, account_id: str | None = None
) -> dict[str, Any]:
    """Reset terminal failure state for a provider."""
    if provider not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    await manager.reset_collector(provider, account_id)
    return {"status": "reset", "provider": provider, "account_id": account_id}


@router.post("/collect/{provider}")
@limiter.limit("6/minute")
async def collect_provider(
    request: Request, provider: str, account_id: str | None = None
) -> dict[str, Any]:
    """Force an immediate re-collection for a specific provider."""
    if provider not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    cards = await manager.collect_one(provider, account_id)
    return {"status": "ok", "provider": provider, "cards": len(cards)}


@router.get("/cost-forecast")
@limiter.limit("30/minute")
async def get_cost_forecast(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Month-to-date cost + 7-day burn extrapolation to end of month.

    Combines current MTD from period_type=month rollups with the average
    daily cost over the last 7 days to project end-of-month spend.
    Optionally filtered by provider_id and/or account_id.
    """
    return query_cost_forecast(
        session,
        provider_id=provider_id,
        account_id=account_id,
    )


@router.get("/anomalies")
@limiter.limit("30/minute")
async def get_anomalies(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    lookback_days: int = Query(default=30, ge=7, le=90),
    z_threshold: float = Query(default=2.0, ge=0.5),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Anomaly detection: per-(provider, account, model) token spikes vs historical mean.

    Uses z-score comparison of today's token usage against the last lookback_days
    of daily rollup history. Returns anomalies where z > z_threshold.
    """
    return query_anomalies(
        session,
        provider_id=provider_id,
        account_id=account_id,
        lookback_days=lookback_days,
        z_threshold=z_threshold,
    )
