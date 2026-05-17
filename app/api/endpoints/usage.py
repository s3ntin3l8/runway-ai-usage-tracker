import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session, select

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.models.schemas import ForecastResponse, LimitCard, LimitsResponse
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

# Window type rank for selecting the "longest" window among multiple cards.
# Higher rank = longer / more authoritative window.
WINDOW_RANK: dict[str, int] = {"monthly": 4, "weekly": 3, "daily": 2, "session": 1}

router = APIRouter()


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
    - sidecar_contributions: per-sidecar token totals from usage_period_rollup
      (current month, all-models grain), used by the Fuel Dump bar.
    """
    from app.models.db import LatestUsage, UsagePeriodRollup

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
    # Per-sidecar contribution lookup from usage_period_rollup (current month)
    month_key = now.strftime("%Y-%m")
    contrib_rows = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "month",
            UsagePeriodRollup.period_key == month_key,
            UsagePeriodRollup.model_id == "",  # all-models grain
            UsagePeriodRollup.sidecar_id != "",  # only per-sidecar rows
        )
    ).all()
    contrib: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for cr in contrib_rows:
        ident = (cr.provider_id, cr.account_id)
        sb = contrib.setdefault(ident, {}).setdefault(cr.sidecar_id, {})
        sb["tokens_input"] = cr.tokens_input
        sb["tokens_output"] = cr.tokens_output
        sb["tokens_cache_read"] = cr.tokens_cache_read
        sb["tokens_cache_create"] = cr.tokens_cache_create
        sb["tokens_reasoning"] = cr.tokens_reasoning
        sb["cost_usd"] = cr.cost_usd
        sb["msgs"] = cr.msgs

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
                reset_at = datetime.fromisoformat(raw_reset.replace("Z", "+00:00"))
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
    """
    from app.models.db import UsagePeriodRollup

    stmt = select(UsagePeriodRollup)
    if provider_id:
        stmt = stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsagePeriodRollup.account_id == account_id)
    if period_type:
        stmt = stmt.where(UsagePeriodRollup.period_type == period_type)
    if period_key:
        stmt = stmt.where(UsagePeriodRollup.period_key == period_key)
    rows = session.exec(stmt).all()

    now = datetime.now(UTC)
    current_year = now.strftime("%Y")
    current_month = now.strftime("%Y-%m")

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
                "msgs": 0,
                "by_model": {},
                "by_sidecar": {},
            },
        )
        if r.model_id == "" and r.sidecar_id == "":
            # Top-level totals row
            bucket["tokens_input"] = r.tokens_input
            bucket["tokens_output"] = r.tokens_output
            bucket["tokens_cache_read"] = r.tokens_cache_read
            bucket["tokens_cache_create"] = r.tokens_cache_create
            bucket["tokens_reasoning"] = r.tokens_reasoning
            bucket["cost_usd"] = r.cost_usd
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
                "msgs": r.msgs,
            }
        # Cross-product rows (model_id != '' AND sidecar_id != '') are skipped —
        # those are for detailed analytics, not needed at this endpoint.

    # Stable shape: every entry always exposes lifetime + current year + current month
    # (filled with zero-value buckets when absent from the rollup table).
    expected_keys = ["lifetime", f"year_{current_year}", f"month_{current_month}"]

    def _empty_bucket() -> dict[str, Any]:
        return {
            "tokens_input": 0,
            "tokens_output": 0,
            "tokens_cache_read": 0,
            "tokens_cache_create": 0,
            "tokens_reasoning": 0,
            "cost_usd": 0.0,
            "msgs": 0,
            "by_model": {},
            "by_sidecar": {},
        }

    cumulative = []
    for (pid, aid), buckets in sorted(grouped.items()):
        entry: dict[str, Any] = {"provider_id": pid, "account_id": aid}
        for k in expected_keys:
            entry[k] = buckets.get(k, _empty_bucket())
        # Surface any additional historical buckets the caller didn't filter out
        for k, v in buckets.items():
            if k not in entry:
                entry[k] = v
        cumulative.append(entry)

    return {"cumulative": cumulative, "generated_at": now.isoformat()}


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
    session: Session = Depends(get_session),
) -> ForecastResponse:
    """Project quota usage to reset time using linear extrapolation in the current window."""
    from app.models.db import LatestUsage

    records = session.exec(select(LatestUsage)).all()
    results = []
    for r in records:
        try:
            results.append(json.loads(r.card_json))
        except (json.JSONDecodeError, TypeError):
            continue

    if not results:
        # Bootstrap fallback
        results = await manager.collect_all()

    cards = [LimitCard(**item) for item in results]

    if provider_id:
        cards = [c for c in cards if c.provider_id == provider_id]
    if account_id:
        cards = [c for c in cards if c.account_id == account_id]
    if window_type:
        cards = [c for c in cards if c.window_type == window_type]

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
    """Flat paginated snapshot rows with per-series delta, newest first."""
    return query_snapshots(
        session,
        provider_id=provider_id,
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
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Chart data: percent → fill curves; tokens/cost → daily bars."""
    return query_chart(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
        metric=metric,
    )


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
        ws = datetime.fromisoformat(window_start.replace("Z", "+00:00").replace(" ", "+"))
        we = datetime.fromisoformat(window_end.replace("Z", "+00:00").replace(" ", "+"))
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
    order: str = Query(default="desc"),
    include_raw: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Recent event tail for a (provider_id, account_id) pair.

    Supports filtering by time range, model, sidecar, and kind. Returns events
    newest-first by default. raw_json is excluded unless include_raw=true.
    Use kind=error to retrieve only provider failure events.
    """
    since_dt: datetime | None = None
    until_dt: datetime | None = None
    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if until:
        until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))

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
        order=order,
    )

    rows = []
    for ev in events:
        d = ev.model_dump()
        if not include_raw:
            d.pop("raw_json", None)
        rows.append(d)

    return {"events": rows, "total": len(rows), "limit": limit}


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
    tz: str | None = Query(default=None, description="IANA timezone (e.g. Europe/Berlin)"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """7×24 hour-of-day token activity grid over the last N days.

    Always returns all 168 cells (7 days × 24 hours). Cells with no events
    have tokens=0. dow follows SQLite convention: 0=Sunday … 6=Saturday.

    When `tz` is a valid IANA name, events are bucketed in that zone (so the
    grid reflects local hour-of-day). Invalid or missing `tz` falls back to
    UTC bucketing.
    """
    cells = query_heatmap(
        session,
        provider_id=provider_id,
        account_id=account_id,
        days=days,
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
    limit: int = Query(default=20, ge=1, le=100),
    sort_by: Literal["tokens", "recent"] = Query(default="tokens"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Top-N sessions within the requested time window.

    Defaults to the last 7 days when 'since' is not provided. Events
    without a session_id are excluded.
    sort_by='tokens' (default) orders by token total desc; 'recent' orders by ts_end desc.
    """
    since_dt: datetime | None = None
    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))

    sessions = query_sessions(
        session,
        provider_id=provider_id,
        account_id=account_id,
        since=since_dt,
        limit=limit,
        sort_by=sort_by,
    )
    return {"sessions": sessions}


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
