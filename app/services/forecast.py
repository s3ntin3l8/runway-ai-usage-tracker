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

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median

from sqlmodel import Session

from app.models.schemas import ForecastEntry, ForecastResponse, LimitCard

logger = logging.getLogger(__name__)

WINDOW_DURATIONS: dict[str, timedelta] = {
    "session": timedelta(hours=5),
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
    "rolling": timedelta(days=30),
}

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


# Minimum number of distinct buckets before we trust a slope. Below this we
# return insufficient_data; otherwise a single recent spike skews the fit.
MIN_BUCKETS_FOR_TREND = 4

# A projection within this many percentage points of now_pct is reported as
# "stable" rather than a real forecast. Covers rounded-series noise and
# downward-clamped slopes where the "forecast" would just echo the current value.
STABLE_PCT_EPSILON = 0.1

# Status thresholds in pct_used terms.
LIMIT_PCT = 100.0
EXHAUSTED_PCT = 99.9
WARN_PCT = 80.0
# Phase 2 — projected_limit_hit_at is suppressed if the predicted hit time lies
# more than this many window-durations in the future.
HORIZON_CAP_MULTIPLIER = 2.0
# Phase 2 — status "decelerating" applies only above this current-usage floor.
DECELERATING_NOW_PCT_THRESHOLD = WARN_PCT


@dataclass(frozen=True)
class TrendFit:
    """A linear fit (slope, intercept) via Theil-Sen estimator."""

    slope: float
    intercept: float
    method: str  # always "theil_sen"


@dataclass(frozen=True)
class _NowState:
    """Card's current position."""

    used: float | None
    pct: float | None


@dataclass(frozen=True)
class _SeriesData:
    """Per-bucket cumulative-pct trajectory used to fit a trend."""

    xs: list[float]
    ys: list[float]


@dataclass(frozen=True)
class _Projection:
    """Projected position at the end of the window."""

    used: float | None = None
    pct: float | None = None
    hit_at: str | None = None


def _fit_trend(xs: list[float], ys: list[float]) -> TrendFit | None:
    """Fit a linear trend using Theil-Sen estimator (median of pairwise slopes).

    Resistant to outliers: a single spike in one bucket can't drag the slope
    the way OLS can. Intercept uses Theil-Sen's conventional `median(y_i - slope * x_i)`
    so the line `(LIMIT_PCT - intercept) / slope` for hit_at is consistent with
    the slope estimate.

    Returns None below MIN_BUCKETS_FOR_TREND points.
    """
    n = len(xs)
    if n < MIN_BUCKETS_FOR_TREND:
        return None
    slopes: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[j] - xs[i]
            if dx == 0:
                continue
            slopes.append((ys[j] - ys[i]) / dx)
    if not slopes:
        return None
    slope = median(slopes)
    intercept = median(ys[i] - slope * xs[i] for i in range(n))
    return TrendFit(slope=slope, intercept=intercept, method="theil_sen")


def _compute_hit_at(
    *,
    fit: TrendFit,
    now: datetime,
    now_pct: float | None,
    projected_pct: float,
    total_window_secs: float,
) -> str | None:
    """ISO timestamp at which the trend crosses the limit, or None.

    The line is anchored at (now, now_pct) with the regression's slope, so
    `hit_secs_from_now = (LIMIT - now_pct) / slope`. This matches the
    anchor-at-now projection used everywhere else in the pipeline.

    Suppressed when:
    - the anchored projection doesn't reach the limit
    - slope is non-positive (line would never cross 100 going forward)
    - the card is already exhausted
    - the hit lies further than HORIZON_CAP_MULTIPLIER × window into the future
    """
    if projected_pct < LIMIT_PCT:
        return None
    if fit.slope <= 0:
        return None
    if now_pct is None or now_pct >= EXHAUSTED_PCT:
        return None
    remaining_pct = LIMIT_PCT - now_pct
    if remaining_pct <= 0:
        return None
    hit_secs_from_now = remaining_pct / fit.slope
    if hit_secs_from_now > HORIZON_CAP_MULTIPLIER * total_window_secs:
        return None
    return (now + timedelta(seconds=hit_secs_from_now)).isoformat()


def _classify_status(
    *,
    now_pct: float | None,
    projected_pct: float,
    projected_pct_raw: float,
    hit_at: str | None,
) -> str:
    """Bucket a forecast into one of the status labels."""
    if now_pct is not None and now_pct >= EXHAUSTED_PCT:
        return "exhausted"
    if (
        now_pct is not None
        and now_pct >= DECELERATING_NOW_PCT_THRESHOLD
        and projected_pct_raw < now_pct
    ):
        return "decelerating"
    if now_pct is not None and (projected_pct - now_pct) < STABLE_PCT_EPSILON:
        return "stable"
    if projected_pct >= LIMIT_PCT or hit_at:
        return "risk"
    if projected_pct >= WARN_PCT:
        return "warn"
    return "ok"


def _make_entry(
    card: LimitCard,
    *,
    status: str,
    window_start: datetime,
    samples_used: int,
    confidence: float,
    now_state: _NowState,
    projection: _Projection = _Projection(),
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
        limit_value=card.limit_value,  # type: ignore[arg-type]
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
    now_state: _NowState,
    fit: TrendFit | None,
    series_data: _SeriesData,
    total_window_secs: float,
    now: datetime,
    limit_value: float,
    include_series: bool = False,
) -> ForecastEntry:
    """Apply the shared classification + clamp + hit-at pipeline."""
    series_payload = _build_series_payload(series_data, window_start) if include_series else None
    ys = series_data.ys

    if fit is None:
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=samples_used,
            confidence=confidence,
            now_state=now_state,
            series=series_payload,
        )

    now_pct = now_state.pct
    projected_pct_raw = fit.intercept + fit.slope * total_window_secs

    elapsed_now = (now - window_start).total_seconds()
    remaining_from_now = max(0.0, total_window_secs - elapsed_now)
    anchor_pct = now_pct if now_pct is not None else (ys[-1] if ys else 0.0)
    projected_pct = max(anchor_pct + fit.slope * remaining_from_now, anchor_pct)
    projected_used = projected_pct / LIMIT_PCT * limit_value

    hit_at = _compute_hit_at(
        fit=fit,
        now=now,
        now_pct=now_pct,
        projected_pct=projected_pct,
        total_window_secs=total_window_secs,
    )

    status = _classify_status(
        now_pct=now_pct,
        projected_pct=projected_pct,
        projected_pct_raw=projected_pct_raw,
        hit_at=hit_at,
    )

    if status == "exhausted":
        projection = _Projection(used=limit_value, pct=LIMIT_PCT)
    elif status == "stable":
        projection = _Projection(used=projected_used, pct=now_pct)
    elif status == "decelerating":
        projection = _Projection(used=now_state.used, pct=now_pct)
    elif status == "risk":
        projection = _Projection(used=limit_value, pct=LIMIT_PCT, hit_at=hit_at)
    else:  # warn | ok
        projection = _Projection(used=projected_used, pct=projected_pct)

    return _make_entry(
        card=card,
        status=status,
        window_start=window_start,
        samples_used=samples_used,
        confidence=confidence,
        now_state=now_state,
        projection=projection,
        slope=fit.slope,
        method=fit.method,
        series=series_payload,
    )


def _resolve_window(card: LimitCard, now: datetime) -> tuple[datetime, datetime, float] | None:
    """Parse reset_at and compute (window_start, reset_at_dt, total_window_secs)."""
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
# Value: pre-bucketed [(bucket_ts, pct_used)] sorted oldest-first.
SnapshotCache = dict[tuple[str, str, str, str, str], list[tuple[datetime, float]]]


def _snapshots_for_card(
    session: Session,
    *,
    snapshot_cache: "SnapshotCache | None",
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
        # The batch cache covers the broadest time range across all cards (driven by
        # the longest window type, e.g. monthly). Trim to [window_start, now] so
        # data from prior windows/sessions doesn't contaminate the regression —
        # the per-card SQL fallback applies the same ts >= window_start filter.
        return [
            (ts, pct)
            for ts, pct in snapshot_cache.get(key, [])
            if window_start <= ts <= now
        ]

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
    snapshot_cache: "SnapshotCache | None" = None,
    include_series: bool = False,
) -> ForecastEntry | None:
    """Forecast quota % usage from historical pct_used snapshots."""
    result = _resolve_window(card, now)
    if result is None:
        return None
    window_start, reset_at_dt, total_window_secs = result
    confidence, _elapsed = _confidence_and_elapsed(window_start, total_window_secs, now)

    now_pct = _derive_now_pct(card)
    limit_value = card.limit_value or LIMIT_PCT
    now_state = _NowState(used=card.used_value, pct=now_pct)

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
        fit=_fit_trend(xs, ys),
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
    snapshot_cache: "SnapshotCache | None" = None,
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
        # Fetch at the finest bucket resolution; coarser-window cards still get
        # the correct last-in-bucket value because they query a wider partition.
        raw_cache = query_pct_snapshot_buckets_batch(
            session,
            since=earliest_window_start,
            until=now,
            bucket_seconds=finest_bucket,
        )
        # Re-bucket per card if its window uses a coarser resolution.
        # Build the final cache keyed by (provider, account, window_type, variant, model_id)
        # with bucket_seconds appropriate for that card's window_type.
        snapshot_cache = {}
        for key, fine_buckets in raw_cache.items():
            _pid, _aid, _wt, _variant, _mid = key
            card_bucket_secs = _forecast_bucket_seconds(_wt)
            if card_bucket_secs == finest_bucket:
                snapshot_cache[key] = fine_buckets
            else:
                # Re-aggregate: keep last value per coarser bucket.
                coarse: dict[int, tuple[datetime, float]] = {}
                for bts, pct in fine_buckets:
                    epoch = int(bts.timestamp())
                    coarse_epoch = (epoch // card_bucket_secs) * card_bucket_secs
                    # fine_buckets are sorted oldest-first; later overwrites are newer.
                    coarse[coarse_epoch] = (
                        datetime.fromtimestamp(coarse_epoch, tz=UTC),
                        pct,
                    )
                snapshot_cache[key] = sorted(coarse.values())

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
