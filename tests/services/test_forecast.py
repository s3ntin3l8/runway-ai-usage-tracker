"""Unit tests for the forecast service."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageSnapshot
from app.models.schemas import LimitCard
from app.services.forecast import compute_forecast

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
    limit = 10_000_000.0  # High limit to avoid "risk"

    # Seed 4 snapshots spread over the last few hours
    for i in range(4):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(session=db_session, ts=ts, used_value=50_000.0 * (i + 1), limit_value=limit)

    card = _make_card(used_value=200_000.0, limit_value=limit, reset_at=reset_at.isoformat())
    result = compute_forecast(card, db_session)

    assert result is not None
    assert result.status == "ok"
    assert result.now_pct == 2.0
    assert result.projected_pct is not None
    assert result.projected_pct > 2.0


def test_insufficient_data_below_threshold(db_session):
    """Fewer than 4 snapshots (e.g. 3) → returns insufficient_data status."""
    now = datetime.now(UTC)
    for i in range(3):
        ts = now - timedelta(hours=3 - i)
        _make_snapshot(session=db_session, ts=ts)

    card = _make_card()
    result = compute_forecast(card, db_session)

    assert result is not None
    assert result.status == "insufficient_data"
    assert result.samples_used == 3


def test_insufficient_data_exactly_three_samples(db_session):
    """Threshold is 4; exactly 3 should still be insufficient."""
    now = datetime.now(UTC)
    for i in range(3):
        ts = now - timedelta(hours=3 - i)
        _make_snapshot(session=db_session, ts=ts)

    card = _make_card()
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "insufficient_data"
    assert result.samples_used == 3


def test_insufficient_data_zero_samples(db_session):
    """No snapshots → returns insufficient_data with 0 samples."""
    card = _make_card()
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "insufficient_data"
    assert result.samples_used == 0


def test_stable_slope_returns_now_pct(db_session):
    """Exactly flat usage (slope=0) → status stable, projected matches now."""
    now = datetime.now(UTC)
    for i in range(4):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(session=db_session, ts=ts, used_value=50_000.0)

    card = _make_card(used_value=50_000.0)
    result = compute_forecast(card, db_session)

    assert result is not None
    assert result.status == "stable"
    assert result.projected_pct == result.now_pct


def test_percent_unit_bypasses_limit_division(db_session):
    """If unit_type is percent, used_value is already 0-100; no /limit_value needed."""
    now = datetime.now(UTC)
    for i in range(4):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            used_value=10.0 * (i + 1),
            unit_type="percent",
            limit_value=100.0,
        )

    card = _make_card(unit_type="percent", unit="percent", used_value=60.0, limit_value=100.0)
    result = compute_forecast(card, db_session)

    assert result is not None
    assert result.now_pct == 60.0
    assert result.projected_pct is not None
    assert result.projected_pct > 60.0


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


def test_includes_session_window(db_session):
    """window_type=session → compute_forecast returns a ForecastEntry."""
    card = _make_card(window_type="session")
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.window_type == "session"
    assert result.status == "insufficient_data"  # No snapshots in db_session


def test_handles_rolling_window_reset(db_session):
    """reset_at within 10% of window duration → treated as rolling and forecasted."""
    now = datetime.now(UTC)
    # Weekly window (7 days), reset in 30 seconds is < 10% (16.8h)
    reset_at = now + timedelta(seconds=30)
    card = _make_card(window_type="weekly", reset_at=reset_at.isoformat())
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "insufficient_data"
    # Verify window_start is now - 7 days (approx)
    expected_start = now - timedelta(days=7)

    # We use isoformat to check strings
    assert result.window_start[:13] == expected_start.isoformat()[:13]


def test_series_key_isolates_by_model_id(db_session):
    """Two model_ids under same provider produce separate ForecastEntry results.

    Service name variations (e.g., display label changes) should NOT affect isolation.
    Model-specific windows (Sonnet/Opus/Design) MUST remain separate.
    """
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)
    limit = 100_000_000.0  # High limit to avoid capping at 100%

    # Sonnet: slow growth (20k → 50k over 4 samples)
    for i, used in enumerate([20_000.0, 30_000.0, 40_000.0, 50_000.0]):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            used_value=used,
            service_name="Claude (Sonnet Weekly)",
            model_id="sonnet",
            limit_value=limit,
        )

    # Opus: fast growth (200k → 500k over 4 samples)
    for i, used in enumerate([200_000.0, 300_000.0, 400_000.0, 500_000.0]):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            used_value=used,
            service_name="Claude (Opus Weekly)",
            model_id="opus",
            limit_value=limit,
        )

    card_sonnet = _make_card(
        service_name="Claude (Sonnet Weekly)",
        model_id="sonnet",
        used_value=50_000.0,
        reset_at=reset_at.isoformat(),
        limit_value=limit,
    )
    card_opus = _make_card(
        service_name="Claude (Opus Weekly)",
        model_id="opus",
        used_value=500_000.0,
        reset_at=reset_at.isoformat(),
        limit_value=limit,
    )

    entry_sonnet = compute_forecast(card_sonnet, db_session)
    entry_opus = compute_forecast(card_opus, db_session)

    assert entry_sonnet is not None
    assert entry_opus is not None
    assert entry_sonnet.samples_used == 4
    assert entry_opus.samples_used == 4
    # Ensure they have different projections because their slopes are different
    assert entry_sonnet.projected_pct != entry_opus.projected_pct


def test_service_name_variations_share_forecast(db_session):
    """If model_id is NULL, variations in service_name should still map to the same series."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)

    # Seed history using "Claude (Weekly Window)"
    for i, used in enumerate([20_000.0, 30_000.0, 40_000.0, 50_000.0]):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            used_value=used,
            service_name="Claude (Weekly Window)",
            model_id=None,
        )

    # Query using "Claude (Weekly Window) [Pro]" (e.g. after a tier rename)
    card = _make_card(
        service_name="Claude (Weekly Window) [Pro]",
        model_id=None,
        used_value=50_000.0,
        reset_at=reset_at.isoformat(),
    )

    entry = compute_forecast(card, db_session)
    assert entry is not None
    assert entry.samples_used == 4
    assert entry.now_pct == 5.0


def test_tiny_positive_slope_reported_as_stable(db_session):
    """Rounded-percentage series with <0.1pp projected growth → stable (not ok)."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)

    # Exactly flat usage: 42.00 → 42.00 → 42.00 → 42.00
    for i in range(4):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            used_value=42.0,
            unit_type="percent",
            limit_value=100.0,
        )

    card = _make_card(
        unit_type="percent",
        unit="percent",
        used_value=42.0,
        limit_value=100.0,
        reset_at=reset_at.isoformat(),
    )
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "stable"


def test_negative_slope_clamps_to_current(db_session):
    """Declining usage (negative slope) → projected_used >= current snapshot used_value."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=4)

    # Declining in the last few hours
    for i, used in enumerate([500_000.0, 400_000.0, 300_000.0, 200_000.0]):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(session=db_session, ts=ts, used_value=used)

    current_used = 200_000.0
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
    limit = 1_000_000.0

    # Seed: 400k → 500k → 600k → 650k in the last few hours
    for i, used in enumerate([400_000.0, 500_000.0, 600_000.0, 650_000.0]):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(session=db_session, ts=ts, used_value=used, limit_value=limit)

    card = _make_card(used_value=650_000.0, limit_value=limit, reset_at=reset_at.isoformat())
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.projected_pct is not None
    assert result.status in ("warn", "risk"), f"expected warn or risk, got {result.status}"
    assert result.projected_pct >= 80.0


def test_risk_status_at_100_percent(db_session):
    """Steep trajectory projecting past 100% → status='risk'."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=2)
    limit = 1_000_000.0

    # Steep growth in the last few hours: 700k → 750k → 800k → 850k
    # Slope = 50k / hour. Hits 1M in 3 hours.
    for i, used in enumerate([700_000.0, 750_000.0, 800_000.0, 850_000.0]):
        ts = now - timedelta(hours=3 - i)
        _make_snapshot(session=db_session, ts=ts, used_value=used, limit_value=limit)

    card = _make_card(used_value=850_000.0, limit_value=limit, reset_at=reset_at.isoformat())
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "risk"
    # Capped at 100% visually
    assert result.projected_pct == 100.0
    assert result.projected_used == 1_000_000.0
    # Field populated
    assert result.projected_limit_hit_at is not None
    hit_at = datetime.fromisoformat(result.projected_limit_hit_at)
    assert hit_at > now
    assert hit_at < reset_at


def test_already_at_100_percent_skips_hit_time(db_session):
    """If already at 100%, skip projecting a future hit time."""
    now = datetime.now(UTC)
    reset_at = now + timedelta(days=2)
    limit = 100.0

    # Usage reached 100% and stayed there
    for i in range(4):
        ts = now - timedelta(hours=3 - i)
        _make_snapshot(
            session=db_session, ts=ts, used_value=100.0, limit_value=limit, unit_type="percent"
        )

    card = _make_card(
        used_value=100.0, limit_value=limit, reset_at=reset_at.isoformat(), unit_type="percent"
    )
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.now_pct == 100.0
    assert result.projected_pct == 100.0
    assert result.projected_limit_hit_at is None
    assert result.status == "exhausted"
