import csv
import io
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Integer, func
from sqlmodel import Session, asc, desc, select

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.models.db import ProviderConfig, UsageSnapshot
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
async def fetch_all_limits(request: Request) -> dict[str, Any]:
    """Fetch all AI service usage limits from the in-memory registry."""
    results = manager.get_registry_snapshot()
    if not results:
        # Bootstrap fallback: registry not yet populated (first request races startup)
        # _do_collect() updates manager._registry, so no external write needed here.
        results = await manager.collect_all()

    # Validate and serialize with None values included
    limit_cards = [LimitCard(**item) for item in results]
    response = LimitsResponse(limits=limit_cards)

    # Return dict with None values included (needed for tier field)
    return response.model_dump(exclude_none=False)


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
    results = manager.get_registry_snapshot()
    if not results:
        # Bootstrap fallback: registry not yet populated (first request races startup)
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
    since = datetime.now(UTC) - timedelta(days=days)

    if export_format == "csv":
        # CSV is the archival dump — fetch raw rows without aggregation
        statement = (
            select(UsageSnapshot)
            .where(UsageSnapshot.timestamp >= since)
            .order_by(desc(UsageSnapshot.timestamp))
            .limit(limit)
        )
        if provider_id:
            statement = statement.where(UsageSnapshot.provider_id == provider_id)
        if account_id:
            statement = statement.where(UsageSnapshot.account_id == account_id)

        results = session.exec(statement).all()
        return _history_as_csv(results)

    # JSON path: SQL aggregation with adaptive bucket granularity
    bucket_seconds = _pick_bucket_seconds(days)

    # SQLite bucket expression
    bucket_expr = (
        func.floor(func.strftime("%s", UsageSnapshot.timestamp).cast(Integer()) / bucket_seconds)
        * bucket_seconds
    ).label("bucket_ts")

    # SQL aggregation
    stmt = (
        select(  # type: ignore[call-overload]
            bucket_expr,
            UsageSnapshot.provider_id,
            UsageSnapshot.account_id,
            UsageSnapshot.account_label,
            UsageSnapshot.service_name,
            UsageSnapshot.window_type,
            UsageSnapshot.model_id,
            UsageSnapshot.variant,
            UsageSnapshot.unit_type,
            UsageSnapshot.data_source,
            func.avg(UsageSnapshot.used_value).label("avg_used"),
            func.max(UsageSnapshot.used_value).label("max_used"),
            func.avg(UsageSnapshot.limit_value).label("avg_limit"),
            func.avg(UsageSnapshot.tokens_input).label("avg_tokens_input"),
            func.avg(UsageSnapshot.tokens_output).label("avg_tokens_output"),
            func.avg(UsageSnapshot.tokens_reasoning).label("avg_tokens_reasoning"),
            func.avg(UsageSnapshot.tokens_total).label("avg_tokens_total"),
            func.avg(UsageSnapshot.msgs).label("avg_msgs"),
        )
        .where(UsageSnapshot.timestamp >= since)
        .order_by(desc("bucket_ts"))
    )

    if provider_id:
        stmt = stmt.where(UsageSnapshot.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsageSnapshot.account_id == account_id)

    stmt = stmt.group_by(
        bucket_expr,
        UsageSnapshot.provider_id,
        UsageSnapshot.account_id,
        UsageSnapshot.service_name,
        UsageSnapshot.window_type,
        UsageSnapshot.model_id,
        UsageSnapshot.variant,
        UsageSnapshot.unit_type,
    )

    raw = session.exec(stmt.limit(20000)).all()

    # Post-processing: Apply label map
    label_map = _build_label_map(session)
    by_model_lookup = _build_by_model_lookup(
        session, since, bucket_seconds, provider_id, account_id
    )

    # Build aggregated snapshots for averages and peaks
    averages = []
    peaks = []
    for r in raw:
        snapshot_base = {
            "timestamp": datetime.fromtimestamp(r.bucket_ts, tz=UTC),
            "provider_id": r.provider_id,
            "account_id": r.account_id,
            "account_label": (
                _effective_label(r.account_label) or label_map.get((r.provider_id, r.account_id))
            ),
            "service_name": r.service_name,
            "window_type": r.window_type,
            "model_id": r.model_id,
            "variant": r.variant,
            "unit_type": r.unit_type,
            "data_source": r.data_source,
        }

        # Average snapshot (include even if NULL to preserve structure)
        averages.append(
            UsageSnapshot(
                **snapshot_base,
                used_value=round(r.avg_used, 4) if r.avg_used is not None else None,
                limit_value=round(r.avg_limit, 4) if r.avg_limit is not None else None,
                tokens_input=round(r.avg_tokens_input, 4)
                if r.avg_tokens_input is not None
                else None,
                tokens_output=round(r.avg_tokens_output, 4)
                if r.avg_tokens_output is not None
                else None,
                tokens_reasoning=round(r.avg_tokens_reasoning, 4)
                if r.avg_tokens_reasoning is not None
                else None,
                tokens_total=round(r.avg_tokens_total, 4)
                if r.avg_tokens_total is not None
                else None,
                msgs=round(r.avg_msgs, 1) if r.avg_msgs is not None else None,
                health="good",  # Not used for aggregated data
            )
        )

        # Peak snapshot (include even if NULL)
        peaks.append(
            UsageSnapshot(
                **snapshot_base,
                used_value=round(r.max_used, 4) if r.max_used is not None else None,
                limit_value=round(r.avg_limit, 4) if r.avg_limit is not None else None,
                tokens_input=round(r.avg_tokens_input, 4)
                if r.avg_tokens_input is not None
                else None,
                tokens_output=round(r.avg_tokens_output, 4)
                if r.avg_tokens_output is not None
                else None,
                tokens_reasoning=round(r.avg_tokens_reasoning, 4)
                if r.avg_tokens_reasoning is not None
                else None,
                tokens_total=round(r.avg_tokens_total, 4)
                if r.avg_tokens_total is not None
                else None,
                msgs=round(r.avg_msgs, 1) if r.avg_msgs is not None else None,
                health="good",
            )
        )

    # Group by timestamp+provider+account for table display
    avg_grouped = _group_snapshots(averages[:limit], bucket_seconds, label_map, by_model_lookup)
    peak_grouped = _group_snapshots(peaks[:limit], bucket_seconds, label_map, by_model_lookup)

    return {"averages": avg_grouped, "peaks": peak_grouped}


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
    since = datetime.now(UTC) - timedelta(days=days)
    bucket_seconds = _pick_bucket_seconds(days)

    # SQLite bucket expression: floor-divide epoch seconds to bucket boundaries
    bucket_expr = (
        func.floor(func.strftime("%s", UsageSnapshot.timestamp).cast(Integer()) / bucket_seconds)
        * bucket_seconds
    ).label("bucket_ts")

    # SQL aggregation: AVG for trend, MAX for spike preservation (BAND mode)
    stmt = (
        select(  # type: ignore[call-overload]
            bucket_expr,
            UsageSnapshot.provider_id,
            UsageSnapshot.account_id,
            UsageSnapshot.account_label,
            UsageSnapshot.service_name,
            UsageSnapshot.window_type,
            UsageSnapshot.model_id,
            UsageSnapshot.variant,
            UsageSnapshot.unit_type,
            UsageSnapshot.data_source,
            func.avg(UsageSnapshot.used_value).label("avg_used"),
            func.max(UsageSnapshot.used_value).label("max_used"),
            func.avg(UsageSnapshot.limit_value).label("avg_limit"),
            func.avg(UsageSnapshot.tokens_input).label("avg_tokens_input"),
            func.avg(UsageSnapshot.tokens_output).label("avg_tokens_output"),
            func.avg(UsageSnapshot.tokens_reasoning).label("avg_tokens_reasoning"),
            func.avg(UsageSnapshot.tokens_cache_read).label("avg_tokens_cache_read"),
            func.avg(UsageSnapshot.tokens_total).label("avg_tokens_total"),
            func.avg(UsageSnapshot.msgs).label("avg_msgs"),
        )
        .where(UsageSnapshot.timestamp >= since)
        .order_by(asc("bucket_ts"))
    )

    if provider_id:
        stmt = stmt.where(UsageSnapshot.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsageSnapshot.account_id == account_id)

    # GROUP BY bucket timestamp + series identity
    stmt = stmt.group_by(
        bucket_expr,
        UsageSnapshot.provider_id,
        UsageSnapshot.account_id,
        UsageSnapshot.service_name,
        UsageSnapshot.window_type,
        UsageSnapshot.model_id,
        UsageSnapshot.variant,
        UsageSnapshot.unit_type,
    )

    raw = session.exec(stmt).all()

    # Post-processing: Apply label map (preserves current behavior)
    label_map = _build_label_map(session)

    return [
        {
            "id": None,
            "timestamp": datetime.fromtimestamp(r.bucket_ts, tz=UTC).isoformat(),
            "provider_id": r.provider_id,
            "account_id": r.account_id,
            "account_label": (
                _effective_label(r.account_label) or label_map.get((r.provider_id, r.account_id))
            ),
            "service_name": r.service_name,
            "used_value": round(r.avg_used, 4) if r.avg_used is not None else None,
            "limit_value": round(r.avg_limit, 4) if r.avg_limit is not None else None,
            "max_used_value": round(r.max_used, 4) if r.max_used is not None else None,
            "unit_type": r.unit_type,
            "window_type": r.window_type,
            "model_id": r.model_id,
            "variant": r.variant,
            "data_source": r.data_source,
            "token_usage": {
                "input": round(r.avg_tokens_input, 0) if r.avg_tokens_input is not None else None,
                "output": round(r.avg_tokens_output, 0)
                if r.avg_tokens_output is not None
                else None,
                "reasoning": round(r.avg_tokens_reasoning, 0)
                if r.avg_tokens_reasoning is not None
                else None,
                "cache_read": round(r.avg_tokens_cache_read, 0)
                if r.avg_tokens_cache_read is not None
                else None,
                "total": round(r.avg_tokens_total, 0) if r.avg_tokens_total is not None else None,
            }
            if any([r.avg_tokens_input, r.avg_tokens_output, r.avg_tokens_total])
            else None,
            "msgs": round(r.avg_msgs, 0) if r.avg_msgs is not None else None,
        }
        for r in raw
    ][:limit]


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
    since = datetime.now(UTC) - timedelta(days=days)

    stmt = (
        select(  # type: ignore[call-overload]
            UsageSnapshot.timestamp,
            UsageSnapshot.provider_id,
            UsageSnapshot.account_id,
            UsageSnapshot.window_type,
            UsageSnapshot.model_id,
            UsageSnapshot.unit_type,
            UsageSnapshot.tokens_total,
            UsageSnapshot.used_value,
            UsageSnapshot.limit_value,
        )
        .where(UsageSnapshot.timestamp >= since)
        .order_by(asc(UsageSnapshot.timestamp))
    )

    if provider_id:
        stmt = stmt.where(UsageSnapshot.provider_id == provider_id)
    if account_id:
        stmt = stmt.where(UsageSnapshot.account_id == account_id)

    rows = session.exec(stmt).all()

    # Group by series key
    series_groups: dict[str, list[Any]] = {}
    for r in rows:
        key = (
            f"{r.provider_id}|{r.account_id}|{r.window_type or ''}|{r.model_id or ''}|{r.unit_type}"
        )
        series_groups.setdefault(key, []).append(r)

    # 1. Calculate deltas for EVERY individual series
    series_deltas = {}
    # Glitch filter: drops below 50% are resets; others are ignored as glitches
    GLITCH_THRESHOLD = 0.5

    for key, arr in series_groups.items():
        arr.sort(key=lambda r: r.timestamp)
        token_delta = 0.0
        cost_delta = 0.0
        critical = False

        # Initialize high-water marks from the first reading (Baseline)
        first = arr[0]
        max_tokens = first.tokens_total if first.tokens_total is not None else 0.0
        max_cost = (first.used_value if first.used_value is not None else 0.0) if first.unit_type == "currency" else 0.0

        for i in range(1, len(arr)):
            curr = arr[i]
            
            # Token Delta
            curr_tok = curr.tokens_total if curr.tokens_total is not None else 0.0
            if max_tokens == 0.0 and curr_tok > 0:
                max_tokens = curr_tok
                continue
            if curr_tok > max_tokens:
                token_delta += curr_tok - max_tokens
                max_tokens = curr_tok
            elif curr_tok < max_tokens * GLITCH_THRESHOLD:
                max_tokens = curr_tok

            # Cost Delta
            if curr.unit_type == "currency":
                curr_cost = curr.used_value if curr.used_value is not None else 0.0
                if max_cost == 0.0 and curr_cost > 0:
                    max_cost = curr_cost
                    continue
                if curr_cost > max_cost:
                    cost_delta += curr_cost - max_cost
                    max_cost = curr_cost
                elif curr_cost < max_cost * GLITCH_THRESHOLD:
                    max_cost = curr_cost

            # Critical check
            if not critical and curr.used_value is not None:
                if (curr.unit_type == "percent" and curr.used_value >= 90) or (
                    curr.limit_value and curr.limit_value > 0 and (curr.used_value / curr.limit_value) >= 0.9
                ):
                    critical = True

        series_deltas[key] = {
            "token_delta": token_delta,
            "cost_delta": cost_delta,
            "critical": critical,
            "provider_id": arr[0].provider_id,
            "account_id": arr[0].account_id,
            "window_type": arr[0].window_type,
            "model_id": arr[0].model_id,
            "unit_type": arr[0].unit_type,
        }

    # 2. Aggregate totals with Hierarchy Filter (Prevent double-counting)
    # Group series by (provider, account, window, unit)
    hierarchy_groups: dict[tuple, list[str]] = {}
    for key, d in series_deltas.items():
        h_key = (d["provider_id"], d["account_id"], d["window_type"], d["unit_type"])
        hierarchy_groups.setdefault(h_key, []).append(key)

    token_delta_total = 0.0
    cost_delta_total = 0.0
    provider_token_deltas: dict[str, float] = {}
    critical_series_count = 0
    series_list = []

    for h_key, keys in hierarchy_groups.items():
        # Check if we have specific models for this window
        model_keys = [k for k in keys if series_deltas[k]["model_id"] is not None]
        
        # If specific models exist, use ONLY them (ignore model_id=None)
        # If no models exist, use whatever is there (usually just the aggregate model_id=None)
        selected_keys = model_keys if model_keys else keys

        for k in selected_keys:
            d = series_deltas[k]
            token_delta_total += d["token_delta"]
            cost_delta_total += d["cost_delta"]
            pid = d["provider_id"]
            provider_token_deltas[pid] = provider_token_deltas.get(pid, 0.0) + d["token_delta"]
            if d["critical"]:
                critical_series_count += 1
            
            series_list.append({
                "provider_id": pid,
                "account_id": d["account_id"],
                "window_type": d["window_type"],
                "model_id": d["model_id"],
                "unit_type": d["unit_type"],
                "token_delta": round(d["token_delta"], 2),
                "cost_delta": round(d["cost_delta"], 2),
                "critical": d["critical"],
            })

    return {
        "token_delta_total": round(token_delta_total, 2),
        "cost_delta_total": round(cost_delta_total, 2),
        "provider_token_deltas": {k: round(v, 2) for k, v in provider_token_deltas.items()},
        "critical_series_count": critical_series_count,
        "series_sampled": False,
        "series": series_list,
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
    snapshots: Sequence[UsageSnapshot],
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


def _history_as_csv(results: Sequence[UsageSnapshot]) -> StreamingResponse:
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
