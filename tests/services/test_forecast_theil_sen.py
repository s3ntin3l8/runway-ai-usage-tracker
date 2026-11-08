"""Targeted tests for the Phase 2 algorithm changes: Theil-Sen robustness,
horizon cap on projected_limit_hit_at, and the decelerating status."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import QuotaSnapshot
from app.models.schemas import LimitCard
from app.services.forecast import compute_forecast, fit_trend


@pytest.fixture
def db_session(mock_db_session):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_card(
    *,
    used_value: float,
    limit_value: float,
    reset_at: datetime,
    window_type: str = "weekly",
) -> LimitCard:
    return LimitCard(
        service_name="Test",
        unit="tokens",
        unit_type="tokens",
        used_value=used_value,
        limit_value=limit_value,
        is_unlimited=False,
        reset_at=reset_at.isoformat(),
        window_type=window_type,
        provider_id="anthropic",
        account_id="acc1",
        health="good",
        data_source="api",
    )


def _make_snapshot(session, *, ts, pct_used, reset_at, window_type="weekly"):
    snap = QuotaSnapshot(
        provider_id="anthropic",
        account_id="acc1",
        window_type=window_type,
        variant="",
        model_id="",
        ts=ts,
        pct_used=pct_used,
        reset_at=reset_at,
    )
    session.add(snap)
    session.commit()


class TestTheilSenEstimator:
    def test_perfect_linear_data_matches_ols(self):
        """On clean linear data, Theil-Sen and OLS produce the same fit."""
        xs = [0.0, 3600.0, 7200.0, 10800.0]
        ys = [10.0, 30.0, 50.0, 70.0]
        fit = fit_trend(xs, ys)
        assert fit is not None
        assert fit.method == "theil_sen"
        assert fit.slope == pytest.approx(20.0 / 3600.0, rel=1e-9)
        assert fit.intercept == pytest.approx(10.0, abs=1e-9)

    def test_single_outlier_does_not_drag_slope(self):
        """A 10× spike at one bucket must not significantly shift the slope."""
        # Clean slope = 5 pct/hr = 5/3600 pct/sec
        clean_xs = [i * 3600.0 for i in range(8)]
        clean_ys = [5.0 * (i + 1) for i in range(8)]
        clean = fit_trend(clean_xs, clean_ys)
        assert clean is not None

        # Inject a 10× spike at index 4 — y jumps from 25 to 250 then resumes.
        spiky_ys = clean_ys.copy()
        spiky_ys[4] = 250.0
        spiky_ys[5] = 30.0
        spiky_ys[6] = 35.0
        spiky_ys[7] = 40.0
        spiky = fit_trend(clean_xs, spiky_ys)
        assert spiky is not None

        # Theil-Sen median-of-pairwise-slopes is robust: a single outlier
        # shouldn't more than double the slope estimate (OLS would explode).
        assert spiky.slope == pytest.approx(clean.slope, rel=0.5)

    def test_below_min_buckets_returns_none(self):
        """Fewer than MIN_BUCKETS_FOR_TREND points → no fit."""
        assert fit_trend([0.0, 3600.0], [10.0, 20.0]) is None
        assert fit_trend([0.0, 3600.0, 7200.0], [10.0, 20.0, 30.0]) is None


class TestHorizonCap:
    def test_anchored_projection_below_limit_yields_no_hit_at(self, db_session):
        """Gentle slope that can't reach 100% within remaining time → no hit_at."""
        window_start = datetime(2026, 5, 12, 0, 0, 0, tzinfo=UTC)
        now = window_start + timedelta(hours=23)
        reset_at = window_start + timedelta(hours=24)
        limit = 1_000_000

        # Gentle growth: 0.1 pct/hour for 5 hourly buckets. now_pct ≈ 0.5%.
        # With 1h remaining and slope ≈ 2.78e-5 pct/s, projected ≈ 0.6% ≪ 100%.
        for i in range(5):
            _make_snapshot(
                db_session,
                ts=window_start + timedelta(hours=i),
                pct_used=0.1 * (i + 1),
                reset_at=reset_at,
            )

        card = _make_card(used_value=5_000.0, limit_value=float(limit), reset_at=reset_at)
        result = compute_forecast(card, db_session, now=now)
        assert result is not None
        assert result.projected_limit_hit_at is None
        assert result.status in ("ok", "stable")


class TestDeceleratingStatus:
    def test_high_usage_with_dipping_projection_decelerates(self, db_session):
        """At 95% with regression projecting below current → decelerating."""
        window_start = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
        now = window_start + timedelta(days=6, hours=12)
        reset_at = window_start + timedelta(days=7)
        limit = 1000

        # pct_used: small daily increments then a large spike on day 6.
        # Theil-Sen median slope is dominated by the slow-growth pairs → regression
        # projects below the current 95%, triggering decelerating.
        pct_values = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 95.0]
        for i, pct in enumerate(pct_values):
            _make_snapshot(
                db_session,
                ts=window_start + timedelta(days=i),
                pct_used=pct,
                reset_at=reset_at,
            )

        card = _make_card(used_value=950.0, limit_value=float(limit), reset_at=reset_at)
        result = compute_forecast(card, db_session, now=now)
        assert result is not None
        assert result.status == "decelerating"

    def test_low_usage_decelerating_does_not_fire(self, db_session):
        """At 40% (below DECELERATING_NOW_PCT_THRESHOLD), same dipping pattern
        produces 'stable' or 'ok' — not 'decelerating'."""
        window_start = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
        now = window_start + timedelta(days=6, hours=12)
        reset_at = window_start + timedelta(days=7)
        limit = 1000

        # Same back-loaded shape at lower magnitude → now_pct=40.
        pct_values = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 40.0]
        for i, pct in enumerate(pct_values):
            _make_snapshot(
                db_session,
                ts=window_start + timedelta(days=i),
                pct_used=pct,
                reset_at=reset_at,
            )

        card = _make_card(used_value=400.0, limit_value=float(limit), reset_at=reset_at)
        result = compute_forecast(card, db_session, now=now)
        assert result is not None
        assert result.status != "decelerating"

    def test_exhausted_wins_over_decelerating(self, db_session):
        """At 99.95%, status=exhausted regardless of slope direction."""
        window_start = datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)
        now = window_start + timedelta(days=6, hours=12)
        reset_at = window_start + timedelta(days=7)
        limit = 1000

        pct_values = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 99.95]
        for i, pct in enumerate(pct_values):
            _make_snapshot(
                db_session,
                ts=window_start + timedelta(days=i),
                pct_used=pct,
                reset_at=reset_at,
            )

        card = _make_card(used_value=999.5, limit_value=float(limit), reset_at=reset_at)
        result = compute_forecast(card, db_session, now=now)
        assert result is not None
        assert result.status == "exhausted"
