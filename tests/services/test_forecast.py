"""Unit tests for the forecast service (quota-snapshot based)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import QuotaSnapshot
from app.models.schemas import LimitCard
from app.services.forecast import SnapshotCache, compute_forecast

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
    variant: str | None = None,
    service_name: str = "Claude API",
    window_type: str = "weekly",
    unit_type: str = "tokens",
    unit: str = "tokens",
    used_value: float | None = 50_000.0,
    limit_value: float | None = 1_000_000.0,
    pct_used: float | None = None,
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
        pct_used=pct_used,
        is_unlimited=is_unlimited,
        reset_at=reset_at,
        window_type=window_type,
        provider_id=provider_id,
        account_id=account_id,
        account_label=account_label,
        model_id=model_id,
        variant=variant,
        health="good",
        data_source="api",
    )


def _make_snapshot(
    *,
    session: Session,
    ts: datetime,
    pct_used: float,
    reset_at: datetime,
    provider_id: str = "anthropic",
    account_id: str = "acc1",
    window_type: str = "weekly",
    variant: str = "",
    model_id: str = "",
) -> QuotaSnapshot:
    snap = QuotaSnapshot(
        provider_id=provider_id,
        account_id=account_id,
        window_type=window_type,
        variant=variant,
        model_id=model_id,
        ts=ts,
        pct_used=pct_used,
        reset_at=reset_at,
    )
    session.add(snap)
    session.commit()
    return snap


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_forecast_with_no_snapshots_returns_insufficient(db_session):
    """No snapshots in the window → returns insufficient_data status."""
    card = _make_card()
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "insufficient_data"
    assert result.samples_used == 0


def test_forecast_extrapolates_linear_growth(db_session):
    """Snapshots growing linearly over 4 hourly buckets → ok status, projected > now."""
    now = datetime.now(UTC)
    reset_at_dt = now + timedelta(days=4)
    limit = 10_000_000.0

    pct_values = [1.0, 1.5, 2.0, 2.5]
    for i, pct in enumerate(pct_values):
        _make_snapshot(
            session=db_session,
            ts=now - timedelta(hours=4 - i),
            pct_used=pct,
            reset_at=reset_at_dt,
        )

    card = _make_card(
        used_value=25_000.0,
        limit_value=limit,
        reset_at=reset_at_dt.isoformat(),
    )
    result = compute_forecast(card, db_session)

    assert result is not None
    assert result.status in ("ok", "warn")
    assert result.samples_used >= 4
    assert result.projected_pct is not None
    assert result.projected_pct > (25_000.0 / limit * 100)


def test_forecast_skips_cards_without_limit_value(db_session):
    """limit_value=None and non-percent unit_type → compute_forecast returns None."""
    card = _make_card(limit_value=None)
    result = compute_forecast(card, db_session)
    assert result is None


def test_forecast_handles_single_bucket_gracefully(db_session):
    """Only one distinct hour-bucket of snapshots → insufficient_data, no crash."""
    now = datetime.now(UTC)
    reset_at_dt = now + timedelta(days=4)
    anchor_hour = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)

    for i in range(5):
        _make_snapshot(
            session=db_session,
            ts=anchor_hour + timedelta(minutes=i),
            pct_used=10.0 + i * 0.1,
            reset_at=reset_at_dt,
        )

    card = _make_card(reset_at=reset_at_dt.isoformat())
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status == "insufficient_data"
    # All 5 snapshots fall into the same hourly bucket → samples_used = 1
    assert result.samples_used == 1


def test_forecast_excludes_unlimited(db_session):
    """is_unlimited=True → compute_forecast returns None."""
    card = _make_card(is_unlimited=True)
    result = compute_forecast(card, db_session)
    assert result is None


def test_forecast_excludes_pay_as_you_go(db_session):
    """unit=pay-as-you-go → compute_forecast returns None."""
    card = _make_card(unit="pay-as-you-go", limit_value=1_000_000.0)
    result = compute_forecast(card, db_session)
    assert result is None


def test_forecast_handles_percent_unit_type(db_session):
    """percent unit_type with quota snapshots → produces a result."""
    now = datetime.now(UTC)
    reset_at_dt = now + timedelta(days=4)
    for i in range(4):
        _make_snapshot(
            session=db_session,
            ts=now - timedelta(hours=4 - i),
            pct_used=10.0 + i * 2.0,
            reset_at=reset_at_dt,
            window_type="weekly",
        )
    card = _make_card(
        unit_type="percent",
        unit="percent",
        used_value=16.0,
        limit_value=100.0,
        pct_used=16.0,
        reset_at=reset_at_dt.isoformat(),
    )
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status in (
        "ok",
        "warn",
        "risk",
        "stable",
        "insufficient_data",
        "decelerating",
        "exhausted",
    )


def test_forecast_risk_status_steep_trajectory(db_session):
    """Steep pct growth over short window → risk or warn status."""
    now = datetime.now(UTC)
    reset_at_dt = now + timedelta(hours=6)
    limit = 100_000.0

    pct_values = [20.0, 40.0, 60.0, 80.0]
    for i, pct in enumerate(pct_values):
        _make_snapshot(
            session=db_session,
            ts=now - timedelta(hours=4 - i),
            pct_used=pct,
            reset_at=reset_at_dt,
            window_type="daily",
        )

    card = _make_card(
        used_value=80_000.0,
        limit_value=limit,
        reset_at=reset_at_dt.isoformat(),
        window_type="daily",
    )
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status in ("risk", "warn")
    assert result.projected_pct is not None
    assert result.projected_pct >= 80.0


def test_forecast_isolates_by_model(db_session):
    """Snapshots for model_id='sonnet' don't bleed into model_id='' aggregate forecast."""
    now = datetime.now(UTC)
    reset_at_dt = now + timedelta(days=4)

    for i in range(4):
        ts = now - timedelta(hours=4 - i)
        _make_snapshot(
            session=db_session,
            ts=ts,
            pct_used=50.0 + i * 5.0,
            reset_at=reset_at_dt,
            model_id="sonnet",
        )

    # Aggregate card (model_id=None) queries model_id="" — no matching snapshots.
    card_agg = _make_card(
        model_id=None,
        used_value=0.0,
        limit_value=1_000_000.0,
        reset_at=reset_at_dt.isoformat(),
    )
    result_agg = compute_forecast(card_agg, db_session)
    assert result_agg is not None
    assert result_agg.status == "insufficient_data"

    # Sonnet card queries model_id="sonnet" — sees all 4 snapshots.
    card_sonnet = _make_card(
        model_id="sonnet",
        used_value=65_000.0,
        limit_value=1_000_000.0,
        reset_at=reset_at_dt.isoformat(),
    )
    result_sonnet = compute_forecast(card_sonnet, db_session)
    assert result_sonnet is not None
    assert result_sonnet.samples_used >= 4


def test_forecast_missing_reset_at_returns_none(db_session):
    """reset_at=None → compute_forecast returns None."""
    card = LimitCard(
        service_name="Test",
        unit="tokens",
        unit_type="tokens",
        used_value=100.0,
        limit_value=1_000_000.0,
        window_type="weekly",
        provider_id="anthropic",
        account_id="acc1",
        health="good",
        data_source="api",
        reset_at=None,
    )
    result = compute_forecast(card, db_session)
    assert result is None


def test_forecast_includes_session_window(db_session):
    """window_type=session → uses 5-min buckets; no snapshots → insufficient_data."""
    card = _make_card(window_type="session")
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.window_type == "session"
    assert result.status == "insufficient_data"


def test_forecast_session_window_with_enough_buckets(db_session):
    """session window: 4+ distinct 5-min buckets → produces a real forecast."""
    now = datetime.now(UTC)
    reset_at_dt = now + timedelta(hours=3)

    # Seed 4 snapshots 10 minutes apart so they land in different 5-min buckets.
    for i in range(4):
        _make_snapshot(
            session=db_session,
            ts=now - timedelta(minutes=30 - i * 10),
            pct_used=10.0 + i * 5.0,
            reset_at=reset_at_dt,
            window_type="session",
        )

    card = _make_card(
        used_value=25_000.0,
        limit_value=200_000.0,
        reset_at=reset_at_dt.isoformat(),
        window_type="session",
    )
    result = compute_forecast(card, db_session)
    assert result is not None
    assert result.status != "insufficient_data"
    assert result.samples_used >= 4


# ── Lock-in tests: pin hit-at + decelerating behavior ─────────────────────────


def test_projected_limit_hit_at_matches_anchored_formula(db_session):
    """For a clean linear trajectory, hit_at = now + (100 - now_pct) / slope, ±1s."""
    from statistics import median

    window_start = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
    now = window_start + timedelta(hours=4)
    reset_at = window_start + timedelta(days=7)
    limit = 1000.0

    # pct_used grows linearly: 10, 30, 50, 70 at hours 0–3.
    pct_values = [10.0, 30.0, 50.0, 70.0]
    for i, pct in enumerate(pct_values):
        _make_snapshot(
            session=db_session,
            ts=window_start + timedelta(hours=i),
            pct_used=pct,
            reset_at=reset_at,
        )

    card = _make_card(
        used_value=700.0,  # now_pct = 70.0
        limit_value=limit,
        reset_at=reset_at.isoformat(),
        window_type="weekly",
    )
    result = compute_forecast(card, db_session, now=now)
    assert result is not None
    assert result.status == "risk"
    assert result.projected_limit_hit_at is not None

    # Theil-Sen == OLS on perfectly linear data.
    xs = [i * 3600.0 for i in range(4)]
    ys = pct_values
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    ]
    slope = median(slopes)
    expected_hit_ts = now + timedelta(seconds=(100.0 - 70.0) / slope)
    actual_hit_ts = datetime.fromisoformat(result.projected_limit_hit_at)
    delta = abs((actual_hit_ts - expected_hit_ts).total_seconds())
    assert delta < 1.0, f"hit_at off by {delta}s"


def test_decelerating_status_when_high_usage_and_projection_dips(db_session):
    """Monotone-but-slowing trajectory at high usage → status='decelerating'."""
    window_start = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
    now = window_start + timedelta(days=6, hours=12)
    reset_at = window_start + timedelta(days=7)
    limit = 1000.0

    # pct_used: 5, 6, 7, 8, 9, 10, 95 — small daily increments then a huge spike at day 6.
    pct_values = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 95.0]
    for i, pct in enumerate(pct_values):
        _make_snapshot(
            session=db_session,
            ts=window_start + timedelta(days=i),
            pct_used=pct,
            reset_at=reset_at,
        )

    card = _make_card(
        used_value=950.0,  # now_pct = 95.0
        limit_value=limit,
        reset_at=reset_at.isoformat(),
        window_type="weekly",
    )
    result = compute_forecast(card, db_session, now=now)
    assert result is not None
    assert result.status == "decelerating"
    assert result.projected_pct == pytest.approx(95.0, abs=0.5)
    assert result.slope is not None and result.slope >= 0.0
    assert result.method == "theil_sen"


def test_batch_cache_trimmed_to_current_window(db_session):
    """Regression: batch cache must not include snapshots from prior session windows.

    The batch snapshot cache is built from the earliest window_start across ALL
    cards (e.g. a monthly card pushes it back 30 days). Without per-card trimming,
    session snapshots from *previous* sessions contaminate the Theil-Sen regression
    and produce slope ≈ 0 → status "stable" even during active heavy usage.

    After the fix, _snapshots_for_card filters cache entries to [window_start, now]
    so only the current session's data is used.
    """
    # Current session: window_start = 2h ago, reset = 3h from now.
    now = datetime(2026, 5, 21, 15, 0, 0, tzinfo=UTC)
    reset_at_dt = now + timedelta(hours=3)
    window_start = reset_at_dt - timedelta(hours=5)  # 2h ago relative to now

    # ── In-window buckets: 4 points showing clear growth ──────────────────────
    in_window_buckets: list[tuple[datetime, float]] = [
        (window_start + timedelta(minutes=5),  2.0),
        (window_start + timedelta(minutes=30), 8.0),
        (window_start + timedelta(minutes=60), 14.0),
        (window_start + timedelta(minutes=90), 20.0),
    ]

    # ── Out-of-window buckets: prior sessions from 24h ago, all at 0% ─────────
    # These represent the contaminating data the bug introduced.
    prior_session_start = window_start - timedelta(hours=24)
    out_of_window_buckets: list[tuple[datetime, float]] = [
        (prior_session_start + timedelta(minutes=i * 5), 0.0)
        for i in range(60)  # 60 × 5min = 5h of flat-zero prior session
    ]

    # Combine into a single cache entry (mimics the batch path's broad time range).
    cache_key = ("anthropic", "acc1", "session", "", "")
    contaminated_cache: SnapshotCache = {
        cache_key: out_of_window_buckets + in_window_buckets,
    }

    card = _make_card(
        used_value=20.0,
        limit_value=100.0,
        unit_type="percent",
        unit="percent",
        pct_used=20.0,
        reset_at=reset_at_dt.isoformat(),
        window_type="session",
    )

    result = compute_forecast(card, db_session, now=now, snapshot_cache=contaminated_cache)

    assert result is not None
    # Before fix: contaminated cache → slope ≈ 0 → status "stable", projected ≈ now_pct
    # After fix: trimmed to current window → positive slope → status "ok" or "warn"
    assert result.status not in ("stable", "insufficient_data"), (
        f"Forecast should reflect current-window growth, got status={result.status!r} "
        f"projected_pct={result.projected_pct}"
    )
    # Samples should reflect only in-window buckets, not the 60 prior-session buckets.
    assert result.samples_used == len(in_window_buckets), (
        f"Expected {len(in_window_buckets)} in-window samples, got {result.samples_used}"
    )
    # Slope must be positive (usage is growing).
    assert result.slope is not None and result.slope > 0, (
        f"Expected positive slope, got {result.slope}"
    )
