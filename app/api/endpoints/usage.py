# ruff: noqa: F821  # Phase 1 schema reset: function bodies reference deleted tables (UsageSnapshot, CumulativeUsage, UsageSnapshotModel); rewritten in Phase 7-8
import csv
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Integer, func
from sqlmodel import Session, select

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.models.db import ProviderConfig
from app.models.schemas import ForecastResponse, LimitCard, LimitsResponse
from app.services.collector_manager import manager
from app.services.forecast import compute_all_forecasts

router = APIRouter()

_CSV_COLUMNS = [
    "timestamp",
    "provider_id",
    "account_id",
    "account_label",
    "service_name",
    "used_value",
    "limit_value",
    "unit_type",
    "currency",
    "tier",
    "model_id",
    "window_type",
    "variant",
    "health",
]


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
    - sidecar_contributions: per-sidecar token totals from CumulativeUsage
      (current month), used by the Fuel Dump bar.
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

    now = datetime.now(UTC)
    # Phase 7 will rewire sidecar_contributions from usage_period_rollup table.
    # CumulativeUsage was removed in the Phase 1 schema reset.
    contrib: dict[tuple[str, str], dict[str, dict[str, float]]] = {}

    fleet = []
    for (pid, aid), gcards in sorted(groups.items()):
        critical = _pick_critical_card(gcards)
        secondary = [c for c in gcards if c is not critical]
        fleet.append(
            {
                "provider_id": pid,
                "account_id": aid,
                "critical_gauge": critical,
                "secondary_limits": secondary,
                "sidecar_contributions": contrib.get((pid, aid), {}),
            }
        )

    return {"fleet": fleet, "generated_at": now.isoformat()}


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
    """Authoritative cumulative usage rolled up across sidecars.

    Default: returns one entry per (provider_id, account_id) with
    `lifetime`, current-year, and current-month totals summed across all
    contributing sidecars (matches spec §4 GROUP BY rollup).

    Query params narrow the scope:
    - provider_id / account_id: identity filter
    - period_type: one of 'lifetime' | 'year' | 'month'
    - period_key: a specific bucket (e.g. '2026', '2026-05', 'all')
    """
    # Phase 8 will rewire from usage_period_rollup table.
    # CumulativeUsage was removed in the Phase 1 schema reset.
    now = datetime.now(UTC)
    return {"cumulative": [], "generated_at": now.isoformat()}


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


@router.get("/history")
@limiter.limit("10/minute")
async def get_usage_history(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=1.0, ge=0.01, le=90.0),
    limit: int = Query(default=500, ge=1, le=2000),
    export_format: str = Query(default="json", alias="format"),
    session: Session = Depends(get_session),
):
    """Fetch usage history snapshots. Use format=csv for a downloadable CSV.

    Uses SQL GROUP BY for aggregation - dramatically faster than Python-side processing.
    """
    # Phase 7 will rewire from usage_period_rollup / UsageEvent tables.
    # UsageSnapshot was removed in the Phase 1 schema reset.
    return {"averages": [], "peaks": []}


@router.get("/history/raw")
@limiter.limit("30/minute")
async def get_usage_history_raw(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=1.0, ge=0.01, le=90.0),
    limit: int = Query(default=500, ge=1, le=2000),
    session: Session = Depends(get_session),
):
    """Fetch pre-bucketed usage history for chart rendering.

    Snapshots are grouped into time buckets (granularity determined by the
    requested window) so that the chart always receives ~50 data points per
    time range regardless of how many raw polls exist in the DB.  Each
    returned row carries both the avg and the peak (max_used_value) for its
    bucket, enabling the BAND mode shaded-area display.

    Uses SQL GROUP BY for aggregation - dramatically faster than Python-side processing.
    """
    # Phase 7 will rewire from usage_period_rollup / UsageEvent tables.
    # UsageSnapshot was removed in the Phase 1 schema reset.
    return []


@router.get("/history/deltas")
@limiter.limit("30/minute")
async def get_usage_history_deltas(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=1.0, ge=0.01, le=90.0),
    session: Session = Depends(get_session),
):
    """Fetch raw snapshots and compute positive deltas per series.

    Unlike /history/raw (which buckets and averages), this endpoint returns
    the *actual consumption* within the period by walking each time-series
    chronologically and summing only positive deltas.

    To improve fidelity, it employs a 'high-water mark' with glitch filtering:
    - Minor drops (>50% of previous peak) are ignored as transient API glitches.
    - Substantial drops (<50% of previous peak) are treated as periodic counter resets.
    """
    # Phase 7 will rewire from usage_period_rollup / UsageEvent tables.
    # UsageSnapshot was removed in the Phase 1 schema reset.
    return {
        "token_delta_total": 0.0,
        "cost_delta_total": 0.0,
        "provider_token_deltas": {},
        "critical_series_count": 0,
        "series_sampled": False,
        "series": [],
    }


def _effective_label(raw: str | None) -> str | None:
    """Return None for absent or generic placeholder labels ('default', 'Default', etc.)."""
    if not raw or raw.lower() == "default":
        return None
    return raw


def _build_label_map(session: Session) -> dict[tuple[str, str], str]:
    """Return current custom labels from ProviderConfig keyed by (provider_id, account_id).

    Used to overlay user-set labels onto historical snapshots that may have been
    collected before the label was configured (stored as NULL account_label).
    """
    configs = session.exec(
        select(ProviderConfig).where(ProviderConfig.account_label.isnot(None))  # type: ignore[union-attr]
    ).all()
    return {(c.provider_id, c.account_id): c.account_label for c in configs if c.account_label}


def _build_by_model_lookup(
    session: Session,
    since: datetime,
    bucket_seconds: int,
    provider_id: str | None = None,
    account_id: str | None = None,
) -> dict[tuple[int, str, str], list[dict]]:
    """Aggregate UsageSnapshotModel by time bucket for history detail.

    Returns dict keyed by (bucket_epoch, provider_id, account_id) with
    summed cost, msgs, and token fields per model.
    """
    from app.models.db import UsageSnapshotModel

    bucket_expr = (
        func.floor(func.strftime("%s", UsageSnapshot.timestamp).cast(Integer()) / bucket_seconds)
        * bucket_seconds
    ).label("bucket_ts")

    stmt = (
        select(  # type: ignore[call-overload]
            bucket_expr,
            UsageSnapshot.provider_id,
            UsageSnapshot.account_id,
            UsageSnapshot.window_type,
            UsageSnapshotModel.model_id,
            func.avg(UsageSnapshotModel.cost).label("avg_cost"),
            func.avg(UsageSnapshotModel.msgs).label("avg_msgs"),
            func.avg(UsageSnapshotModel.tokens_input).label("avg_input"),
            func.avg(UsageSnapshotModel.tokens_output).label("avg_output"),
            func.avg(UsageSnapshotModel.tokens_reasoning).label("avg_reasoning"),
            func.avg(UsageSnapshotModel.tokens_cache_read).label("avg_cache_read"),
            func.avg(UsageSnapshotModel.tokens_total).label("avg_total"),
        )
        .join(UsageSnapshotModel, UsageSnapshot.id == UsageSnapshotModel.snapshot_id)
        .where(UsageSnapshot.timestamp >= since)
        .group_by(
            bucket_expr,
            UsageSnapshot.provider_id,
            UsageSnapshot.account_id,
            UsageSnapshot.window_type,
            UsageSnapshotModel.model_id,
        )
    )

    if provider_id:
        stmt = stmt.where(UsageSnapshot.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsageSnapshot.account_id == account_id)

    results = session.exec(stmt).all()

    lookup: dict[tuple[int, str, str, str], list[dict]] = {}
    for r in results:
        key = (int(r.bucket_ts), r.provider_id, r.account_id, r.window_type)
        if key not in lookup:
            lookup[key] = []
        lookup[key].append(
            {
                "model_id": r.model_id,
                "cost": round(r.avg_cost, 4) if r.avg_cost is not None else None,
                "msgs": int(r.avg_msgs) if r.avg_msgs is not None else None,
                "tokens_input": round(r.avg_input, 0) if r.avg_input is not None else None,
                "tokens_output": round(r.avg_output, 0) if r.avg_output is not None else None,
                "tokens_reasoning": round(r.avg_reasoning, 0)
                if r.avg_reasoning is not None
                else None,
                "tokens_cache_read": round(r.avg_cache_read, 0)
                if r.avg_cache_read is not None
                else None,
                "tokens_total": round(r.avg_total, 0) if r.avg_total is not None else None,
            }
        )

    return lookup


# Credit-based providers: their "monthly" is a credit bucket, goes to weekly column
CREDIT_PROVIDERS = {"openrouter", "minimax"}

# Session-like window types (exact matches only)
SESSION_WINDOWS = {"session", "daily", "hourly", "prepaid"}
# Weekly-like window types (exact matches only)
WEEKLY_WINDOWS = {"weekly", "biweekly", "bi-weekly"}


def _classify_window(
    window_type: str | None,
    provider_id: str | None = None,
    model_id: str | None = None,
) -> str:
    """Classify window_type into category: 'session', 'weekly', 'monthly', or 'other'.

    For credit providers (openrouter, minimax):
    - session/daily/hourly → session column
    - monthly (credit bucket) → weekly column
    - other windows → other column

    For other providers:
    - session/daily/hourly → session column
    - weekly/biweekly → weekly column
    - monthly → monthly column
    - weekly + model_id set (sonnet/opus/design) → Additional (avoids collision in single Weekly cell)
    - Other (model-specific, etc.) → Additional
    """
    if not window_type:
        return "other"
    w = window_type.lower()

    # Session windows go to session for all providers
    if w in SESSION_WINDOWS:
        return "session"

    # For credit providers: monthly = credit bucket = weekly column
    if provider_id and provider_id.lower() in CREDIT_PROVIDERS:
        if w in WEEKLY_WINDOWS or w == "monthly":
            return "weekly"
        return "other"

    # Monthly gets its own slot
    if w == "monthly":
        return "monthly"

    # For other providers: weekly-like windows go to weekly, unless model-scoped
    if w in WEEKLY_WINDOWS:
        return "other" if model_id else "weekly"

    return "other"


def _group_snapshots(
    snapshots: Sequence[Any],  # UsageSnapshot removed in schema reset
    bucket_seconds: int = 60,
    label_map: dict[tuple[str, str], str] | None = None,
    by_model_lookup: dict[tuple[int, str, str], list[dict]] | None = None,
) -> list[dict]:
    """Group snapshots by bucket+provider+account_label for table display.

    Uses bucketed timestamps so snapshots collected slightly apart in time
    (e.g., 9:13:01 vs 9:13:02) are grouped together.

    Returns list of grouped records:
    {
        "timestamp": "...",
        "provider_id": "...",
        "account_label": "...",
        "session": [{"value": float, "unit": str}, ...],   # or null
        "weekly": [{"value": float, "unit": str}, ...],    # or null
        "monthly": [{"value": float, "unit": str}, ...],   # or null
        "additional": [ {"window": str, "value": float, "unit": str}, ... ]
    }
    """
    from collections import defaultdict

    grouped: dict[tuple, dict] = defaultdict(
        lambda: {
            "windows": [],
        }
    )

    # Track original timestamps per key to return the "representative" timestamp
    timestamp_map: dict[tuple, datetime] = {}

    # Track account_id per group key for by_model lookup
    account_id_map: dict[tuple, str] = {}

    for s in snapshots:
        ts = s.timestamp if s.timestamp.tzinfo else s.timestamp.replace(tzinfo=UTC)
        # Epoch-based bucket: floor-divide epoch seconds so all timestamps within
        # a bucket window (e.g. the same calendar day) hash to the same key.
        bucket_epoch = int(ts.timestamp()) // bucket_seconds * bucket_seconds
        bucket_ts = datetime.fromtimestamp(bucket_epoch, tz=UTC)
        resolved_label = (
            _effective_label(s.account_label)
            or (label_map or {}).get((s.provider_id, s.account_id))
            or s.account_id
        )
        key = (bucket_ts.isoformat(), s.provider_id, resolved_label)

        # Store first timestamp seen for this key as representative
        if key not in timestamp_map:
            timestamp_map[key] = ts

        # Track account_id per group key for by_model lookup
        if key not in account_id_map:
            account_id_map[key] = s.account_id

        category = _classify_window(s.window_type, s.provider_id, s.model_id)

        # Lookup per-model breakdown for THIS specific snapshot
        by_model = None
        if by_model_lookup:
            bm_key = (
                int(bucket_ts.timestamp()),
                s.provider_id,
                s.account_id,
                s.window_type,
            )
            by_model = by_model_lookup.get(bm_key)

        grouped[key]["windows"].append(
            {
                "category": category,
                "window": s.window_type,
                "model_id": s.model_id,
                "value": s.used_value,
                "unit": s.unit_type,
                "limit": s.limit_value,
                "token_usage": {
                    "input": s.tokens_input,
                    "output": s.tokens_output,
                    "reasoning": s.tokens_reasoning,
                    "cache_read": s.tokens_cache_read,
                    "total": s.tokens_total,
                }
                if s.tokens_total is not None
                else None,
                "msgs": s.msgs,
                "by_model": by_model,
            }
        )

    result = []
    for (bucket_ts_iso, provider_id, account_label), data in grouped.items():
        # Use the stored representative timestamp for display
        rep_ts = timestamp_map[(bucket_ts_iso, provider_id, account_label)]

        result.append(
            {
                "timestamp": rep_ts.isoformat(),
                "provider_id": provider_id,
                "account_label": account_label,
                "windows": data["windows"],
            }
        )

    # Sort by timestamp descending (newest first)
    result.sort(key=lambda x: x["timestamp"], reverse=True)
    return result


def _pick_bucket_seconds(days: float) -> int:
    """Bucket size for the history window. Matches the frontend pickBucketSeconds."""
    if days >= 30:
        return 86400  # 30d/90d → daily        (~30–90 pts)
    if days >= 7:
        return 10800  # 7d → 3-hourly          (~56 pts)
    if days >= 1:
        return 1800  # 24h → 30-min           (~48 pts)
    if days >= 0.25:
        return 900  # 6h → 15-min            (~24 pts)
    return 300  # 1h → 5-min             (~12 slots)


def _history_as_csv(results: Sequence[Any]) -> StreamingResponse:  # UsageSnapshot removed
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for s in results:
        writer.writerow(
            {
                "timestamp": s.timestamp.isoformat(),
                "provider_id": s.provider_id,
                "account_id": s.account_id,
                "account_label": s.account_label or "",
                "service_name": s.service_name,
                "used_value": s.used_value,
                "limit_value": s.limit_value,
                "unit_type": s.unit_type,
                "currency": s.currency or "",
                "tier": s.tier or "",
                "model_id": s.model_id or "",
                "window_type": s.window_type,
                "variant": s.variant or "",
                "health": s.health,
            }
        )
    filename = f"runway-history-{datetime.now(UTC).strftime('%Y-%m-%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
