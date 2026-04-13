# app/services/compaction.py
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.models.db import UsageSnapshot

logger = logging.getLogger(__name__)

_HOURLY_DAYS = 60  # compact to hourly: rows older than 60 days
_DAILY_DAYS = 180  # compact to daily:  rows older than 180 days


def compact_snapshots(session: Session) -> dict:
    """
    Downsample old usage_snapshots to reduce DB size.

    Thresholds:
      - 60–180 days old  → one averaged row per (provider, account, model, window, hour)
      - 180+ days old    → one averaged row per (provider, account, model, window, day)

    Compacted rows are marked with raw_metadata_json = NULL.
    Rows already marked NULL are skipped (never re-compacted).

    Returns: {"hourly_compacted": N, "daily_compacted": N}
    """
    now = datetime.now(UTC)
    hourly_threshold = now - timedelta(days=_HOURLY_DAYS)
    daily_threshold = now - timedelta(days=_DAILY_DAYS)

    hourly_count = _compact_range(
        session,
        start=daily_threshold,
        end=hourly_threshold,
        bucket_fn=lambda ts: ts.strftime("%Y-%m-%d %H"),
    )
    daily_count = _compact_range(
        session,
        start=None,
        end=daily_threshold,
        bucket_fn=lambda ts: ts.strftime("%Y-%m-%d"),
    )
    return {"hourly_compacted": hourly_count, "daily_compacted": daily_count}


def _compact_range(
    session: Session,
    start: datetime | None,
    end: datetime,
    bucket_fn,
) -> int:
    """Compact rows in [start, end) into time buckets. Returns number of new rows created."""
    stmt = (
        select(UsageSnapshot)
        .where(UsageSnapshot.timestamp < end)
        .where(UsageSnapshot.raw_metadata_json != None)  # noqa: E711
    )
    if start is not None:
        stmt = stmt.where(UsageSnapshot.timestamp >= start)

    rows = session.exec(stmt).all()
    if not rows:
        return 0

    groups: dict[tuple, list[UsageSnapshot]] = defaultdict(list)
    for row in rows:
        key = (
            row.provider_id,
            row.account_id,
            row.model_id,
            row.window_type,
            row.unit_type,
            bucket_fn(row.timestamp),
        )
        groups[key].append(row)

    created = 0
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue  # single row — no compaction needed

        used_vals = [r.used_value for r in group_rows if r.used_value is not None]
        limit_vals = [r.limit_value for r in group_rows if r.limit_value is not None]
        avg_used = sum(used_vals) / len(used_vals) if used_vals else None
        avg_limit = sum(limit_vals) / len(limit_vals) if limit_vals else None

        template = group_rows[0]

        for row in group_rows:
            session.delete(row)

        session.add(
            UsageSnapshot(
                timestamp=template.timestamp,
                provider_id=template.provider_id,
                account_id=template.account_id,
                account_label=template.account_label,
                service_name=template.service_name,
                used_value=avg_used,
                limit_value=avg_limit,
                unit_type=template.unit_type,
                currency=template.currency,
                tier=template.tier,
                model_id=template.model_id,
                window_type=template.window_type,
                health=template.health,
                sidecar_id=template.sidecar_id,
                is_unlimited=template.is_unlimited,
                data_source=template.data_source,
                error_type=template.error_type,
                raw_metadata_json=None,  # compacted marker
            )
        )
        created += 1

    session.commit()
    logger.info(f"Compaction complete: {created} buckets merged")
    return created
