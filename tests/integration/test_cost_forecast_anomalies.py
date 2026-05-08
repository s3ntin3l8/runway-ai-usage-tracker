"""Integration tests for /usage/cost-forecast (Task 14.2) and /usage/anomalies (Task 14.3)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import UsagePeriodRollup
from app.services.pricing_seed import seed_pricing_table


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        seed_pricing_table(s)
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


def _client():
    return TestClient(app)


def _now():
    return datetime.now(UTC)


def _rollup(
    *,
    provider_id: str = "anthropic",
    account_id: str = "user@x.com",
    period_type: str = "day",
    period_key: str,
    model_id: str = "",
    sidecar_id: str = "",
    cost_usd: float = 0.0,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    msgs: int = 0,
) -> UsagePeriodRollup:
    return UsagePeriodRollup(
        provider_id=provider_id,
        account_id=account_id,
        period_type=period_type,
        period_key=period_key,
        model_id=model_id,
        sidecar_id=sidecar_id,
        cost_usd=cost_usd,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        msgs=msgs,
    )


# ===========================================================================
# Task 14.2 — /cost-forecast
# ===========================================================================


class TestCostForecastEndpoint:
    """Tests for GET /api/v1/usage/cost-forecast."""

    def test_cost_forecast_with_no_data_returns_zeros(self, session):
        r = _client().get("/api/v1/usage/cost-forecast")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["current_month_to_date"] == 0.0
        assert data["daily_burn_avg_7d"] == 0.0
        assert data["projected_eom"] == 0.0
        assert data["by_provider"] == []
        assert "days_in_month" in data
        assert "day_of_month" in data
        assert "days_remaining" in data

    def test_cost_forecast_extrapolates_from_7d_burn(self, session):
        now = _now()
        month_key = now.strftime("%Y-%m")

        # Seed 7 days of $10/day burn
        for i in range(1, 8):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            session.add(_rollup(period_key=day, cost_usd=10.0))

        # Seed MTD rollup = $70
        session.add(_rollup(period_type="month", period_key=month_key, cost_usd=70.0))
        session.commit()

        r = _client().get("/api/v1/usage/cost-forecast")
        assert r.status_code == 200, r.text
        data = r.json()

        assert abs(data["current_month_to_date"] - 70.0) < 0.001
        assert abs(data["daily_burn_avg_7d"] - 10.0) < 0.001

        # projected = MTD + avg * days_remaining
        days_remaining = data["days_remaining"]
        expected_projected = 70.0 + 10.0 * days_remaining
        assert abs(data["projected_eom"] - expected_projected) < 0.01

    def test_cost_forecast_per_provider_breakdown(self, session):
        now = _now()
        month_key = now.strftime("%Y-%m")

        # Two providers with different burn rates
        for i in range(1, 8):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            session.add(
                _rollup(provider_id="anthropic", account_id="u@x", period_key=day, cost_usd=5.0)
            )
            session.add(
                _rollup(provider_id="gemini", account_id="g@x", period_key=day, cost_usd=2.0)
            )

        session.add(
            _rollup(
                provider_id="anthropic",
                account_id="u@x",
                period_type="month",
                period_key=month_key,
                cost_usd=35.0,
            )
        )
        session.add(
            _rollup(
                provider_id="gemini",
                account_id="g@x",
                period_type="month",
                period_key=month_key,
                cost_usd=14.0,
            )
        )
        session.commit()

        r = _client().get("/api/v1/usage/cost-forecast")
        assert r.status_code == 200, r.text
        data = r.json()

        # Two entries in by_provider
        assert len(data["by_provider"]) == 2
        by_pid = {e["provider_id"]: e for e in data["by_provider"]}
        assert abs(by_pid["anthropic"]["daily_burn_avg_7d"] - 5.0) < 0.001
        assert abs(by_pid["gemini"]["daily_burn_avg_7d"] - 2.0) < 0.001

        # Top-level is the sum
        assert abs(data["daily_burn_avg_7d"] - 7.0) < 0.001

    def test_cost_forecast_filters_by_provider_id(self, session):
        now = _now()
        month_key = now.strftime("%Y-%m")

        for i in range(1, 8):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            session.add(
                _rollup(provider_id="anthropic", account_id="u@x", period_key=day, cost_usd=5.0)
            )
            session.add(
                _rollup(provider_id="gemini", account_id="g@x", period_key=day, cost_usd=2.0)
            )

        session.add(
            _rollup(
                provider_id="anthropic",
                account_id="u@x",
                period_type="month",
                period_key=month_key,
                cost_usd=35.0,
            )
        )
        session.add(
            _rollup(
                provider_id="gemini",
                account_id="g@x",
                period_type="month",
                period_key=month_key,
                cost_usd=14.0,
            )
        )
        session.commit()

        r = _client().get("/api/v1/usage/cost-forecast", params={"provider_id": "anthropic"})
        assert r.status_code == 200, r.text
        data = r.json()

        # Only anthropic data
        assert len(data["by_provider"]) == 1
        assert data["by_provider"][0]["provider_id"] == "anthropic"
        assert abs(data["current_month_to_date"] - 35.0) < 0.001

    def test_cost_forecast_no_daily_data_uses_mtd_only(self, session):
        """When there's MTD but no daily history, projected_eom == MTD."""
        now = _now()
        month_key = now.strftime("%Y-%m")
        session.add(_rollup(period_type="month", period_key=month_key, cost_usd=50.0))
        session.commit()

        r = _client().get("/api/v1/usage/cost-forecast")
        assert r.status_code == 200
        data = r.json()
        assert abs(data["current_month_to_date"] - 50.0) < 0.001
        assert data["daily_burn_avg_7d"] == 0.0
        assert abs(data["projected_eom"] - 50.0) < 0.001

    def test_cost_forecast_response_shape(self, session):
        """Verify all required keys are present in the response."""
        r = _client().get("/api/v1/usage/cost-forecast")
        assert r.status_code == 200
        data = r.json()
        required_keys = {
            "as_of",
            "current_month_to_date",
            "daily_burn_avg_7d",
            "projected_eom",
            "days_in_month",
            "day_of_month",
            "days_remaining",
            "by_provider",
        }
        assert required_keys.issubset(data.keys())


# ===========================================================================
# Task 14.3 — /anomalies
# ===========================================================================


class TestAnomaliesEndpoint:
    """Tests for GET /api/v1/usage/anomalies."""

    def test_anomalies_empty_when_no_data(self, session):
        r = _client().get("/api/v1/usage/anomalies")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["anomalies"] == []
        assert data["lookback_days"] == 30
        assert data["z_threshold"] == 2.0

    def test_anomalies_detects_3sigma_spike(self, session):
        now = _now()
        today_key = now.strftime("%Y-%m-%d")

        # 29 days of normal history: 1000 tokens/day (stdev = 0 would fail, so vary slightly)
        base_tokens = [1000] * 14 + [1100] * 7 + [900] * 8  # 29 entries with some variance
        for i, toks in enumerate(base_tokens, start=1):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            session.add(_rollup(period_key=day, tokens_input=toks, model_id=""))

        # Today: massive spike — 50000 tokens
        session.add(_rollup(period_key=today_key, tokens_input=50000, model_id=""))
        session.commit()

        r = _client().get("/api/v1/usage/anomalies", params={"lookback_days": 30})
        assert r.status_code == 200, r.text
        data = r.json()

        assert len(data["anomalies"]) >= 1
        a = data["anomalies"][0]
        assert a["verdict"] == "spike"
        assert a["today_tokens"] == 50000
        assert a["z_score_tokens"] > 3.0

    def test_anomalies_skips_constant_history(self, session):
        """stdev=0 (constant history) should produce no anomalies even with a large spike."""
        now = _now()
        today_key = now.strftime("%Y-%m-%d")

        # 30 days of IDENTICAL history
        for i in range(1, 31):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            session.add(_rollup(period_key=day, tokens_input=1000, model_id=""))

        session.add(_rollup(period_key=today_key, tokens_input=99999, model_id=""))
        session.commit()

        r = _client().get("/api/v1/usage/anomalies")
        assert r.status_code == 200
        # Constant stdev=0 → no anomaly emitted
        assert r.json()["anomalies"] == []

    def test_anomalies_excludes_today_from_historical_window(self, session):
        """Today's value must not be included in the mean/stdev calculation."""
        now = _now()
        today_key = now.strftime("%Y-%m-%d")

        # 10 days of varying history
        history_tokens = [800, 1200, 900, 1100, 950, 1050, 1000, 1100, 900, 800]
        for i, toks in enumerate(history_tokens, start=1):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            session.add(_rollup(period_key=day, tokens_input=toks, model_id=""))

        # Today: spike
        session.add(_rollup(period_key=today_key, tokens_input=50000, model_id=""))
        session.commit()

        r = _client().get("/api/v1/usage/anomalies", params={"lookback_days": 15})
        assert r.status_code == 200, r.text
        data = r.json()
        # Mean should be computed from history only (not including today's 50000)
        if data["anomalies"]:
            a = data["anomalies"][0]
            import statistics

            expected_mean = statistics.mean(history_tokens)
            assert abs(a["historical_mean_tokens"] - expected_mean) < 1.0

    def test_anomalies_threshold_below_2_sigma_returns_empty(self, session):
        """With a spike barely above mean but below z_threshold, no anomaly is emitted."""
        now = _now()
        today_key = now.strftime("%Y-%m-%d")

        # Stable history, moderate today
        history_tokens = [1000, 1100, 900, 1050, 950, 1000, 1100, 1000, 900, 1050]
        for i, toks in enumerate(history_tokens, start=1):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            session.add(_rollup(period_key=day, tokens_input=toks, model_id=""))

        # Today just slightly above mean
        session.add(_rollup(period_key=today_key, tokens_input=1150, model_id=""))
        session.commit()

        # Use very high threshold — nothing should be an anomaly
        r = _client().get("/api/v1/usage/anomalies", params={"z_threshold": 10.0})
        assert r.status_code == 200
        assert r.json()["anomalies"] == []

    def test_anomalies_response_shape(self, session):
        r = _client().get("/api/v1/usage/anomalies")
        assert r.status_code == 200
        data = r.json()
        assert "as_of" in data
        assert "lookback_days" in data
        assert "z_threshold" in data
        assert "anomalies" in data

    def test_anomalies_insufficient_history_skipped(self, session):
        """If only 1 historical day available, stdev can't be computed — skip."""
        now = _now()
        today_key = now.strftime("%Y-%m-%d")

        # Only 1 day of history (need at least 2 for statistics.stdev)
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        session.add(_rollup(period_key=yesterday, tokens_input=1000, model_id=""))
        session.add(_rollup(period_key=today_key, tokens_input=99999, model_id=""))
        session.commit()

        r = _client().get("/api/v1/usage/anomalies")
        assert r.status_code == 200
        # Too few history points — no anomaly
        assert r.json()["anomalies"] == []
