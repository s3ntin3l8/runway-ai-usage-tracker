"""Trend fitting for quota forecasts.

Theil-Sen slope, coarse-quantization detection, limit hit time, and status
classification. Snapshot loading and card assembly stay in ``forecast``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from app.services.forecast_shared import LIMIT_PCT, MIN_BUCKETS_FOR_TREND, _NowState, _Projection

# Snapshots quantized to coarse steps (e.g. Antigravity reports pct_used in
# ~20% increments) make trend fitting unreliable: Theil-Sen sees long plateaus
# → slope=0 → "stable" / "Won't exhaust", which is misleading because the next
# 20% jump could happen any moment. Detect this shape and short-circuit to
# a "low_resolution" status that suppresses the trend line.
COARSE_STEP_PCT_THRESHOLD = 10.0  # min non-zero gap (pct) between distinct values
MAX_UNIQUE_VALS_FOR_COARSE = 5  # at most this many distinct levels across the window

# A projection within this many percentage points of now_pct is reported as
# "stable" rather than a real forecast. Covers rounded-series noise and
# downward-clamped slopes where the "forecast" would just echo the current value.
STABLE_PCT_EPSILON = 0.1

# Status thresholds in pct_used terms.
EXHAUSTED_PCT = 99.9
# A card already this full is reported as "near_limit" regardless of trend
# shape. At this usage level the recent slope is moot — a flat/coarse/sparse
# trend must not read as the reassuring "Won't exhaust" (stable) or
# "Trend unavailable" (low_resolution): the card is nearly out of quota.
# Deliberately set above WARN_PCT (80): at >=90% used the verdict is alarming
# regardless of how much window remains, whereas an 80% card late in its window
# can genuinely coast to reset without exhausting. The 80-89 band keeps the
# trend-based classification (a *flat* card there is still "stable"); we don't
# escalate it from current pace/elapsed ratio because that reintroduces the
# velocity-forward false alarms the trend fit exists to avoid.
NEAR_LIMIT_PCT = 90.0
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


def _is_coarse_quantized(ys: Sequence[float]) -> bool:
    """True when snapshots are step-quantized into coarse buckets.

    All three conditions must hold:
    - few distinct values across the window (≤ MAX_UNIQUE_VALS_FOR_COARSE),
    - the smallest non-zero gap between sorted distinct values is ≥
      COARSE_STEP_PCT_THRESHOLD (each level is a step, not a fine increment), and
    - at least one consecutive plateau (zero adjacent delta) — distinguishes a
      provider that reports {0, 20, 40, 60} as long plateaus from a steep
      monotonic series that happens to land on those same four values.

    Antigravity reports pct_used in 20% increments and snapshots hold the same
    value for many buckets, hitting all three.
    """
    if len(ys) < MIN_BUCKETS_FOR_TREND:
        return False
    distinct = sorted({round(y, 1) for y in ys})
    if len(distinct) > MAX_UNIQUE_VALS_FOR_COARSE:
        return False
    gaps = [b - a for a, b in zip(distinct, distinct[1:], strict=False) if b - a > 0.1]
    if not gaps or min(gaps) < COARSE_STEP_PCT_THRESHOLD:
        return False
    return any(abs(b - a) < 0.1 for a, b in zip(ys, ys[1:], strict=False))


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


def _now_pct_floor_status(now_pct: float | None) -> str | None:
    """Status dictated purely by how full the card already is, or None.

    A card already at/near its limit is critical no matter what the recent
    trend looks like — including when the trend is flat, coarse-quantized, or
    too sparse to fit. This floor sits *above* the trend short-circuits in
    `_build_forecast_entry` because `low_resolution` / `insufficient_data`
    return before `_classify_status` ever sees `now_pct`; a guard placed only
    in the classifier would miss those paths. Returns None when the card is not
    yet near its limit, leaving the trend-based classification untouched.
    """
    if now_pct is None:
        return None
    if now_pct >= EXHAUSTED_PCT:
        return "exhausted"
    if now_pct >= NEAR_LIMIT_PCT:
        return "near_limit"
    return None


def _floor_projection(status: str, now_state: _NowState, limit_value: float) -> _Projection:
    """Projection for a now_pct-floored status (`exhausted` / `near_limit`)."""
    if status == "exhausted":
        return _Projection(used=limit_value, pct=LIMIT_PCT)
    return _Projection(used=now_state.used, pct=now_state.pct)


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
