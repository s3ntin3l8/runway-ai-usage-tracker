"""Quota usage forecast service — quota-snapshot based implementation.

Algorithm per card with a derivable pct_used and limit_value > 0:
1. Compute window_start = reset_at - WINDOW_DURATIONS[window_type]
2. Query quota_snapshots for (provider, account, window_type, variant, model_id),
   filtered to reset_at = card.reset_at (current window only).
3. Bucket the snapshots at a resolution scaled to the window length (5 min for
   session, 30 min for daily, 1 h for weekly/biweekly, 6 h for monthly/rolling).
   Per bucket keep the last (newest) value — correct for a monotonic gauge.
4. Build an (elapsed_seconds, pct_used) series from the bucketed values.
5. Run Theil-Sen median regression → slope + intercept.
6. Project to reset_at anchored at (now, now_pct):
   projected_pct = max(now_pct + slope * remaining_from_now, now_pct)

Forecast is a quota concept: it tracks how fast the provider's reported pct_used
changes over time. Token-event data is not used here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlmodel import Session

from app.models.schemas import ForecastEntry, ForecastResponse, LimitCard
from app.services.forecast_shared import LIMIT_PCT, MIN_BUCKETS_FOR_TREND, NowState, Projection

if TYPE_CHECKING:
    from app.services.forecast_trend import TrendFit

logger = logging.getLogger(__name__)

WINDOW_DURATIONS: dict[str, timedelta] = {
    "session": timedelta(hours=5),
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
    "rolling": timedelta(days=30),
}

# A window whose reset_at is more than one full window-duration in the past
# has not rolled over — the provider/sidecar has stopped reporting for that
# card. Forecasting it is both meaningless (there's no "current window" to
# project) and dangerous: the snapshot cache keeps every historical point
# under that card's identity key, so an unrefreshed window_start can pull in
# months of snapshots and blow up Theil-Sen's O(n^2) pairwise-slope pass
# (observed: one stale session card with reset_at ~2 months old produced
# ~3.7k bucketed points -> ~6.9M pairwise ops -> ~2s of a dashboard load).
# Cap staleness at one window-duration so `_resolve_window` opts these cards
# out before they reach the batch query or the regression.
STALE_WINDOW_MULTIPLIER = 1.0

# Bucket resolution per window type (seconds).
_FORECAST_BUCKET_SECONDS: dict[str, int] = {
    "session": 300,  # 5 min  → up to  60 buckets
    "daily": 1800,  # 30 min → up to  48 buckets
    "weekly": 3600,  # 1 h    → up to 168 buckets
    "biweekly": 3600,  # 1 h    → up to 336 buckets
    "monthly": 21600,  # 6 h    → up to 120 buckets
    "rolling": 21600,  # 6 h    → up to 120 buckets
}
_DEFAULT_BUCKET_SECONDS = 3600  # fallback for unknown window types


def _forecast_bucket_seconds(window_type: str) -> int:
    return _FORECAST_BUCKET_SECONDS.get(window_type, _DEFAULT_BUCKET_SECONDS)


@dataclass(frozen=True)
class _SeriesData:
    """Per-bucket cumulative-pct trajectory used to fit a trend."""

    xs: list[float]
    ys: list[float]


def _make_entry(  # noqa: PLR0913
    card: LimitCard,
    *,
    status: str,
    window_start: datetime,
    samples_used: int,
    confidence: float,
    now_state: NowState,
    limit_value: float,
    projection: Projection = Projection(),
    slope: float | None = None,
    method: str = "theil_sen",
    series: list[dict[str, float | str]] | None = None,
) -> ForecastEntry:
    return ForecastEntry(
        provider_id=card.provider_id or "",
        account_id=card.account_id,
        account_label=card.account_label,
        model_id=card.model_id,
        service_name=card.service_name,
        window_type=card.window_type,
        variant=card.variant,
        unit_type=card.unit_type,
        now_used=now_state.used,
        now_pct=now_state.pct,
        projected_used=projection.used,
        projected_pct=projection.pct,
        projected_limit_hit_at=projection.hit_at,
        # The locally derived limit (falls back to LIMIT_PCT when the card
        # carries no limit_value but still has a derivable pct_used — e.g. a
        # kimi_coding monthly card) — never the raw, possibly-None card value.
        limit_value=limit_value,
        reset_at=card.reset_at,  # type: ignore[arg-type]
        window_start=window_start.isoformat(),
        samples_used=samples_used,
        confidence=confidence,
        status=status,
        method=method,
        slope=slope,
        glide_pct=confidence * LIMIT_PCT,
        series=series,
    )


def _build_series_payload(
    series_data: _SeriesData,
    window_start: datetime,
) -> list[dict[str, float | str]] | None:
    """Return the pct trajectory as [{ts, pct}, ...] for drill-down."""
    xs, ys = series_data.xs, series_data.ys
    if not xs or not ys or len(xs) != len(ys):
        return None
    return [
        {"ts": (window_start + timedelta(seconds=x)).isoformat(), "pct": float(y)}
        for x, y in zip(xs, ys, strict=False)
    ]


def _build_forecast_entry(  # noqa: PLR0913
    card: LimitCard,
    *,
    window_start: datetime,
    confidence: float,
    samples_used: int,
    now_state: NowState,
    fit: TrendFit | None,
    series_data: _SeriesData,
    total_window_secs: float,
    now: datetime,
    limit_value: float,
    include_series: bool = False,
) -> ForecastEntry:
    """Apply the shared classification + clamp + hit-at pipeline."""
    from app.services.forecast_trend import (
        classify_status,
        compute_hit_at,
        floor_projection,
        is_coarse_quantized,
        now_pct_floor_status,
    )

    series_payload = _build_series_payload(series_data, window_start) if include_series else None
    ys = series_data.ys

    # A card already near its limit is critical regardless of trend shape. This
    # floor runs before the coarse / insufficient short-circuits so a spike that
    # plateaus high never reads "Trend unavailable" or "Won't exhaust".
    floor_status = now_pct_floor_status(now_state.pct)

    if is_coarse_quantized(ys):
        return _make_entry(
            card=card,
            status=floor_status or "low_resolution",
            window_start=window_start,
            samples_used=samples_used,
            confidence=confidence if floor_status else 0.0,
            now_state=now_state,
            limit_value=limit_value,
            projection=(
                floor_projection(floor_status, now_state, limit_value)
                if floor_status
                else Projection()
            ),
            series=series_payload,
        )

    if fit is None:
        return _make_entry(
            card=card,
            status=floor_status or "insufficient_data",
            window_start=window_start,
            samples_used=samples_used,
            confidence=confidence,
            now_state=now_state,
            limit_value=limit_value,
            projection=(
                floor_projection(floor_status, now_state, limit_value)
                if floor_status
                else Projection()
            ),
            series=series_payload,
        )

    now_pct = now_state.pct
    projected_pct_raw = fit.intercept + fit.slope * total_window_secs

    elapsed_now = (now - window_start).total_seconds()
    remaining_from_now = max(0.0, total_window_secs - elapsed_now)
    anchor_pct = now_pct if now_pct is not None else (ys[-1] if ys else 0.0)
    projected_pct = max(anchor_pct + fit.slope * remaining_from_now, anchor_pct)
    projected_used = projected_pct / LIMIT_PCT * limit_value

    hit_at = compute_hit_at(
        fit=fit,
        now=now,
        now_pct=now_pct,
        projected_pct=projected_pct,
        total_window_secs=total_window_secs,
    )

    status = classify_status(
        now_pct=now_pct,
        projected_pct=projected_pct,
        projected_pct_raw=projected_pct_raw,
        hit_at=hit_at,
    )

    # Escalate near-limit cards out of the reassuring/flat verdicts ("stable" /
    # "ok" / "warn"). Left as-is: "risk" (carries a concrete hit time, more
    # useful than "near_limit"), "exhausted" (the floor's strongest outcome),
    # and "decelerating" (already high-usage-aware and not falsely reassuring).
    if floor_status is not None and status not in ("risk", "exhausted", "decelerating"):
        status = floor_status

    if status == "exhausted":
        projection = Projection(used=limit_value, pct=LIMIT_PCT)
    elif status == "near_limit":
        projection = Projection(used=now_state.used, pct=now_pct)
    elif status == "stable":
        projection = Projection(used=projected_used, pct=now_pct)
    elif status == "decelerating":
        projection = Projection(used=now_state.used, pct=now_pct)
    elif status == "risk":
        projection = Projection(used=limit_value, pct=LIMIT_PCT, hit_at=hit_at)
    else:  # warn | ok
        projection = Projection(used=projected_used, pct=projected_pct)

    return _make_entry(
        card=card,
        status=status,
        window_start=window_start,
        samples_used=samples_used,
        confidence=confidence,
        now_state=now_state,
        limit_value=limit_value,
        projection=projection,
        slope=fit.slope,
        method=fit.method,
        series=series_payload,
    )


def _resolve_window(card: LimitCard, now: datetime) -> tuple[datetime, datetime, float] | None:
    """Parse reset_at and compute (window_start, reset_at_dt, total_window_secs).

    Returns None (no forecast) for a card whose window has gone stale — see
    STALE_WINDOW_MULTIPLIER.
    """
    if not card.reset_at:
        return None
    effective_window_type = card.window_type
    if effective_window_type == "rolling":
        effective_window_type = "monthly"
    if effective_window_type not in WINDOW_DURATIONS:
        return None
    try:
        reset_at_dt = datetime.fromisoformat(card.reset_at)
        if reset_at_dt.tzinfo is None:
            reset_at_dt = reset_at_dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
    window_duration = WINDOW_DURATIONS[effective_window_type]
    if now - reset_at_dt > window_duration * STALE_WINDOW_MULTIPLIER:
        return None
    window_start = reset_at_dt - window_duration
    if window_start > now:
        window_start = now - window_duration
    total_window_secs = (reset_at_dt - window_start).total_seconds()
    return window_start, reset_at_dt, total_window_secs


def _confidence_and_elapsed(
    window_start: datetime, total_window_secs: float, now: datetime
) -> tuple[float, float]:
    elapsed_secs = (now - window_start).total_seconds()
    confidence = max(
        0.0, min(1.0, elapsed_secs / total_window_secs if total_window_secs > 0 else 0.0)
    )
    return confidence, elapsed_secs


def _derive_now_pct(card: LimitCard) -> float | None:
    """Derive the card's current pct_used from available fields."""
    if card.pct_used is not None:
        return card.pct_used
    if card.unit_type == "percent" and card.used_value is not None:
        return card.used_value
    if card.used_value is not None and card.limit_value and card.limit_value > 0:
        return card.used_value / card.limit_value * LIMIT_PCT
    return None


# Cache type for the batch optimisation path.
# Key: (provider_id, account_id, window_type, variant, model_id)
# Value: finest-resolution [(ts, pct_used)] sorted oldest-first, where `ts` is
# each bucket's real (newest) snapshot timestamp — NOT a floored label.
# `_snapshots_for_card` trims by that real ts and re-floors to the card's bucket
# width, so the boundary bucket stays in parity with the per-card SQL path.
SnapshotCache = dict[tuple[str, str, str, str, str], list[tuple[datetime, float]]]


def _snapshots_for_card(
    session: Session,
    *,
    snapshot_cache: SnapshotCache | None,
    provider_id: str,
    account_id: str,
    window_type: str,
    variant: str,
    model_id: str,
    window_start: datetime,
    now: datetime,
    bucket_seconds: int,
) -> list[tuple[datetime, float]]:
    """Return bucketed (bucket_ts, pct_used) pairs for one card.

    Uses the batch cache when available; falls back to a per-card SQL query.
    """
    if snapshot_cache is not None:
        key = (provider_id, account_id, window_type, variant, model_id)
        # The batch cache covers the broadest time range across all cards (driven
        # by the longest window type, e.g. monthly) and is bucketed at the finest
        # resolution, each point tagged with its real (newest) snapshot ts. Trim
        # by that real ts to [window_start, now] — so prior-window data is excluded
        # AND a boundary snapshot whose floored label predates window_start is
        # still kept — then re-floor to this card's bucket width, keeping the
        # newest value per bucket. This mirrors the per-card SQL path, which
        # filters raw ts >= window_start before bucketing.
        floored: dict[int, tuple[datetime, float]] = {}
        for ts, pct in snapshot_cache.get(key, []):
            if not (window_start <= ts <= now):
                continue
            bucket_epoch = (int(ts.timestamp()) // bucket_seconds) * bucket_seconds
            # Cache points are oldest-first, so the last write per bucket is newest.
            floored[bucket_epoch] = (datetime.fromtimestamp(bucket_epoch, tz=UTC), pct)
        return [floored[epoch] for epoch in sorted(floored)]

    # Per-card fallback: query quota_snapshots directly.
    from sqlalchemy import text

    def _naive_utc_str(dt: datetime) -> str:
        return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

    # ts >= window_start already excludes prior-window points; reset_at equality
    # would exclude valid snapshots whose stored reset_at has drifted by a few
    # microseconds across poll cycles (each API response carries a slightly
    # different timestamp).
    sql = text(
        """
        WITH bucketed AS (
            SELECT
                (CAST(strftime('%s', ts) AS INTEGER) / :bucket_seconds)
                    * :bucket_seconds  AS bucket_epoch,
                pct_used,
                ts,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        (CAST(strftime('%s', ts) AS INTEGER) / :bucket_seconds)
                    ORDER BY ts DESC
                ) AS rn
            FROM quota_snapshots
            WHERE provider_id  = :provider_id
              AND account_id   = :account_id
              AND window_type  = :window_type
              AND COALESCE(variant,  '') = :variant
              AND COALESCE(model_id, '') = :model_id
              AND ts           >= :since
              AND ts           <= :until
              AND pct_used IS NOT NULL
        )
        SELECT bucket_epoch, pct_used
        FROM bucketed
        WHERE rn = 1
        ORDER BY bucket_epoch ASC
        """
    )
    rows = session.exec(  # type: ignore[call-overload]
        sql,
        params={
            "provider_id": provider_id,
            "account_id": account_id,
            "window_type": window_type,
            "variant": variant,
            "model_id": model_id,
            "since": _naive_utc_str(window_start),
            "until": _naive_utc_str(now),
            "bucket_seconds": bucket_seconds,
        },
    ).all()

    result: list[tuple[datetime, float]] = []
    for row in rows:
        bucket_ts = datetime.fromtimestamp(int(row.bucket_epoch), tz=UTC)
        result.append((bucket_ts, float(row.pct_used)))
    return result


def _compute_quota_forecast(  # noqa: PLR0912
    card: LimitCard,
    session: Session,
    now: datetime,
    *,
    snapshot_cache: SnapshotCache | None = None,
    include_series: bool = False,
) -> ForecastEntry | None:
    """Forecast quota % usage from historical pct_used snapshots."""
    from app.services.forecast_trend import fit_trend

    result = _resolve_window(card, now)
    if result is None:
        return None
    window_start, reset_at_dt, total_window_secs = result
    confidence, _elapsed = _confidence_and_elapsed(window_start, total_window_secs, now)

    now_pct = _derive_now_pct(card)
    limit_value = card.limit_value or LIMIT_PCT
    now_state = NowState(used=card.used_value, pct=now_pct)

    bucket_secs = _forecast_bucket_seconds(card.window_type)
    variant = card.variant or ""
    model_id = card.model_id or ""

    buckets = _snapshots_for_card(
        session,
        snapshot_cache=snapshot_cache,
        provider_id=card.provider_id or "",
        account_id=card.account_id or "",
        window_type=card.window_type,
        variant=variant,
        model_id=model_id,
        window_start=window_start,
        now=now,
        bucket_seconds=bucket_secs,
    )

    if len(buckets) < MIN_BUCKETS_FOR_TREND:
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=len(buckets),
            confidence=confidence,
            now_state=now_state,
            limit_value=limit_value,
        )

    xs: list[float] = []
    ys: list[float] = []
    for bucket_ts, pct in buckets:
        elapsed = (bucket_ts - window_start).total_seconds()
        xs.append(elapsed)
        ys.append(pct)

    return _build_forecast_entry(
        card,
        window_start=window_start,
        confidence=confidence,
        samples_used=len(buckets),
        now_state=now_state,
        fit=fit_trend(xs, ys),
        series_data=_SeriesData(xs=xs, ys=ys),
        total_window_secs=total_window_secs,
        now=now,
        limit_value=limit_value,
        include_series=include_series,
    )


def compute_forecast(
    card: LimitCard,
    session: Session,
    now: datetime | None = None,
    *,
    snapshot_cache: SnapshotCache | None = None,
    include_series: bool = False,
) -> ForecastEntry | None:
    """Compute quota forecast for one card.

    `now` is threaded through so all clock reads for a single forecast share
    one timestamp. Batch callers (`compute_all_forecasts`) should capture once
    and pass through.

    `snapshot_cache`, when provided, replaces per-card SQL with in-memory
    lookup — populated by `compute_all_forecasts` to avoid N+1.
    """
    if now is None:
        now = datetime.now(UTC)
    if card.is_unlimited:
        return None
    if not card.reset_at:
        return None
    if card.unit == "pay-as-you-go":
        return None
    # Without a limit_value and with no derivable pct, there is no % basis for forecasting.
    if card.limit_value is None and _derive_now_pct(card) is None:
        return None
    return _compute_quota_forecast(
        card, session, now, snapshot_cache=snapshot_cache, include_series=include_series
    )


def compute_all_forecasts(cards: list[LimitCard], session: Session) -> ForecastResponse:
    from app.services.queries.forecast import query_pct_snapshot_buckets_batch

    forecasts: list[ForecastEntry] = []
    summary: dict[str, int] = {
        "risk": 0,
        "warn": 0,
        "ok": 0,
        "insufficient_data": 0,
        "stable": 0,
        "exhausted": 0,
        "decelerating": 0,
        "low_resolution": 0,
        "near_limit": 0,
    }

    now = datetime.now(UTC)

    # Determine the finest bucket size and the earliest window_start across
    # all eligible cards to bound the snapshot batch query.
    earliest_window_start: datetime | None = None
    finest_bucket: int = _DEFAULT_BUCKET_SECONDS
    for card in cards:
        if card.is_unlimited or not card.reset_at:
            continue
        win = _resolve_window(card, now)
        if win is None:
            continue
        ws = win[0]
        if earliest_window_start is None or ws < earliest_window_start:
            earliest_window_start = ws
        b = _forecast_bucket_seconds(card.window_type)
        finest_bucket = min(finest_bucket, b)

    snapshot_cache: SnapshotCache | None = None
    if earliest_window_start is not None:
        # Fetch at the finest bucket resolution, tagged with each bucket's real
        # (newest) snapshot timestamp. `_snapshots_for_card` does the per-card
        # window trim and re-floors to that card's coarser bucket width, so no
        # re-aggregation is needed here — and trimming on the real ts (not a
        # floored label) keeps the boundary bucket in parity with the per-card
        # SQL path.
        snapshot_cache = query_pct_snapshot_buckets_batch(
            session,
            since=earliest_window_start,
            until=now,
            bucket_seconds=finest_bucket,
        )

    for card in cards:
        entry = compute_forecast(card, session, now=now, snapshot_cache=snapshot_cache)
        if entry is not None:
            forecasts.append(entry)
            summary[entry.status] = summary.get(entry.status, 0) + 1

    return ForecastResponse(
        forecasts=forecasts,
        summary=summary,
        generated_at=now.isoformat(),
    )


# Names moved to forecast_trend. Bound lazily so this module does not import
# forecast_trend at module scope (callers that only need compute_forecast stay
# light). Shared thresholds live in forecast_shared, which both modules import.
_TREND_EXPORTS = frozenset(
    {
        "COARSE_STEP_PCT_THRESHOLD",
        "DECELERATING_NOW_PCT_THRESHOLD",
        "EXHAUSTED_PCT",
        "HORIZON_CAP_MULTIPLIER",
        "MAX_UNIQUE_VALS_FOR_COARSE",
        "NEAR_LIMIT_PCT",
        "STABLE_PCT_EPSILON",
        "WARN_PCT",
        "TrendFit",
        "classify_status",
        "compute_hit_at",
        "fit_trend",
        "floor_projection",
        "is_coarse_quantized",
        "now_pct_floor_status",
    }
)


def __getattr__(name: str) -> object:
    if name not in _TREND_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from app.services import forecast_trend

    value = getattr(forecast_trend, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _TREND_EXPORTS)
