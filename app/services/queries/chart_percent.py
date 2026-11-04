"""Percent-metric chart series for `query_chart`.

Quota-snapshot buckets, plus a `latest_usage` seed point when a series
has no snapshots yet. Moved verbatim out of `snapshots.query_chart`.
"""

import json as _json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlmodel import Session, select

from app.models._datetime import iso_utc
from app.services.queries.snapshots import _bucket_seconds_for, _parse_sqlite_dt

_CHART_PERCENT_SQL = text(
    # Bucket aggregate is MAX(pct_used), not the latest sample in the bucket.
    # A daily-resetting quota whose reset falls near the end of the UTC bucket
    # (e.g. Gemini Pro at ~21:08 UTC) would otherwise read 0 for the whole day —
    # peak captures the actual usage the user is asking the chart about.
    """
    SELECT
        MIN(ts)       AS ts,
        MAX(pct_used) AS pct_used,
        provider_id, account_id, window_type, model_id
    FROM quota_snapshots
    WHERE ts >= :since
      AND pct_used IS NOT NULL
      AND (:provider_id IS NULL OR provider_id = :provider_id)
      AND (:account_id  IS NULL OR account_id  = :account_id)
    GROUP BY provider_id, account_id, window_type, model_id,
             (CAST(strftime('%s', ts) AS INTEGER) / :bucket_seconds)
    ORDER BY provider_id, window_type, model_id, ts ASC
    """
)


def query_chart_percent(
    session: Session,
    *,
    provider_id: str | None,
    account_id: str | None,
    days: float,
    since: datetime,
) -> dict:
    # Bucket snapshots by time at the SQL layer to keep chart resolution
    # matched to the timeframe (see `_BUCKET_TIERS`). The `pct_used IS NOT
    # NULL` filter lives inside the bucketed CTE so a null row can never
    # be selected as the bucket representative.
    bucket_seconds = _bucket_seconds_for(days)
    chart_params = {
        "since": since.replace(tzinfo=None),
        "provider_id": provider_id,
        "account_id": account_id,
        "bucket_seconds": bucket_seconds,
    }
    snaps = session.exec(_CHART_PERCENT_SQL, params=chart_params).all()  # type: ignore[call-overload]

    series_map: dict[str, dict] = {}
    for s in snaps:
        use_model = s.model_id or ""
        key = f"{s.provider_id}::{s.window_type}::{use_model}"
        if key not in series_map:
            label = f"{s.provider_id.capitalize()} · {s.window_type.capitalize()}"
            if use_model:
                label += f" · {use_model}"
            series_map[key] = {
                "key": key,
                "provider_id": s.provider_id,
                "window_type": s.window_type,
                "model_id": use_model,
                "label": label,
                "color_hint": s.provider_id,
                "points": [],
            }
        ts_dt = _parse_sqlite_dt(s.ts)
        ts_iso = iso_utc(ts_dt) if ts_dt is not None else str(s.ts)
        series_map[key]["points"].append({"ts": ts_iso, "pct_used": s.pct_used})

    # Seed any provider/window_type that has current pct_used data in latest_usage
    # but no snapshots yet (e.g. first run after schema migration).
    from app.models.db import LatestUsage

    lu_all = session.exec(select(LatestUsage).where(LatestUsage.model_id == "")).all()
    now_iso = datetime.now(UTC).isoformat()
    for lu in lu_all:
        if provider_id and lu.provider_id != provider_id:
            continue
        if account_id and lu.account_id != account_id:
            continue
        try:
            card = _json.loads(lu.card_json)
        except Exception:
            continue
        # Derive pct_used (same logic as accumulator)
        pct: float | None = card.get("pct_used")
        if pct is None:
            used = card.get("used_value")
            if used is not None:
                if card.get("unit_type") == "percent":
                    pct = float(used)
                else:
                    lim = card.get("limit_value")
                    if lim and lim > 0:
                        pct = (used / lim) * 100.0
        if pct is None:
            continue
        wt = lu.window_type
        use_model = lu.model_id or ""
        key = f"{lu.provider_id}::{wt}::{use_model}"
        if key not in series_map:
            label = f"{lu.provider_id.capitalize()} · {wt.capitalize()}"
            series_map[key] = {
                "key": key,
                "provider_id": lu.provider_id,
                "window_type": wt,
                "model_id": use_model,
                "label": label,
                "color_hint": lu.provider_id,
                "points": [],
            }
        # Only add seed point if this series has no snapshot yet (avoid duplicating last value)
        if not series_map[key]["points"]:
            series_map[key]["points"].append({"ts": now_iso, "pct_used": pct})

    return {"series": list(series_map.values())}
