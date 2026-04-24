from datetime import UTC, datetime, timedelta
from statistics import LinearRegression, linear_regression

from sqlmodel import Session, asc, select

from app.models.db import UsageSnapshot
from app.models.schemas import ForecastEntry, ForecastResponse, LimitCard

WINDOW_DURATIONS: dict[str, timedelta] = {
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
}


def _fit_linear(xs: list[float], ys: list[float]) -> LinearRegression | None:
    if len(xs) < 2:
        return None
    return linear_regression(xs, ys)


def _coerce_utc_timestamp(dt: datetime) -> float:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).timestamp()
    return dt.timestamp()


def compute_forecast(card: LimitCard, session: Session) -> ForecastEntry | None:
    if card.is_unlimited:
        return None
    if card.limit_value is None or card.limit_value <= 0:
        return None
    if not card.reset_at:
        return None
    if card.unit == "pay-as-you-go":
        return None
    if card.window_type == "session":
        return None
    if card.window_type not in WINDOW_DURATIONS:
        return None

    try:
        reset_at_dt = datetime.fromisoformat(card.reset_at)
        if reset_at_dt.tzinfo is None:
            reset_at_dt = reset_at_dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None

    now = datetime.now(UTC)
    if reset_at_dt <= now + timedelta(seconds=60):
        return None

    window_duration = WINDOW_DURATIONS[card.window_type]
    window_start = reset_at_dt - window_duration

    provider_id = card.provider_id or ""
    account_id = card.account_id or ""

    stmt = (
        select(UsageSnapshot)
        .where(
            UsageSnapshot.provider_id == provider_id,
            UsageSnapshot.account_id == account_id,
            UsageSnapshot.service_name == card.service_name,
            UsageSnapshot.model_id == card.model_id,
            UsageSnapshot.window_type == card.window_type,
            UsageSnapshot.unit_type == card.unit_type,
            UsageSnapshot.timestamp >= window_start,
        )
        .order_by(asc(UsageSnapshot.timestamp))
    )
    rows = session.exec(stmt).all()

    total_window_secs = (reset_at_dt - window_start).total_seconds()
    elapsed_secs = (now - window_start).total_seconds()
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

    if len(rows) < 2:
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
            projected_used=None,
            projected_pct=None,
            limit_value=card.limit_value,
            reset_at=card.reset_at,
            window_start=window_start.isoformat(),
            samples_used=len(rows),
            confidence=confidence,
            status="insufficient_data",
            method="linear",
        )

    valid_rows = [r for r in rows if r.used_value is not None]
    if len(valid_rows) < 2:
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
            projected_used=None,
            projected_pct=None,
            limit_value=card.limit_value,
            reset_at=card.reset_at,
            window_start=window_start.isoformat(),
            samples_used=len(rows),
            confidence=confidence,
            status="insufficient_data",
            method="linear",
        )

    xs = [_coerce_utc_timestamp(r.timestamp) for r in valid_rows]
    ys = [r.used_value for r in valid_rows if r.used_value is not None]

    fit = _fit_linear(xs, ys)
    if fit is None:
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
            projected_used=None,
            projected_pct=None,
            limit_value=card.limit_value,
            reset_at=card.reset_at,
            window_start=window_start.isoformat(),
            samples_used=len(rows),
            confidence=confidence,
            status="insufficient_data",
            method="linear",
        )

    slope, intercept = fit.slope, fit.intercept

    current_used_value = valid_rows[-1].used_value
    assert current_used_value is not None  # valid_rows filtered to non-None used_value

    if abs(slope) < 1e-9:
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
            projected_used=current_used_value,
            projected_pct=now_pct,
            limit_value=card.limit_value,
            reset_at=card.reset_at,
            window_start=window_start.isoformat(),
            samples_used=len(rows),
            confidence=confidence,
            status="stable",
            method="linear",
        )

    projected_used = intercept + slope * reset_at_dt.timestamp()
    projected_used = max(projected_used, current_used_value)

    if card.unit_type == "percent":
        projected_pct = projected_used
    else:
        projected_pct = projected_used / card.limit_value * 100

    if projected_pct >= 100:
        status = "risk"
    elif projected_pct >= 80:
        status = "warn"
    else:
        status = "ok"

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
        limit_value=card.limit_value,
        reset_at=card.reset_at,
        window_start=window_start.isoformat(),
        samples_used=len(rows),
        confidence=confidence,
        status=status,
        method="linear",
    )


def compute_all_forecasts(cards: list[LimitCard], session: Session) -> ForecastResponse:
    forecasts: list[ForecastEntry] = []
    summary: dict[str, int] = {"risk": 0, "warn": 0, "ok": 0, "insufficient_data": 0, "stable": 0}

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
