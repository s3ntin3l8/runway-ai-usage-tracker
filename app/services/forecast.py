import logging
from datetime import UTC, datetime, timedelta
from statistics import LinearRegression, linear_regression

from sqlmodel import Session, asc, select

from app.models.db import UsageSnapshot
from app.models.schemas import ForecastEntry, ForecastResponse, LimitCard

logger = logging.getLogger(__name__)

WINDOW_DURATIONS: dict[str, timedelta] = {
    "session": timedelta(hours=5),
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
}

# Minimum snapshot count before we trust a slope. With a 15-min poll cadence,
# 4 samples ≈ 45–60 min of history — enough to distinguish "genuinely flat"
# from "too early to tell".
MIN_SAMPLES_FOR_TREND = 4

# A projection within this many percentage points of now_pct is reported as
# "stable" rather than a real forecast. Covers rounded-series noise and
# downward-clamped slopes where the "forecast" would just echo the current value.
STABLE_PCT_EPSILON = 0.1


def _fit_linear(xs: list[float], ys: list[float]) -> LinearRegression | None:
    if len(xs) < 2:
        return None
    return linear_regression(xs, ys)


def _coerce_utc_timestamp(dt: datetime) -> float:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).timestamp()
    return dt.timestamp()


def _make_entry(
    card: LimitCard,
    *,
    status: str,
    window_start: datetime,
    samples_used: int,
    confidence: float,
    now_used: float | None,
    now_pct: float | None,
    projected_used: float | None = None,
    projected_pct: float | None = None,
    projected_limit_hit_at: str | None = None,
) -> ForecastEntry:
    return ForecastEntry(
        provider_id=card.provider_id or "",
        account_id=card.account_id,
        account_label=card.account_label,
        model_id=card.model_id,
        service_name=card.service_name,
        window_type=card.window_type,
        unit_type=card.unit_type,
        now_used=now_used,
        now_pct=now_pct,
        projected_used=projected_used,
        projected_pct=projected_pct,
        projected_limit_hit_at=projected_limit_hit_at,
        limit_value=card.limit_value,  # type: ignore[arg-type]  # caller verified non-None
        reset_at=card.reset_at,  # type: ignore[arg-type]  # caller verified non-None
        window_start=window_start.isoformat(),
        samples_used=samples_used,
        confidence=confidence,
        status=status,
        method="linear",
    )


def compute_forecast(card: LimitCard, session: Session) -> ForecastEntry | None:
    if card.is_unlimited:
        return None
    if card.limit_value is None or card.limit_value <= 0:
        return None
    if not card.reset_at:
        return None
    if card.unit == "pay-as-you-go":
        return None
    if card.window_type not in WINDOW_DURATIONS:
        return None

    try:
        if card.reset_at:
            reset_at_dt = datetime.fromisoformat(card.reset_at)
            if reset_at_dt.tzinfo is None:
                reset_at_dt = reset_at_dt.replace(tzinfo=UTC)
        else:
            # Missing reset_at. Assume rolling window relative to now.
            reset_at_dt = datetime.now(UTC)
    except (ValueError, TypeError):
        return None

    now = datetime.now(UTC)
    window_duration = WINDOW_DURATIONS[card.window_type]

    # Determine window_start and the target projection date.
    # For fixed windows, reset_at is typically the end of the window.
    # For rolling windows (like Claude's 7-day or 5-hour limits), reset_at is often
    # the time the *oldest* usage drops off, which can be near 'now' or even in the past.
    if reset_at_dt < now + (window_duration * 0.1):
        # Treat as a rolling window: analyze the last full window duration,
        # and project forward by a full window duration from now.
        window_start = now - window_duration
        forecast_target_dt = now + window_duration
    else:
        window_start = reset_at_dt - window_duration
        forecast_target_dt = reset_at_dt

    # Trend start: prioritize recent data for the slope calculation to capture
    # current activity bursts (e.g. usage this morning) without being diluted
    # by days of flat history.
    trend_start = max(window_start, now - timedelta(hours=24))

    stmt = (
        select(UsageSnapshot)
        .where(
            UsageSnapshot.provider_id == (card.provider_id or ""),
            UsageSnapshot.account_id == (card.account_id or ""),
            UsageSnapshot.window_type == card.window_type,
            UsageSnapshot.unit_type == card.unit_type,
            UsageSnapshot.model_id == card.model_id
            if card.model_id is not None
            else UsageSnapshot.model_id.is_(None),
            UsageSnapshot.timestamp >= trend_start,
        )
        .order_by(asc(UsageSnapshot.timestamp))
    )
    rows = session.exec(stmt).all()
    valid_rows = [r for r in rows if r.used_value is not None]

    logger.debug(
        f"Forecast for {card.service_name}: found {len(valid_rows)}/{len(rows)} valid snapshots "
        f"(need {MIN_SAMPLES_FOR_TREND} for trend)"
    )

    total_window_secs = (forecast_target_dt - window_start).total_seconds()
    elapsed_secs = (now - window_start).total_seconds()
    # window_progress: fraction of the reset window that has elapsed (0.0 = start, 1.0 = reset)
    confidence = max(
        0.0, min(1.0, elapsed_secs / total_window_secs if total_window_secs > 0 else 0.0)
    )

    now_used = card.used_value
    if card.unit_type == "percent":
        now_pct = card.used_value
    elif card.used_value is not None:
        now_pct = card.used_value / card.limit_value * 100
    else:
        now_pct = None

    if len(valid_rows) < MIN_SAMPLES_FOR_TREND:
        logger.warning(
            f"Forecast unavailable for {card.service_name}: only {len(valid_rows)} snapshots "
            f"(need {MIN_SAMPLES_FOR_TREND}). Wait for more collection cycles."
        )
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=len(valid_rows),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
            projected_used=None,
            projected_pct=None,
        )

    xs = [_coerce_utc_timestamp(r.timestamp) for r in valid_rows]
    ys = [r.used_value for r in valid_rows if r.used_value is not None]

    fit = _fit_linear(xs, ys)
    if fit is None:
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=len(valid_rows),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
            projected_used=None,
            projected_pct=None,
        )

    slope, intercept = fit.slope, fit.intercept
    current_used_value = valid_rows[-1].used_value
    assert current_used_value is not None  # valid_rows filtered to non-None used_value

    projected_used = intercept + slope * forecast_target_dt.timestamp()
    # Clamp against both the last snapshot and the card's live value to avoid projecting downward
    clamp_floor = max(current_used_value, card.used_value or 0.0)
    projected_used = max(projected_used, clamp_floor)

    if card.unit_type == "percent":
        projected_pct = projected_used
        target_val = 100.0
    else:
        projected_pct = projected_used / card.limit_value * 100
        target_val = card.limit_value

    # Stability check MUST happen before capping, so we detect real trends
    # even if they are projected to hit the limit soon.
    is_stable = now_pct is not None and projected_pct - now_pct < STABLE_PCT_EPSILON
    if is_stable or (now_pct is not None and now_pct >= 99.9):
        # Already at 100% or stable -> no projection necessary
        is_exhausted = now_pct is not None and now_pct >= 99.9
        return _make_entry(
            card=card,
            status="exhausted" if is_exhausted else "stable",
            window_start=window_start,
            samples_used=len(valid_rows),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
            projected_used=target_val if is_exhausted else projected_used,
            projected_pct=100.0 if is_exhausted else now_pct,
        )

    # Calculate exact time the trend hits the limit
    projected_limit_hit_at = None
    if projected_pct >= 100:
        if slope > 0 and (now_pct is None or now_pct < 99.9):
            hit_timestamp = (target_val - intercept) / slope
            # Only report if the hit time is in the future
            if hit_timestamp > now.timestamp():
                projected_limit_hit_at = datetime.fromtimestamp(hit_timestamp, tz=UTC).isoformat()

        # Cap the visual projection at 100%
        projected_pct = 100.0
        projected_used = target_val

    if projected_pct >= 100 or projected_limit_hit_at:
        status = "risk"
    elif projected_pct >= 80:
        status = "warn"
    else:
        status = "ok"

    return _make_entry(
        card=card,
        status=status,
        window_start=window_start,
        samples_used=len(valid_rows),
        confidence=confidence,
        now_used=now_used,
        now_pct=now_pct,
        projected_used=projected_used,
        projected_pct=projected_pct,
        projected_limit_hit_at=projected_limit_hit_at,
    )


def compute_all_forecasts(cards: list[LimitCard], session: Session) -> ForecastResponse:
    forecasts: list[ForecastEntry] = []
    summary: dict[str, int] = {
        "risk": 0,
        "warn": 0,
        "ok": 0,
        "insufficient_data": 0,
        "stable": 0,
        "exhausted": 0,
    }

    for card in cards:
        entry = compute_forecast(card, session)
        if entry is not None:
            forecasts.append(entry)
            if entry.status in summary:
                summary[entry.status] += 1

    return ForecastResponse(
        forecasts=forecasts,
        summary=summary,
        generated_at=datetime.now(UTC).isoformat(),
    )
