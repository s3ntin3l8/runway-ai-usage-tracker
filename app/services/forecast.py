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
    "rolling": timedelta(days=30),
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


def _resolve_window(card: LimitCard, now: datetime) -> tuple[datetime, datetime, float] | None:
    """Parse reset_at and compute (window_start, reset_at_dt, total_window_secs).

    Returns None if reset_at is missing/unparseable or window_type is unknown.
    Handles rolling window_type with a 30-day default.
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
    window_start = reset_at_dt - window_duration
    if window_start > now:
        window_start = now - window_duration
    total_window_secs = (reset_at_dt - window_start).total_seconds()
    return window_start, reset_at_dt, total_window_secs


def _confidence_and_elapsed(
    window_start: datetime, total_window_secs: float, now: datetime
) -> tuple[float, float]:
    """Return (confidence, elapsed_secs) for a window."""
    elapsed_secs = (now - window_start).total_seconds()
    confidence = max(
        0.0, min(1.0, elapsed_secs / total_window_secs if total_window_secs > 0 else 0.0)
    )
    return confidence, elapsed_secs


def compute_forecast(
    card: LimitCard, session: Session, now: datetime | None = None
) -> ForecastEntry | None:
    """Dispatch to the appropriate forecast method based on unit_type.

    `now` is threaded through every helper so all clock reads for a single
    forecast share one timestamp — otherwise separate reads could straddle a
    window boundary and skew the trajectory. Batch callers
    (`compute_all_forecasts`) should capture once and pass through.
    """
    if now is None:
        now = datetime.now(UTC)
    if card.is_unlimited:
        return None
    if card.limit_value is None or card.limit_value <= 0:
        # Allow percent cards with limit_value=100 to pass
        if card.unit_type != "percent":
            return None
    if not card.reset_at:
        return None
    if card.unit == "pay-as-you-go":
        return None
    # Normalize singular 'token' to 'tokens'
    effective_unit_type = card.unit_type
    if effective_unit_type == "token":
        effective_unit_type = "tokens"

    if effective_unit_type in ("percent",):
        return _compute_percent_forecast(card, session, effective_unit_type, now=now)
    if effective_unit_type in ("currency", "credits"):
        return _compute_currency_forecast(card, session, now=now)
    if effective_unit_type in ("tokens", "generic"):
        return _compute_token_forecast(card, session, now=now)
    # Unsupported unit types (requests, minutes, etc.) — insufficient data
    return None


def _compute_token_forecast(
    card: LimitCard, session: Session, now: datetime
) -> ForecastEntry | None:
    """Original linear-regression forecast for token-denominated cards."""
    result = _resolve_window(card, now)
    if result is None:
        return None
    window_start, reset_at_dt, total_window_secs = result
    confidence, elapsed_secs = _confidence_and_elapsed(window_start, total_window_secs, now)

    if card.limit_value is None or card.limit_value <= 0:
        return None

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
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=len(buckets),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
        )

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
    reset_elapsed = (reset_at_dt - window_start).total_seconds()
    projected_pct = intercept + slope * reset_elapsed

    current_pct = ys[-1] if ys else (now_pct or 0.0)
    projected_pct = max(projected_pct, current_pct)
    projected_used = projected_pct / 100.0 * card.limit_value

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

    projected_limit_hit_at: str | None = None
    if projected_pct >= 100:
        if slope > 0 and (now_pct is None or now_pct < 99.9):
            hit_elapsed = (100.0 - intercept) / slope
            hit_ts = window_start + timedelta(seconds=hit_elapsed)
            if hit_ts > now:
                projected_limit_hit_at = hit_ts.isoformat()
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


def _compute_percent_forecast(
    card: LimitCard, session: Session, effective_unit_type: str, now: datetime
) -> ForecastEntry | None:
    """Forecast for percent-denominated cards (unit_type='percent').

    Uses the card's own pct_used as 'now' position, then derives a consumption
    rate from hourly token usage events to project forward. For percent cards,
    we know the current gauge position and the limit (usually 100%). We
    extrapolate by computing how fast tokens are burning relative to the window.
    """
    result = _resolve_window(card, now)
    if result is None:
        return None
    window_start, reset_at_dt, total_window_secs = result
    confidence, elapsed_secs = _confidence_and_elapsed(window_start, total_window_secs, now)

    # Derive now_pct from the card
    now_pct: float | None = None
    if card.pct_used is not None:
        now_pct = card.pct_used
    elif card.used_value is not None and card.limit_value and card.limit_value > 0:
        now_pct = card.used_value / card.limit_value * 100
    elif card.used_value is not None:
        now_pct = card.used_value  # already a percentage when limit=100

    now_used = card.used_value

    # Get hourly token consumption within this window
    buckets = _fetch_hourly_buckets(
        session,
        provider_id=card.provider_id or "",
        account_id=card.account_id or "",
        model_id=card.model_id,
        since=window_start,
        until=now,
    )

    if len(buckets) < MIN_BUCKETS_FOR_TREND or now_pct is None:
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=len(buckets),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
        )

    # Build a cumulative token series and convert to pct_used trajectory
    # We know pct_used at "now" from the card. We can infer how much
    # of the window's limit each hour of token usage represents.
    cumulative_tokens = 0
    xs: list[float] = []
    ys: list[float] = []
    total_tokens_in_window = sum(toks for _, toks in buckets)
    if total_tokens_in_window == 0:
        return _make_entry(
            card=card,
            status="stable",
            window_start=window_start,
            samples_used=len(buckets),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
            projected_used=now_used,
            projected_pct=now_pct,
        )

    # Tokens per percentage point = total_tokens / pct_change_covered
    # If we're at pct_used now, total_tokens maps to pct_used pct points
    token_per_pct = total_tokens_in_window / now_pct if now_pct > 0 else 1.0

    for bucket_ts, toks in buckets:
        cumulative_tokens += toks
        elapsed = (bucket_ts - window_start).total_seconds()
        pct = cumulative_tokens / token_per_pct if token_per_pct > 0 else 0.0
        xs.append(elapsed)
        ys.append(pct)

    fit = _fit_linear(xs, ys)
    if fit is None:
        # With only 2 points, just extrapolate linearly
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
    reset_elapsed = total_window_secs
    projected_pct = intercept + slope * reset_elapsed
    current_pct = ys[-1] if ys else (now_pct or 0.0)
    projected_pct = max(projected_pct, current_pct)

    limit_value = card.limit_value or 100.0
    projected_used = projected_pct / 100.0 * limit_value

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
            projected_used=limit_value if is_exhausted else projected_used,
            projected_pct=100.0 if is_exhausted else now_pct,
        )

    projected_limit_hit_at: str | None = None
    if projected_pct >= 100:
        if slope > 0 and (now_pct is None or now_pct < 99.9):
            hit_elapsed = (100.0 - intercept) / slope
            hit_ts = window_start + timedelta(seconds=hit_elapsed)
            if hit_ts > now:
                projected_limit_hit_at = hit_ts.isoformat()
        projected_pct = 100.0
        projected_used = limit_value

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


def _compute_currency_forecast(
    card: LimitCard, session: Session, now: datetime
) -> ForecastEntry | None:
    """Forecast for currency-denominated cards (unit_type='currency' or 'credits').

    Uses daily cost_usd from period rollups to project spending trajectory.
    """
    result = _resolve_window(card, now)
    if result is None:
        return None
    window_start, reset_at_dt, total_window_secs = result
    confidence, elapsed_secs = _confidence_and_elapsed(window_start, total_window_secs, now)

    now_pct: float | None = None
    if card.pct_used is not None:
        now_pct = card.pct_used
    elif card.used_value is not None and card.limit_value and card.limit_value > 0:
        now_pct = card.used_value / card.limit_value * 100
    now_used = card.used_value
    limit_value = card.limit_value

    if limit_value is None or limit_value <= 0:
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=0,
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
        )

    # Fetch daily cost rollups within the window
    from sqlalchemy import text

    since_key = window_start.strftime("%Y-%m-%d")
    sql = text("""
        SELECT
            period_key,
            SUM(cost_usd) AS daily_cost,
            SUM(tokens_input + tokens_output + tokens_cache_read
                 + tokens_cache_create + tokens_reasoning) AS daily_tokens
        FROM usage_period_rollup
        WHERE period_type = 'day'
          AND model_id = ''
          AND sidecar_id = ''
          AND period_key >= :since_key
          AND (:provider_id IS NULL OR provider_id = :provider_id)
          AND (:account_id IS NULL OR account_id = :account_id)
        GROUP BY period_key
        ORDER BY period_key ASC
    """)
    params = {
        "since_key": since_key,
        "provider_id": card.provider_id or "",
        "account_id": card.account_id or "",
    }
    rows = session.exec(sql, params=params).all()  # type: ignore[call-overload]

    if len(rows) < MIN_BUCKETS_FOR_TREND:
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=len(rows),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
        )

    # Build (elapsed_seconds, pct_used) series from cumulative cost
    cumulative_cost = 0.0
    xs: list[float] = []
    ys: list[float] = []
    for row in rows:
        period_key, daily_cost, daily_tokens = row
        cumulative_cost += float(daily_cost or 0)
        day_ts = datetime.strptime(str(period_key), "%Y-%m-%d").replace(tzinfo=UTC)
        elapsed = (day_ts - window_start).total_seconds()
        pct = cumulative_cost / limit_value * 100
        xs.append(elapsed)
        ys.append(pct)

    fit = _fit_linear(xs, ys)
    if fit is None:
        return _make_entry(
            card=card,
            status="insufficient_data",
            window_start=window_start,
            samples_used=len(rows),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
        )

    slope, intercept = fit.slope, fit.intercept
    reset_elapsed = total_window_secs
    projected_pct = intercept + slope * reset_elapsed

    current_pct = ys[-1] if ys else (now_pct or 0.0)
    projected_pct = max(projected_pct, current_pct)
    projected_used = projected_pct / 100.0 * limit_value

    is_stable = now_pct is not None and (projected_pct - now_pct) < STABLE_PCT_EPSILON
    if is_stable or (now_pct is not None and now_pct >= 99.9):
        is_exhausted = now_pct is not None and now_pct >= 99.9
        return _make_entry(
            card=card,
            status="exhausted" if is_exhausted else "stable",
            window_start=window_start,
            samples_used=len(rows),
            confidence=confidence,
            now_used=now_used,
            now_pct=now_pct,
            projected_used=limit_value if is_exhausted else projected_used,
            projected_pct=100.0 if is_exhausted else now_pct,
        )

    projected_limit_hit_at: str | None = None
    if projected_pct >= 100:
        if slope > 0 and (now_pct is None or now_pct < 99.9):
            hit_elapsed = (100.0 - intercept) / slope
            hit_ts = window_start + timedelta(seconds=hit_elapsed)
            if hit_ts > now:
                projected_limit_hit_at = hit_ts.isoformat()
        projected_pct = 100.0
        projected_used = limit_value

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
        samples_used=len(rows),
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

    now = datetime.now(UTC)
    for card in cards:
        entry = compute_forecast(card, session, now=now)
        if entry is not None:
            forecasts.append(entry)
            if entry.status in summary:
                summary[entry.status] += 1

    return ForecastResponse(
        forecasts=forecasts,
        summary=summary,
        generated_at=now.isoformat(),
    )
