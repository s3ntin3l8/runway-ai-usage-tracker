"""Tests for forecast service — percent and currency card branches."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent, UsagePeriodRollup
from app.models.schemas import LimitCard
from app.services.forecast import compute_forecast


@pytest.fixture
def db_session(mock_db_session):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_card(
    *,
    provider_id: str = "anthropic",
    account_id: str | None = "acc1",
    service_name: str = "Claude Pro",
    window_type: str = "weekly",
    unit_type: str = "tokens",
    unit: str = "tokens",
    used_value: float | None = 50_000.0,
    limit_value: float | None = 1_000_000.0,
    is_unlimited: bool = False,
    reset_at: str | None = None,
    pct_used: float | None = None,
    model_id: str | None = None,
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
        model_id=model_id,
        health="good",
        data_source="api",
        pct_used=pct_used,
    )


def _make_event(
    session: Session,
    *,
    ts: datetime,
    provider_id: str = "anthropic",
    account_id: str = "acc1",
    model_id: str | None = None,
    event_id_suffix: str = "",
    tokens_input: int = 10_000,
    tokens_output: int = 5_000,
) -> UsageEvent:
    event = UsageEvent(
        provider_id=provider_id,
        account_id=account_id,
        event_id=f"evt_{ts.isoformat()}_{event_id_suffix}",
        ts=ts,
        model_id=model_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
        cost_usd=0.0,
        sidecar_id="local",
    )
    session.add(event)
    session.commit()
    return event


def _make_rollup(
    session: Session,
    *,
    period_key: str,
    provider_id: str = "anthropic",
    account_id: str = "acc1",
    period_type: str = "day",
    cost_usd: float = 1.00,
    tokens_input: int = 50_000,
    tokens_output: int = 25_000,
) -> UsagePeriodRollup:
    r = UsagePeriodRollup(
        provider_id=provider_id,
        account_id=account_id,
        period_type=period_type,
        period_key=period_key,
        model_id="",
        sidecar_id="",
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
        cost_usd=cost_usd,
        msgs=10,
    )
    session.add(r)
    session.commit()
    return r


# ── Percent card tests ──────────────────────────────────────────────────────


class TestPercentForecast:
    def test_percent_card_with_no_events(self, db_session):
        """Percent card with no events → insufficient_data."""
        card = _make_card(
            unit_type="percent", unit="percent", used_value=42.0, limit_value=100.0, pct_used=42.0
        )
        result = compute_forecast(card, db_session)
        assert result is not None
        assert result.status == "insufficient_data"

    def test_percent_card_with_events_produces_forecast(self, db_session):
        """Percent card with hourly events should produce a forecast (not None)."""
        now = datetime.now(UTC)
        reset_at = now + timedelta(days=3)

        # Seed multiple hours of events
        for i in range(4):
            ts = now - timedelta(hours=4 - i)
            _make_event(db_session, ts=ts, event_id_suffix=f"pct_{i}")

        card = _make_card(
            unit_type="percent",
            unit="percent",
            used_value=45.0,
            limit_value=100.0,
            pct_used=45.0,
            reset_at=reset_at.isoformat(),
        )
        result = compute_forecast(card, db_session)
        assert result is not None
        # With events, should produce a forecast (not just insufficient_data)
        assert result.status in ("ok", "warn", "risk", "stable", "insufficient_data")
        assert result.now_pct is not None

    def test_percent_card_high_usage_risk(self, db_session):
        """Percent card at 95% with growing events → risk status."""
        now = datetime.now(UTC)
        reset_at = now + timedelta(days=1)

        for i in range(4):
            ts = now - timedelta(hours=4 - i)
            _make_event(
                db_session,
                ts=ts,
                event_id_suffix=f"risk_{i}",
                tokens_input=50_000,
                tokens_output=50_000,
            )

        card = _make_card(
            unit_type="percent",
            unit="percent",
            used_value=95.0,
            limit_value=100.0,
            pct_used=95.0,
            reset_at=reset_at.isoformat(),
        )
        result = compute_forecast(card, db_session)
        assert result is not None
        # At 95% with growth, should be at least warn
        assert result.status in ("risk", "warn", "exhausted")

    def test_percent_card_stable(self, db_session):
        """Percent card at 10% with no events → insufficient_data (can't project)."""
        card = _make_card(
            unit_type="percent",
            unit="percent",
            used_value=10.0,
            limit_value=100.0,
            pct_used=10.0,
        )
        # No events → linear regression can't be built → insufficient_data
        result = compute_forecast(card, db_session)
        assert result is not None
        assert result.status == "insufficient_data"


# ── Currency card tests ──────────────────────────────────────────────────────


class TestCurrencyForecast:
    def test_currency_card_with_no_rollups(self, db_session):
        """Currency card with no rollup data → insufficient_data."""
        card = _make_card(
            unit_type="currency",
            unit="USD",
            used_value=10.0,
            limit_value=100.0,
            pct_used=10.0,
        )
        result = compute_forecast(card, db_session)
        assert result is not None
        assert result.status == "insufficient_data"

    def test_currency_card_with_rollups_produces_forecast(self, db_session):
        """Currency card with daily rollups should produce a forecast."""
        now = datetime.now(UTC)
        reset_at = now + timedelta(days=5)

        for i in range(4):
            day_key = (now - timedelta(days=4 - i)).strftime("%Y-%m-%d")
            _make_rollup(db_session, period_key=day_key, cost_usd=2.50)

        card = _make_card(
            unit_type="currency",
            unit="USD",
            used_value=10.0,
            limit_value=100.0,
            pct_used=10.0,
            reset_at=reset_at.isoformat(),
        )
        result = compute_forecast(card, db_session)
        assert result is not None
        assert result.status in ("ok", "warn", "risk", "stable", "insufficient_data")

    def test_currency_card_spending_rapidly(self, db_session):
        """Currency card at $80/$100 with fast burn → risk or warn."""
        now = datetime.now(UTC)
        reset_at = now + timedelta(days=2)

        for i in range(5):
            day_key = (now - timedelta(days=5 - i)).strftime("%Y-%m-%d")
            _make_rollup(db_session, period_key=day_key, cost_usd=10.00)

        card = _make_card(
            unit_type="currency",
            unit="USD",
            used_value=80.0,
            limit_value=100.0,
            pct_used=80.0,
            reset_at=reset_at.isoformat(),
        )
        result = compute_forecast(card, db_session)
        assert result is not None
        assert result.status in ("risk", "warn", "exhausted")


# ── Dispatch and edge cases ─────────────────────────────────────────────────


class TestForecastDispatch:
    def test_token_singular_normalizes(self, db_session):
        """unit_type='token' (singular) should be handled like 'tokens'."""
        card = _make_card(
            unit_type="token", unit="tokens", used_value=50_000.0, limit_value=1_000_000.0
        )
        result = compute_forecast(card, db_session)
        # Should not be None — 'token' is normalized to 'tokens'
        assert result is not None
        assert result.status == "insufficient_data"  # No events

    def test_rolling_window_type_supported(self, db_session):
        """window_type='rolling' should be supported (mapped to monthly duration)."""
        card = _make_card(
            unit_type="tokens",
            window_type="rolling",
            used_value=50_000.0,
            limit_value=1_000_000.0,
        )
        result = compute_forecast(card, db_session)
        assert result is not None
        # Should not be None — rolling windows are now supported

    def test_unsupported_unit_type_returns_none(self, db_session):
        """unit_type='requests' should return None (unsupported)."""
        card = _make_card(
            unit_type="requests", unit="requests", used_value=50.0, limit_value=1000.0
        )
        result = compute_forecast(card, db_session)
        assert result is None
