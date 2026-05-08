"""Quota usage forecast service — event-sourced implementation.

Algorithm per card with unit_type='tokens' and limit_value > 0:
1. Compute window_start = reset_at - WINDOW_DURATIONS[window_type]
2. Query usage_events, group into hourly buckets, build cumulative token series
3. Convert (ts, cumulative_tokens) → (elapsed_seconds, pct_used) via limit_value
4. Run linear_regression(elapsed_seconds, pct_used) → slope + intercept
5. Project to reset_seconds → projected_pct_at_reset

Non-token-denominated unit types (percent, currency) are not supported here;
they return None. This is intentional: dividing raw token event sums by a
non-token limit_value (e.g. 100 for a %-based card) produces nonsense.
Forecasting for those card types is deferred to a future phase.
"""

import logging
from datetime import UTC, datetime, timedelta
from statistics import LinearRegression, linear_regression

from sqlalchemy import text
from sqlmodel import Session

from app.models.schemas import ForecastEntry, ForecastResponse, LimitCard

logger = logging.getLogger(__name__)

WINDOW_DURATIONS: dict[str, timedelta] = {
    "session": timedelta(hours=5),
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
}

# Minimum number of distinct hourly buckets before we trust a slope.
# With one event per hour, 2 buckets = 1 hour of history.
MIN_BUCKETS_FOR_TREND = 2

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
        variant=card.variant,
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


def _fetch_hourly_buckets(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    model_id: str | None,
    since: datetime,
    until: datetime,
) -> list[tuple[datetime, int]]:
    """Return (hour_bucket_ts, token_sum) pairs from usage_events, ordered oldest-first.

    Tokens counted: tokens_input + tokens_output + tokens_cache_read + tokens_cache_create.
    tokens_reasoning is excluded (it's a sub-type of output, already counted there).
    """
    sql = text(
        """
        SELECT
            strftime('%Y-%m-%d %H:00:00', ts) AS hour_bucket,
            SUM(tokens_input + tokens_output + tokens_cache_read + tokens_cache_create) AS toks
        FROM usage_events
        WHERE provider_id = :provider_id
          AND account_id  = :account_id
          AND ts >= :since
          AND ts <= :until
          AND (:model_id IS NULL OR model_id = :model_id)
        GROUP BY hour_bucket
        ORDER BY hour_bucket ASC
        """
    )

    # SQLite stores datetimes as naive UTC strings ("2026-05-08 17:00:00.000000").
    # Passing an ISO-8601 string with 'T' separator + '+00:00' offset breaks SQLite
    # string comparisons because 'T' (ASCII 84) > ' ' (ASCII 32), making the bound
    # compare as lexicographically larger than stored values. Strip to naive UTC.
    def _naive_utc_str(dt: datetime) -> str:
        return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

    rows = session.exec(  # type: ignore[call-overload]
        sql,
        params={
            "provider_id": provider_id,
            "account_id": account_id,
            "since": _naive_utc_str(since),
            "until": _naive_utc_str(until),
            "model_id": model_id,
        },
    ).all()

    result: list[tuple[datetime, int]] = []
    for row in rows:
        # Parse the hour bucket string to a UTC datetime
        try:
            ts = datetime.strptime(str(row.hour_bucket), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        result.append((ts, int(row.toks or 0)))
    return result


def compute_forecast(card: LimitCard, session: Session) -> ForecastEntry | None:
    """Compute a linear-regression forecast for one LimitCard.

    Returns None when:
    - card is unlimited
    - limit_value is None or <= 0
    - unit_type is not 'tokens' (non-token forecasting deferred)
    - reset_at is missing or unparseable
    - unit == 'pay-as-you-go'
    - window_type is not in WINDOW_DURATIONS
    """
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
    # Only forecast token-denominated cards. Percent/currency cards need a
    # different approach (current value already in the right unit); deferred.
    if card.unit_type not in ("tokens", "generic"):
        return None

    try:
        reset_at_dt = datetime.fromisoformat(card.reset_at)
        if reset_at_dt.tzinfo is None:
            reset_at_dt = reset_at_dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None

    now = datetime.now(UTC)
    window_duration = WINDOW_DURATIONS[card.window_type]
    window_start = reset_at_dt - window_duration

    # For rolling windows where reset_at is in the past or very near now,
    # use now as the anchor so window_start doesn't drift into the future.
    if window_start > now:
        window_start = now - window_duration

    forecast_target_dt = reset_at_dt

    # Confidence: fraction of window elapsed (0.0 = just started, 1.0 = at reset)
    total_window_secs = (forecast_target_dt - window_start).total_seconds()
    elapsed_secs = (now - window_start).total_seconds()
    confidence = max(
        0.0, min(1.0, elapsed_secs / total_window_secs if total_window_secs > 0 else 0.0)
    )

    # Fetch hourly event aggregates within the window
    buckets = _fetch_hourly_buckets(
        session,
        provider_id=card.provider_id or "",
        account_id=card.account_id or "",
        model_id=card.model_id,
        since=window_start,
        until=now,
    )

    now_used = card.used_value
    now_pct: float | None
    if card.unit_type == "percent":
        now_pct = card.used_value
    elif card.used_value is not None:
        now_pct = card.used_value / card.limit_value * 100
    else:
        now_pct = None

    if len(buckets) < MIN_BUCKETS_FOR_TREND:
        logger.debug(
            f"Forecast insufficient for {card.service_name}: "
            f"{len(buckets)} hourly buckets (need {MIN_BUCKETS_FOR_TREND})"
        )
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=len(buckets),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
        )

    # Build (elapsed_seconds_from_window_start, cumulative_pct) series
    cumulative_tokens = 0
    xs: list[float] = []
    ys: list[float] = []
    for bucket_ts, toks in buckets:
        cumulative_tokens += toks
        elapsed = (bucket_ts - window_start).total_seconds()
        pct = cumulative_tokens / card.limit_value * 100
        xs.append(elapsed)
        ys.append(pct)

    fit = _fit_linear(xs, ys)
    if fit is None:
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=len(buckets),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
        )

    slope, intercept = fit.slope, fit.intercept

    # Project to reset time
    reset_elapsed = (forecast_target_dt - window_start).total_seconds()
    projected_pct = intercept + slope * reset_elapsed

    # Clamp: projection must not be less than current pct
    current_pct = ys[-1] if ys else (now_pct or 0.0)
    projected_pct = max(projected_pct, current_pct)
    projected_used = projected_pct / 100.0 * card.limit_value

    # Stability check: if projected growth < epsilon vs. current, report stable
    is_stable = now_pct is not None and (projected_pct - now_pct) < STABLE_PCT_EPSILON
    if is_stable or (now_pct is not None and now_pct >= 99.9):
        is_exhausted = now_pct is not None and now_pct >= 99.9
        return _make_entry(
            card=card,
            status="exhausted" if is_exhausted else "stable",
            window_start=window_start,
            samples_used=len(buckets),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
            projected_used=card.limit_value if is_exhausted else projected_used,
            projected_pct=100.0 if is_exhausted else now_pct,
        )

    # Compute exact limit-hit time
    projected_limit_hit_at: str | None = None
    if projected_pct >= 100:
        if slope > 0 and (now_pct is None or now_pct < 99.9):
            # target_pct = slope * elapsed + intercept → elapsed = (100 - intercept) / slope
            hit_elapsed = (100.0 - intercept) / slope
            hit_ts = window_start + timedelta(seconds=hit_elapsed)
            if hit_ts > now:
                projected_limit_hit_at = hit_ts.isoformat()

        # Cap the visual projection at 100%
        projected_pct = 100.0
        projected_used = card.limit_value

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
        samples_used=len(buckets),
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
