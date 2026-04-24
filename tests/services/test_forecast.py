"""Unit tests for the forecast service."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageSnapshot
from app.models.schemas import LimitCard
from app.services.forecast import compute_all_forecasts, compute_forecast

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db_session(mock_db_session):
    """Override the global mock_db_session with a real in-memory SQLite session."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_card(
    *,
    provider_id: str = "anthropic",
    account_id: str | None = "acc1",
    account_label: str | None = None,
    model_id: str | None = None,
    service_name: str = "Claude API",
    window_type: str = "weekly",
    unit_type: str = "tokens",
    unit: str = "tokens",
    used_value: float | None = 50_000.0,
    limit_value: float | None = 1_000_000.0,
    is_unlimited: bool = False,
    reset_at: str | None = None,
) -> LimitCard:
    if reset_at is None:
        reset_at = (datetime.now(UTC) + timedelta(days=4)).isoformat()
    return LimitCard(
        service_name=service_name,
        unit=unit,
        unit_type=unit_type,
        used_value=used_value,
        limit_value=limit_value,
        is_unlimited=is_unlimited,
        reset_at=reset_at,
        window_type=window_type,
        provider_id=provider_id,
        account_id=account_id,
        account_label=account_label,
        model_id=model_id,
        health="good",
        data_source="api",
    )


def _make_snapshot(
    *,
    session: Session,
    ts: datetime,
    used_value: float = 50_000.0,
    provider_id: str = "anthropic",
    account_id: str = "acc1",
    service_name: str = "Claude API",
    model_id: str | None = None,
    window_type: str = "weekly",
    unit_type: str = "tokens",
    limit_value: float = 1_000_000.0,
) -> UsageSnapshot:
    snap = UsageSnapshot(
        timestamp=ts,
        provider_id=provider_id,
        account_id=account_id,
        service_name=service_name,
        model_id=model_id,
        window_type=window_type,
        unit_type=unit_type,
        used_value=used_value,
        limit_value=limit_value,
        health="good",
        data_source="api",
    )
    session.add(snap)
    session.commit()
    return snap


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_linear_fit_clean_trajectory(db_session):
    """Monotonically increasing snapshots → status ok, projected_pct > now_pct."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)

    # Seed 4 snapshots spread over 3 days of the 7-day window
    window_start = reset_at - timedelta(days=7)
    for i in range(4):
        ts = window_start + timedelta(hours=18 * i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            used_value=100_000.0 + 50_000.0 * i,
        )

    card = _make_card(
        used_value=250_000.0,
        limit_value=1_000_000.0,
        reset_at=reset_at.isoformat(),
    )
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "ok"
    assert result.projected_pct is not None
    assert result.now_pct is not None
    assert result.projected_pct > result.now_pct
    assert result.samples_used == 4


def test_insufficient_data_single_sample(db_session):
    """Single snapshot → insufficient_data, projected_pct is None."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)
    window_start = reset_at - timedelta(days=7)

    _make_snapshot(
        session=db_session,
        ts=window_start + timedelta(hours=6),
        used_value=100_000.0,
    )

    card = _make_card(reset_at=reset_at.isoformat())
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "insufficient_data"
    assert result.projected_pct is None
    assert result.projected_used is None
    assert result.samples_used == 1


def test_insufficient_data_zero_samples(db_session):
    """No snapshots in window → insufficient_data."""
    reset_at = datetime.now(UTC) + timedelta(days=4)
    card = _make_card(reset_at=reset_at.isoformat())
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "insufficient_data"
    assert result.projected_pct is None
    assert result.samples_used == 0


def test_stable_slope_returns_now_pct(db_session):
    """Flat snapshots → stable, projected_pct equals now_pct."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)
    window_start = reset_at - timedelta(days=7)

    flat_value = 200_000.0
    for i in range(4):
        ts = window_start + timedelta(hours=24 * i)
        _make_snapshot(session=db_session, ts=ts, used_value=flat_value)

    card = _make_card(
        used_value=flat_value,
        limit_value=1_000_000.0,
        reset_at=reset_at.isoformat(),
    )
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "stable"
    assert result.projected_pct is not None
    assert result.now_pct is not None
    assert abs(result.projected_pct - result.now_pct) < 0.01


def test_percent_unit_bypasses_limit_division(db_session):
    """unit_type=percent: projected_pct is not divided by limit again."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)
    window_start = reset_at - timedelta(days=7)

    for i, pct in enumerate([20.0, 35.0, 50.0, 60.0]):
        ts = window_start + timedelta(hours=18 * i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            used_value=pct,
            unit_type="percent",
            limit_value=100.0,
        )

    card = _make_card(
        unit_type="percent",
        unit="percent",
        used_value=60.0,
        limit_value=100.0,
        reset_at=reset_at.isoformat(),
    )
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.projected_pct is not None
    # projected_pct should be the raw used_value (already a %), not divided by limit.
    # A 20→60 trend over 3 of 7 days projects to ~120+% at reset.
    # If the bug (re-dividing by 100) were present, result would be ~1.2 — this catches it.
    assert result.projected_pct > 90.0


def test_excludes_unlimited(db_session):
    """is_unlimited=True → compute_forecast returns None."""
    card = _make_card(is_unlimited=True)
    result = compute_forecast(card, db_session)
    assert result is None


def test_excludes_pay_as_you_go(db_session):
    """unit=pay-as-you-go → compute_forecast returns None."""
    card = _make_card(unit="pay-as-you-go", limit_value=1_000_000.0)
    result = compute_forecast(card, db_session)
    assert result is None


def test_excludes_session_window(db_session):
    """window_type=session → compute_forecast returns None."""
    card = _make_card(window_type="session")
    result = compute_forecast(card, db_session)
    assert result is None


def test_excludes_stale_reset_at(db_session):
    """reset_at within 60 seconds → compute_forecast returns None."""
    reset_at = datetime.now(UTC) + timedelta(seconds=30)
    card = _make_card(reset_at=reset_at.isoformat())
    result = compute_forecast(card, db_session)
    assert result is None


def test_series_key_isolates_by_service_name(db_session):
    """Two service_names under same provider produce separate ForecastEntry results."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)
    window_start = reset_at - timedelta(days=7)

    # Service A: slow growth (20k → 40k over 3 samples)
    for i, used in enumerate([20_000.0, 30_000.0, 40_000.0]):
        ts = window_start + timedelta(hours=24 * i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            used_value=used,
            service_name="Service A",
        )

    # Service B: fast growth (200k → 400k over 3 samples)
    for i, used in enumerate([200_000.0, 300_000.0, 400_000.0]):
        ts = window_start + timedelta(hours=24 * i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            used_value=used,
            service_name="Service B",
        )

    card_a = _make_card(
        service_name="Service A",
        used_value=40_000.0,
        reset_at=reset_at.isoformat(),
    )
    card_b = _make_card(
        service_name="Service B",
        used_value=400_000.0,
        reset_at=reset_at.isoformat(),
    )

    response = compute_all_forecasts([card_a, card_b], db_session)
    assert len(response.forecasts) == 2

    entry_a = next(e for e in response.forecasts if e.service_name == "Service A")
    entry_b = next(e for e in response.forecasts if e.service_name == "Service B")

    assert entry_a.samples_used == 3
    assert entry_b.samples_used == 3

    # Service B has 10x higher usage → higher projected_pct
    assert entry_b.projected_pct is not None
    assert entry_a.projected_pct is not None
    assert entry_b.projected_pct > entry_a.projected_pct * 5


def test_negative_slope_clamps_to_current(db_session):
    """Declining usage (negative slope) → projected_used >= current snapshot used_value."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)
    window_start = reset_at - timedelta(days=7)

    # Declining: 500k → 400k → 300k (mid-window reset artifact)
    for i, used in enumerate([500_000.0, 400_000.0, 300_000.0]):
        ts = window_start + timedelta(hours=24 * i)
        _make_snapshot(session=db_session, ts=ts, used_value=used)

    current_used = 300_000.0
    card = _make_card(
        used_value=current_used,
        limit_value=1_000_000.0,
        reset_at=reset_at.isoformat(),
    )
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.projected_used is not None, "expected a projection, got None"
    assert result.projected_used >= current_used


def test_warn_status_at_80_percent(db_session):
    """Trajectory projecting to ~85% at reset → status='warn'."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=3)
    window_start = reset_at - timedelta(days=7)
    limit = 1_000_000.0

    # Seed: 400k → 500k → 600k over first 4 days; projects to ~750-800k at reset
    for i, used in enumerate([400_000.0, 500_000.0, 600_000.0]):
        ts = window_start + timedelta(days=i * 1.5)
        _make_snapshot(session=db_session, ts=ts, used_value=used, limit_value=limit)

    card = _make_card(used_value=600_000.0, limit_value=limit, reset_at=reset_at.isoformat())
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.projected_pct is not None
    assert result.status in ("warn", "risk"), f"expected warn or risk, got {result.status}"
    assert result.projected_pct >= 80.0


def test_risk_status_at_100_percent(db_session):
    """Steep trajectory projecting past 100% → status='risk'."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=2)
    window_start = reset_at - timedelta(days=7)
    limit = 1_000_000.0

    # Seed: rapid growth, will clearly exceed limit by reset
    for i, used in enumerate([400_000.0, 600_000.0, 800_000.0]):
        ts = window_start + timedelta(days=i * 1.5)
        _make_snapshot(session=db_session, ts=ts, used_value=used, limit_value=limit)

    card = _make_card(used_value=800_000.0, limit_value=limit, reset_at=reset_at.isoformat())
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.projected_pct is not None
    assert result.status == "risk", (
        f"expected risk, got {result.status} ({result.projected_pct:.1f}%)"
    )
    assert result.projected_pct >= 100.0
