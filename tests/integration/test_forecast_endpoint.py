"""Integration tests for GET /api/v1/usage/forecast endpoint."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app as fastapi_app
from app.models.db import UsageSnapshot
from app.services.collector_manager import manager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CARD = {
    "service_name": "Test Service",
    "provider_id": "anthropic",
    "account_id": "acc1",
    "window_type": "weekly",
    "unit_type": "tokens",
    "unit": "tokens",
    "used_value": 500_000.0,
    "limit_value": 1_000_000.0,
    "is_unlimited": False,
    "reset_at": (datetime.now(UTC) + timedelta(days=4)).isoformat(),
    "health": "good",
    "data_source": "api",
    "input_source": "server",
}


def _card(**overrides) -> dict:
    return {**_BASE_CARD, **overrides}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestForecastEndpoint:
    """Integration tests for GET /api/v1/usage/forecast."""

    def test_forecast_endpoint_returns_200(self):
        """With 2 forecastable cards and no history, both should be insufficient_data."""
        registry = [
            _card(service_name="Service A", account_id="acc1"),
            _card(service_name="Service B", account_id="acc2"),
        ]
        client = TestClient(fastapi_app)
        with patch.object(manager, "_registry", registry):
            response = client.get("/api/v1/usage/forecast")

        assert response.status_code == 200
        data = response.json()

        assert "forecasts" in data
        assert len(data["forecasts"]) == 2

        assert "summary" in data
        assert set(data["summary"].keys()) == {"risk", "warn", "ok", "insufficient_data", "stable"}

        assert "generated_at" in data
        assert data["generated_at"]  # non-empty

        # No snapshot history → insufficient_data for all entries
        for entry in data["forecasts"]:
            assert entry["status"] == "insufficient_data"

    def test_forecast_endpoint_filters_by_provider_id(self):
        """Only cards matching provider_id=anthropic should be returned."""
        registry = [
            _card(service_name="Anthropic Card", provider_id="anthropic", account_id="a1"),
            _card(service_name="ChatGPT Card", provider_id="chatgpt", account_id="b1"),
        ]
        client = TestClient(fastapi_app)
        with patch.object(manager, "_registry", registry):
            response = client.get("/api/v1/usage/forecast?provider_id=anthropic")

        assert response.status_code == 200
        data = response.json()
        forecasts = data["forecasts"]

        assert len(forecasts) == 1
        assert forecasts[0]["provider_id"] == "anthropic"
        assert forecasts[0]["service_name"] == "Anthropic Card"

    def test_forecast_endpoint_filters_by_window_type(self):
        """Only cards with window_type=weekly should be returned when filtered."""
        registry = [
            _card(service_name="Weekly Card", window_type="weekly"),
            _card(
                service_name="Daily Card",
                window_type="daily",
                reset_at=(datetime.now(UTC) + timedelta(hours=20)).isoformat(),
            ),
        ]
        client = TestClient(fastapi_app)
        with patch.object(manager, "_registry", registry):
            response = client.get("/api/v1/usage/forecast?window_type=weekly")

        assert response.status_code == 200
        data = response.json()
        forecasts = data["forecasts"]

        assert len(forecasts) == 1
        assert forecasts[0]["window_type"] == "weekly"
        assert forecasts[0]["service_name"] == "Weekly Card"

    def test_forecast_endpoint_excludes_unlimited(self):
        """Unlimited cards must not appear in the forecast output."""
        registry = [
            _card(service_name="Limited Card", is_unlimited=False),
            _card(
                service_name="Unlimited Card",
                is_unlimited=True,
                limit_value=None,
            ),
        ]
        client = TestClient(fastapi_app)
        with patch.object(manager, "_registry", registry):
            response = client.get("/api/v1/usage/forecast")

        assert response.status_code == 200
        data = response.json()
        forecasts = data["forecasts"]

        service_names = [f["service_name"] for f in forecasts]
        assert "Unlimited Card" not in service_names
        assert "Limited Card" in service_names

    def test_forecast_endpoint_with_snapshot_history(self):
        """With real snapshot history, the endpoint should return a non-insufficient_data status."""
        # Build an in-memory SQLite engine for this test.
        # StaticPool forces all SQLAlchemy connections to share a single underlying
        # sqlite3 connection, which is required so that tables created by create_all
        # and rows seeded by the test are visible to the override_get_session used by
        # the request handler (default sqlite:// gives each connection its own DB).
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)

        def override_get_session():
            with Session(test_engine) as session:
                yield session

        fastapi_app.dependency_overrides[get_session] = override_get_session

        try:
            card = _card(
                service_name="Snapshot Service", provider_id="anthropic", account_id="snap_acc"
            )
            reset_at = datetime.fromisoformat(card["reset_at"])
            window_start = reset_at - timedelta(days=7)

            # Seed 5 snapshot rows spread across the window with increasing usage
            with Session(test_engine) as session:
                for i in range(5):
                    frac = (i + 1) / 6  # 1/6 … 5/6 of the window
                    ts = window_start + timedelta(seconds=frac * 7 * 24 * 3600)
                    snap = UsageSnapshot(
                        timestamp=ts,
                        provider_id=card["provider_id"],
                        account_id=card["account_id"],
                        account_label=None,
                        service_name=card["service_name"],
                        used_value=200_000.0 * (i + 1),  # 200k … 1000k
                        limit_value=card["limit_value"],
                        unit_type=card["unit_type"],
                        model_id=None,
                        window_type=card["window_type"],
                        health="good",
                        data_source="api",
                        is_unlimited=False,
                    )
                    session.add(snap)
                session.commit()

            client = TestClient(fastapi_app)
            with patch.object(manager, "_registry", [card]):
                response = client.get("/api/v1/usage/forecast")

            assert response.status_code == 200
            data = response.json()
            assert len(data["forecasts"]) == 1
            entry = data["forecasts"][0]
            # With 5 data points the forecast must go beyond insufficient_data
            assert entry["status"] != "insufficient_data", (
                f"Expected a real forecast status, got: {entry['status']}"
            )
        finally:
            fastapi_app.dependency_overrides.pop(get_session, None)
