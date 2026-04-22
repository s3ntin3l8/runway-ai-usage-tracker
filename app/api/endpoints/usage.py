import csv
import io
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlmodel import Session, desc, select

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.models.db import ProviderConfig, UsageSnapshot
from app.models.schemas import LimitCard, LimitsResponse
from app.services.collector_manager import manager

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


@router.get("/history")
@limiter.limit("30/minute")
async def get_usage_history(
    request: Request,
    provider_id: str | None = None,
    account_id: str | None = None,
    days: float = Query(default=1.0, ge=0.01, le=90.0),
    limit: int = Query(default=500, ge=1, le=2000),
    export_format: str = Query(default="json", alias="format"),
    session: Session = Depends(get_session),
):
    """Fetch usage history snapshots. Use format=csv for a downloadable CSV."""
    since = datetime.now(UTC) - timedelta(days=days)

    statement = select(UsageSnapshot).where(UsageSnapshot.timestamp >= since)

    if provider_id:
        statement = statement.where(UsageSnapshot.provider_id == provider_id)
    if account_id:
        statement = statement.where(UsageSnapshot.account_id == account_id)

    statement = statement.order_by(desc(UsageSnapshot.timestamp))

    if export_format == "csv":
        # CSV is the archival dump — keep raw rows, apply limit directly.
        results = session.exec(statement.limit(limit)).all()
        return _history_as_csv(results)

    # JSON path: downsample to one row per (provider, account, model, window, unit, bucket).
    # Bucket size adapts to the window so short views keep sub-hour resolution and
    # long views stay compact. Response includes both averages (avg per bucket)
    # and peaks (max per bucket).
    bucket_seconds = _pick_bucket_seconds(days)
    raw = session.exec(statement.limit(20000)).all()
    averages, peaks = _dedupe_with_peaks(raw, bucket_seconds)

    label_map = _build_label_map(session)

    # Group by timestamp+provider+account for table display
    avg_grouped = _group_snapshots(averages[:limit], bucket_seconds, label_map)
    peak_grouped = _group_snapshots(peaks[:limit], bucket_seconds, label_map)

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
    """Fetch raw usage history for chart rendering.

    Returns ungrouped snapshots so chart can display each unique
    provider+service+window combination as separate line.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    statement = select(UsageSnapshot).where(UsageSnapshot.timestamp >= since)

    if provider_id:
        statement = statement.where(UsageSnapshot.provider_id == provider_id)
    if account_id:
        statement = statement.where(UsageSnapshot.account_id == account_id)

    statement = statement.order_by(desc(UsageSnapshot.timestamp))
    results = session.exec(statement.limit(limit)).all()

    label_map = _build_label_map(session)

    return [
        {
            "id": r.id,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "provider_id": r.provider_id,
            "account_id": r.account_id,
            "account_label": (
                _effective_label(r.account_label) or label_map.get((r.provider_id, r.account_id))
            ),
            "service_name": r.service_name,
            "used_value": r.used_value,
            "limit_value": r.limit_value,
            "unit_type": r.unit_type,
            "window_type": r.window_type,
            "data_source": r.data_source,
        }
        for r in results
    ]


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


# Credit-based providers: their "monthly" is a credit bucket, goes to weekly column
CREDIT_PROVIDERS = {"openrouter", "minimax"}

# Session-like window types (exact matches only)
SESSION_WINDOWS = {"session", "daily", "hourly", "prepaid"}
# Weekly-like window types (exact matches only)
WEEKLY_WINDOWS = {"weekly", "biweekly", "bi-weekly", "monthly"}

# Special model-specific windows that should go to Additional (not Weekly)
SPECIAL_MODEL_WINDOWS = {"seven_day_sonnet", "seven_day_opus", "seven_day_omelette"}


def _classify_window(window_type: str | None, provider_id: str | None = None) -> str:
    """Classify window_type into category: 'session', 'weekly', or 'other'.

    For credit providers (openrouter, minimax):
    - session/daily/hourly → session column
    - monthly (credit bucket) → weekly column
    - other windows → other column

    For other providers:
    - session/daily/hourly → session column
    - weekly/biweekly/monthly → weekly column
    - Special Claude models (seven_day_sonnet, seven_day_opus, seven_day_omelette) → Additional
    - Other (model-specific, etc.) → Additional
    """
    if not window_type:
        return "other"
    w = window_type.lower()

    # Special model windows always go to Additional
    if w in SPECIAL_MODEL_WINDOWS:
        return "other"

    # Session windows go to session for all providers
    if w in SESSION_WINDOWS:
        return "session"

    # For credit providers: monthly = credit bucket = weekly column
    if provider_id and provider_id.lower() in CREDIT_PROVIDERS:
        if w in WEEKLY_WINDOWS:
            return "weekly"
        return "other"

    # For other providers: weekly-like windows go to weekly
    if w in WEEKLY_WINDOWS:
        return "weekly"

    return "other"


def _group_snapshots(
    snapshots: Sequence[UsageSnapshot],
    bucket_seconds: int = 60,
    label_map: dict[tuple[str, str], str] | None = None,
) -> list[dict]:
    """Group snapshots by bucket+provider+account_label for table display.

    Uses bucketed timestamps so snapshots collected slightly apart in time
    (e.g., 9:13:01 vs 9:13:02) are grouped together.

    Returns list of grouped records:
    {
        "timestamp": "...",
        "provider_id": "...",
        "account_label": "...",
        "session": {"value": float, "unit": str},  # or null
        "weekly": {"value": float, "unit": str},   # or null
        "additional": [ {"window": str, "value": float, "unit": str}, ... ]
    }
    """
    from collections import defaultdict

    grouped: dict[tuple, dict] = defaultdict(
        lambda: {
            "session": None,
            "weekly": None,
            "additional": [],
        }
    )

    # Track original timestamps per key to return the "representative" timestamp
    timestamp_map: dict[tuple, datetime] = {}

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

        category = _classify_window(s.window_type, s.provider_id)
        entry = {
            "value": s.used_value,
            "unit": s.unit_type,
            "limit": s.limit_value,
        }

        if category == "session":
            grouped[key]["session"] = entry
        elif category == "weekly":
            grouped[key]["weekly"] = entry
        else:
            grouped[key]["additional"].append(
                {
                    "window": s.window_type,
                    "value": s.used_value,
                    "unit": s.unit_type,
                    "limit": s.limit_value,
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
                "session": data["session"],
                "weekly": data["weekly"],
                "additional": data["additional"] or None,
            }
        )

    # Sort by timestamp descending (newest first)
    result.sort(key=lambda x: x["timestamp"], reverse=True)
    return result


def _pick_bucket_seconds(days: float) -> int:
    """Bucket size for the history window. Matches the frontend pickBucketSeconds."""
    if days >= 2:
        return 86400  # 7d/30d/90d → daily
    if days >= 0.5:
        return 3600  # 1d → hourly (≤24 points)
    if days >= 0.1:
        return 900  # 6h → 15 min (≤24 points)
    return 60  # 1h → 1 min (≤60 points)


def _dedupe_with_peaks(
    rows: Sequence[UsageSnapshot], bucket_seconds: int
) -> tuple[list[UsageSnapshot], list[UsageSnapshot]]:
    """Dedupe by bucket, preserving first/last points and tracking peaks.

    Returns (averages, peaks) where:
    - averages: most recent row per bucket
    - peaks: row with max used_value per bucket
    Both always include first and last points by timestamp.
    """
    if not rows:
        return [], []

    sorted_rows = sorted(rows, key=lambda r: r.timestamp)
    first_row = sorted_rows[0]
    last_row = sorted_rows[-1]

    avg_seen: set[tuple] = set()
    averages: list[UsageSnapshot] = []

    bucket_peaks: dict[tuple, UsageSnapshot] = {}

    for r in sorted_rows:
        ts = r.timestamp if r.timestamp.tzinfo else r.timestamp.replace(tzinfo=UTC)
        bucket = int(ts.timestamp()) // bucket_seconds
        key = (
            r.provider_id,
            r.account_id,
            r.service_name,
            r.model_id,
            r.window_type,
            r.unit_type,
            bucket,
        )

        if key not in avg_seen:
            avg_seen.add(key)
            averages.append(r)

        value = r.used_value if r.used_value is not None else 0.0
        existing = bucket_peaks.get(key)
        if existing is None or value > (existing.used_value or 0.0):
            bucket_peaks[key] = r

    peaks = list(bucket_peaks.values())

    avg_keys = {
        (r.provider_id, r.account_id, r.service_name, r.model_id, r.window_type, r.unit_type)
        for r in averages
    }
    if (
        first_row.provider_id,
        first_row.account_id,
        first_row.service_name,
        first_row.model_id,
        first_row.window_type,
        first_row.unit_type,
    ) not in avg_keys:
        averages.insert(0, first_row)
    avg_keys_last = {
        (r.provider_id, r.account_id, r.service_name, r.model_id, r.window_type, r.unit_type)
        for r in averages
    }
    if (
        last_row.provider_id,
        last_row.account_id,
        last_row.service_name,
        last_row.model_id,
        last_row.window_type,
        last_row.unit_type,
    ) not in avg_keys_last:
        averages.append(last_row)

    peak_keys = {
        (r.provider_id, r.account_id, r.service_name, r.model_id, r.window_type, r.unit_type)
        for r in peaks
    }
    if (
        first_row.provider_id,
        first_row.account_id,
        first_row.service_name,
        first_row.model_id,
        first_row.window_type,
        first_row.unit_type,
    ) not in peak_keys:
        peaks.insert(0, first_row)
    peak_keys_last = {
        (r.provider_id, r.account_id, r.service_name, r.model_id, r.window_type, r.unit_type)
        for r in peaks
    }
    if (
        last_row.provider_id,
        last_row.account_id,
        last_row.service_name,
        last_row.model_id,
        last_row.window_type,
        last_row.unit_type,
    ) not in peak_keys_last:
        peaks.append(last_row)

    averages.sort(key=lambda r: r.timestamp, reverse=True)
    peaks.sort(key=lambda r: r.timestamp, reverse=True)

    return averages, peaks


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
