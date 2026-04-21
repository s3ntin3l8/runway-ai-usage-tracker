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
from app.models.db import UsageSnapshot
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
    limit: int = Query(default=50, ge=1, le=2000),
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
    # long views stay compact. This also prevents a high-volume day from consuming
    # the whole LIMIT budget and hiding older days.
    bucket_seconds = _pick_bucket_seconds(days)
    raw = session.exec(statement.limit(20000)).all()
    deduped = _dedupe_by_bucket(raw, bucket_seconds)
    return [_snapshot_to_dict(s) for s in deduped[:limit]]


def _pick_bucket_seconds(days: float) -> int:
    """Bucket size for the history window. Matches the frontend pickBucketSeconds."""
    if days >= 2:
        return 86400  # 7d/30d/90d → daily
    if days >= 0.5:
        return 3600  # 1d → hourly (≤24 points)
    if days >= 0.1:
        return 900  # 6h → 15 min (≤24 points)
    return 60  # 1h → 1 min (≤60 points)


def _dedupe_by_bucket(rows: Sequence[UsageSnapshot], bucket_seconds: int) -> list[UsageSnapshot]:
    """Keep the most recent row per (provider, account, model, window, unit, bucket).

    Assumes `rows` is already sorted by timestamp descending — the first row seen
    for each key wins.
    """
    seen: set[tuple] = set()
    kept: list[UsageSnapshot] = []
    for r in rows:
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
        if key in seen:
            continue
        seen.add(key)
        kept.append(r)
    return kept


def _snapshot_to_dict(s: UsageSnapshot) -> dict:
    # Ensure timestamp is timezone-aware UTC before isoformat()
    ts = s.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)

    return {
        "id": s.id,
        "timestamp": ts.isoformat(),
        "provider_id": s.provider_id,
        "account_id": s.account_id,
        "account_label": s.account_label,
        "service_name": s.service_name,
        "used_value": s.used_value,
        "limit_value": s.limit_value,
        "unit_type": s.unit_type,
        "currency": s.currency,
        "tier": s.tier,
        "model_id": s.model_id,
        "window_type": s.window_type,
        "health": s.health,
        "sidecar_id": s.sidecar_id,
        "is_unlimited": s.is_unlimited,
        "data_source": s.data_source,
        "metadata": s.raw_metadata,
    }


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
