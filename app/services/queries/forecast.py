"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

import calendar
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from app.models.db import UsagePeriodRollup

# Card identity key for the snapshot batch cache:
# (provider_id, account_id, window_type, variant, model_id)
SnapshotCacheKey = tuple[str, str, str, str, str]


def query_pct_snapshot_buckets_batch(
    session: Session,
    *,
    since: datetime,
    until: datetime,
    bucket_seconds: int,
) -> dict[SnapshotCacheKey, list[tuple[datetime, float]]]:
    """All pct_used snapshots in [since, until], bucketed and partitioned by card identity.

    Single scan; Python-side partitioning. Each bucket keeps the last (newest)
    snapshot value in the interval — correct for a monotonically increasing gauge.

    The returned timestamp is the kept snapshot's *real* ts (not the floored
    bucket label). Consumers re-floor to their own bucket width; carrying the
    real ts lets the per-card window trim (``ts >= window_start``) rescue a
    boundary snapshot whose floored label predates ``window_start`` — matching
    the per-card SQL path, which filters raw ts before bucketing.

    Returns: {(provider_id, account_id, window_type, variant, model_id): [(ts, pct_used)]}
    """

    def _naive_utc_str(dt: datetime) -> str:
        return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

    sql = text(
        """
        WITH bucketed AS (
            SELECT
                provider_id,
                account_id,
                window_type,
                COALESCE(variant, '')  AS variant,
                COALESCE(model_id, '') AS model_id,
                (CAST(strftime('%s', ts) AS INTEGER) / :bucket_seconds)
                    * :bucket_seconds             AS bucket_epoch,
                pct_used,
                ts,
                ROW_NUMBER() OVER (
                    PARTITION BY provider_id, account_id, window_type,
                                 COALESCE(variant, ''), COALESCE(model_id, ''),
                                 (CAST(strftime('%s', ts) AS INTEGER) / :bucket_seconds)
                    ORDER BY ts DESC
                ) AS rn
            FROM quota_snapshots
            WHERE ts >= :since AND ts <= :until
              AND pct_used IS NOT NULL
        )
        SELECT provider_id, account_id, window_type, variant, model_id,
               CAST(strftime('%s', ts) AS INTEGER) AS ts_epoch, pct_used
        FROM bucketed
        WHERE rn = 1
        ORDER BY provider_id, account_id, window_type, variant, model_id, bucket_epoch ASC
        """
    )
    rows = session.exec(  # type: ignore[call-overload]
        sql,
        params={
            "since": _naive_utc_str(since),
            "until": _naive_utc_str(until),
            "bucket_seconds": bucket_seconds,
        },
    ).all()

    result: dict[SnapshotCacheKey, list[tuple[datetime, float]]] = {}
    for row in rows:
        key: SnapshotCacheKey = (
            str(row.provider_id),
            str(row.account_id),
            str(row.window_type),
            str(row.variant),
            str(row.model_id),
        )
        snapshot_ts = datetime.fromtimestamp(int(row.ts_epoch), tz=UTC)
        result.setdefault(key, []).append((snapshot_ts, float(row.pct_used)))
    return result


def query_cost_forecast(
    session: Session,
    *,
    provider_id: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Return a cost forecast combining current MTD with 7-day burn average.

    Algorithm:
    - MTD: sum cost_usd from period_type=month, model_id='', sidecar_id='' for current month.
    - 7d avg: sum cost_usd from period_type=day, model_id='', sidecar_id='' for past 7 days
              divided by 7 (always divides by 7, zero-filling missing days).
    - projected_eom = MTD + (daily_avg × days_remaining).
    """
    now = datetime.now(UTC)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    day_of_month = now.day
    days_remaining = days_in_month - day_of_month
    month_key = now.strftime("%Y-%m")
    seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    # Fetch current-month rollup rows (all-up grain: model_id='', sidecar_id='')
    mtd_stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == "month",
        UsagePeriodRollup.period_key == month_key,
        UsagePeriodRollup.model_id == "",
        UsagePeriodRollup.sidecar_id == "",
    )
    if provider_id:
        mtd_stmt = mtd_stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        mtd_stmt = mtd_stmt.where(UsagePeriodRollup.account_id == account_id)
    mtd_rows = list(session.exec(mtd_stmt).all())

    # Fetch last-7-days daily rollup rows (all-up grain)
    daily_stmt = select(UsagePeriodRollup).where(
        UsagePeriodRollup.period_type == "day",
        UsagePeriodRollup.model_id == "",
        UsagePeriodRollup.sidecar_id == "",
        UsagePeriodRollup.period_key >= seven_days_ago,
    )
    if provider_id:
        daily_stmt = daily_stmt.where(UsagePeriodRollup.provider_id == provider_id)
    if account_id:
        daily_stmt = daily_stmt.where(UsagePeriodRollup.account_id == account_id)
    daily_rows = list(session.exec(daily_stmt).all())

    # Group by (provider_id, account_id)
    mtd_by_account: dict[tuple[str, str], float] = {}
    for r in mtd_rows:
        key: tuple[str, str] = (r.provider_id, r.account_id)
        mtd_by_account[key] = mtd_by_account.get(key, 0.0) + r.cost_usd

    daily_sum_by_account: dict[tuple[str, str], float] = {}
    for r in daily_rows:
        key = (r.provider_id, r.account_id)
        daily_sum_by_account[key] = daily_sum_by_account.get(key, 0.0) + r.cost_usd

    # Build per-account breakdown
    all_keys: set[tuple[str, str]] = set(mtd_by_account.keys()) | set(daily_sum_by_account.keys())
    by_provider: list[dict[str, Any]] = []
    total_mtd = 0.0
    total_7d_sum = 0.0

    for key in sorted(all_keys):
        pid, aid = key
        mtd = mtd_by_account.get(key, 0.0)
        seven_d_sum = daily_sum_by_account.get(key, 0.0)
        daily_avg = seven_d_sum / 7.0
        projected = mtd + daily_avg * days_remaining if daily_avg > 0 else mtd
        by_provider.append(
            {
                "provider_id": pid,
                "account_id": aid,
                "current_month_to_date": round(mtd, 6),
                "daily_burn_avg_7d": round(daily_avg, 6),
                "projected_eom": round(projected, 6),
            }
        )
        total_mtd += mtd
        total_7d_sum += seven_d_sum

    total_daily_avg = total_7d_sum / 7.0
    total_projected = (
        total_mtd + total_daily_avg * days_remaining if total_daily_avg > 0 else total_mtd
    )

    return {
        "as_of": now.isoformat(),
        "current_month_to_date": round(total_mtd, 6),
        "daily_burn_avg_7d": round(total_daily_avg, 6),
        "projected_eom": round(total_projected, 6),
        "days_in_month": days_in_month,
        "day_of_month": day_of_month,
        "days_remaining": days_remaining,
        "by_provider": by_provider,
    }


# ---------------------------------------------------------------------------
# 14.3  query_anomalies
# ---------------------------------------------------------------------------
