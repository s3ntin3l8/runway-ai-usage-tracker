# app/services/compaction.py
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.models.db import UsageSnapshot, UsageSnapshotModel

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

    rows = session.exec(stmt).all(); print(f"DEBUG: Found {len(rows)} rows to compact")
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

        # Collect all numeric values for averaging UsageSnapshot
        numeric_fields = [
            "used_value",
            "limit_value",
            "tokens_input",
            "tokens_output",
            "tokens_reasoning",
            "tokens_cache_read",
            "tokens_total",
            "msgs",
        ]
        averages = {}
        for field in numeric_fields:
            vals = [getattr(r, field) for r in group_rows if getattr(r, field) is not None]
            averages[field] = sum(vals) / len(vals) if vals else None

        # Handle UsageSnapshotModel aggregation
        snapshot_ids = [r.id for r in group_rows if r.id is not None]
        model_groups: dict[str, list[UsageSnapshotModel]] = defaultdict(list)
        if snapshot_ids:
            stmt_models = select(UsageSnapshotModel).where(
                UsageSnapshotModel.snapshot_id.in_(snapshot_ids)
            )
            model_rows = session.exec(stmt_models).all()
            for mr in model_rows:
                model_groups[mr.model_id].append(mr)
                session.delete(mr)  # Delete old individual model breakdowns

        template = group_rows[0]

        for row in group_rows:
            session.delete(row)

        new_snapshot = UsageSnapshot(
            timestamp=template.timestamp,
            provider_id=template.provider_id,
            account_id=template.account_id,
            account_label=template.account_label,
            service_name=template.service_name,
            used_value=averages["used_value"],
            limit_value=averages["limit_value"],
            tokens_input=averages["tokens_input"],
            tokens_output=averages["tokens_output"],
            tokens_reasoning=averages["tokens_reasoning"],
            tokens_cache_read=averages["tokens_cache_read"],
            tokens_total=averages["tokens_total"],
            msgs=int(averages["msgs"]) if averages["msgs"] is not None else None,
            unit_type=template.unit_type,
            currency=template.currency,
            tier=template.tier,
            model_id=template.model_id,
            window_type=template.window_type,
            variant=template.variant,
            health=template.health,
            sidecar_id=template.sidecar_id,
            is_unlimited=template.is_unlimited,
            data_source=template.data_source,
            error_type=template.error_type,
            raw_metadata_json=None,  # compacted marker
        )
        session.add(new_snapshot)
        session.flush()  # Get new_snapshot.id

        # Create compacted model entries
        model_numeric_fields = [
            "cost",
            "msgs",
            "tokens_input",
            "tokens_output",
            "tokens_reasoning",
            "tokens_cache_read",
            "tokens_total",
        ]
        for model_id, mrs in model_groups.items():
            m_averages = {}
            for field in model_numeric_fields:
                vals = [getattr(mr, field) for mr in mrs if getattr(mr, field) is not None]
                m_averages[field] = sum(vals) / len(vals) if vals else None

            session.add(
                UsageSnapshotModel(
                    snapshot_id=new_snapshot.id,
                    model_id=model_id,
                    cost=m_averages["cost"],
                    msgs=int(m_averages["msgs"]) if m_averages["msgs"] is not None else None,
                    tokens_input=m_averages["tokens_input"],
                    tokens_output=m_averages["tokens_output"],
                    tokens_reasoning=m_averages["tokens_reasoning"],
                    tokens_cache_read=m_averages["tokens_cache_read"],
                    tokens_total=m_averages["tokens_total"],
                )
            )

        created += 1

    session.commit()
    logger.info(f"Compaction complete: {created} buckets merged")
    return created
