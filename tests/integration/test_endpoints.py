"""
Integration tests for the API endpoints and collector orchestration.

Tests cover:
- Full /api/limits endpoint with all collectors
- Graceful handling of individual collector failures
- Response validation against Pydantic schemas
- Error aggregation and reporting
- Rate limiting and timeout handling
"""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.schemas import LimitCard
from app.services.collector_manager import manager


@pytest.fixture
def _empty_db_session():
    """Override get_session with an empty in-memory SQLite so /limits falls
    back to manager.collect_all() instead of reading rows from the real DB."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def _get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    yield engine
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def test_client():
    """Create a test client for FastAPI app."""
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_db_session")
class TestLimitsEndpoint:
    """Integration tests for /api/limits endpoint."""

    async def test_limits_endpoint_success(self):
        """Test successful response from /api/limits with multiple collectors."""
        from fastapi.testclient import TestClient

        test_client = TestClient(app)

        with patch.object(manager, "collect_all") as mock_collect:
            mock_collect.return_value = [
                {
                    "service_name": "Claude Pro",
                    "icon": "🟠",
                    "remaining": "45.5%",
                    "unit": "capacity",
                    "reset": "in 4h 23m",
                    "health": "good",
                    "pace": "~5 days",
                    "detail": "45.5% used [OAuth]",
                },
                {
                    "service_name": "GitHub Copilot",
                    "icon": "🐙",
                    "remaining": "450/500",
                    "unit": "requests",
                    "reset": "in 2h 15m",
                    "health": "warning",
                    "pace": "Sustainable",
                    "detail": "90.0% used",
                },
            ]

            response = test_client.get("/api/v1/usage/limits")

        assert response.status_code == 200
        data = response.json()
        assert "limits" in data
        assert isinstance(data["limits"], list)
        assert len(data["limits"]) == 2

    async def test_limits_endpoint_partial_failure(self):
        """Test endpoint gracefully handles one collector failing."""
        from fastapi.testclient import TestClient

        test_client = TestClient(app)

        with patch.object(manager, "collect_all") as mock_collect:
            # Some collectors succeed, some fail (collector failures handled internally)
            mock_collect.return_value = [
                {
                    "service_name": "Claude Pro",
                    "icon": "🟠",
                    "remaining": "50%",
                    "unit": "capacity",
                    "reset": "in 5h",
                    "health": "good",
                    "pace": "~5 days",
                    "detail": "API: OAuth",
                },
                {
                    "service_name": "GitHub API",
                    "icon": "🐙",
                    "remaining": "ERR",
                    "unit": "request",
                    "reset": "Unknown",
                    "health": "critical",
                    "pace": "N/A",
                    "detail": "Connection timeout",
                },
            ]

            response = test_client.get("/api/v1/usage/limits")

        # Should still return 200 with mixed results
        assert response.status_code == 200
        data = response.json()
        assert len(data["limits"]) == 2

        # One success, one error
        assert any(card.get("remaining") != "ERR" for card in data["limits"])
        assert any(card.get("remaining") == "ERR" for card in data["limits"])

    async def test_limits_endpoint_all_collectors_fail(self):
        """Test endpoint when all collectors fail."""
        from fastapi.testclient import TestClient

        test_client = TestClient(app)

        with patch.object(manager, "collect_all") as mock_collect:
            mock_collect.return_value = []

            response = test_client.get("/api/v1/usage/limits")

        # Should still return 200 with empty limits
        assert response.status_code == 200
        data = response.json()
        assert data["limits"] == []


@pytest.mark.asyncio
class TestIngestEndpoint:
    """Integration tests for /api/v1/fleet/ingest endpoint."""

    def _get_hmac_headers(self, body: str, api_key: str = None) -> dict:
        """Generate HMAC headers for testing."""
        key = api_key or settings.INGEST_API_KEY
        timestamp = str(int(time.time()))
        # HMAC-SHA256 signature generation (matches the server); not password hashing.
        signature = hmac.new(
            key.encode(), f"{timestamp}".encode() + body.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "X-Signature": signature,
            "X-Timestamp": timestamp,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _make_secure_settings():
        """Create mock settings that pass security checks for ingest tests."""

        mock = MagicMock()
        mock.INGEST_API_KEY = "test-secret-key-for-ingest-tests"
        mock.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        return mock

    async def test_ingest_success(self, _empty_db_session):
        """Test successful metric ingestion."""
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        test_client = TestClient(app)
        test_key = "test-secret-key-for-ingest-tests"

        payload = {
            "provider": "claude",
            "sidecar_id": "test-host",
            "metrics": [
                {
                    "service_name": "Claude Pro",
                    "icon": "🟠",
                    "remaining": "60%",
                    "unit": "capacity",
                    "reset": "in 3h",
                    "health": "good",
                    "pace": "~5 days",
                    "detail": "External ingest",
                    "provider_id": "anthropic",
                    "account_id": "user@example.com",
                    "window_type": "weekly",
                    "data_source": "web",
                }
            ],
        }

        body = json.dumps(payload)
        headers = self._get_hmac_headers(body, api_key=test_key)

        with patch("app.api.endpoints.fleet.settings") as mock_settings:
            mock_settings.INGEST_API_KEY = test_key
            mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False

            with patch("app.api.endpoints.fleet.token_cache") as mock_cache:
                mock_cache.store = AsyncMock()

                response = test_client.post("/api/v1/fleet/ingest", content=body, headers=headers)

        # Should accept valid ingest
        assert response.status_code in [200, 202]
        assert response.json()["metrics_stored"] == 1

    async def test_ingest_invalid_signature(self):
        """Test that invalid signatures are rejected."""
        from fastapi.testclient import TestClient

        test_client = TestClient(app)

        payload = {"provider": "test", "metrics": []}
        body = json.dumps(payload)

        headers = {
            "X-Signature": "invalid-sig",
            "X-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
        }

        with patch("app.api.endpoints.fleet.settings") as mock_settings:
            mock_settings.INGEST_API_KEY = "test-key"
            mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False

            response = test_client.post("/api/v1/fleet/ingest", content=body, headers=headers)

        assert response.status_code == 401
        assert "Invalid HMAC signature" in response.json()["detail"]

    async def test_ingest_structured_metadata_extraction(self, _empty_db_session):
        """Verify that tokens are extracted from structured metadata."""
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        test_client = TestClient(app)
        test_key = "test-secret-key-for-ingest-tests"

        oauth_token = "sk-ant-oauthtest123"
        payload = {
            "provider": "anthropic",
            "metrics": [
                {
                    "service_name": "Claude Pro",
                    "icon": "🟠",
                    "remaining": "Token",
                    "unit": "oauth",
                    "reset": "—",
                    "health": "good",
                    "pace": "Token",
                    "detail": "[Token Extracted] [Sidecar]",
                    "data_source": "token_extracted",
                    "metadata": {"oauth_token": oauth_token, "provider_id": "anthropic"},
                }
            ],
        }

        body = json.dumps(payload)
        headers = self._get_hmac_headers(body, api_key=test_key)

        with patch("app.api.endpoints.fleet.settings") as mock_settings:
            mock_settings.INGEST_API_KEY = test_key
            mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False

            with patch("app.api.endpoints.fleet.token_cache") as mock_cache:
                mock_cache.store = AsyncMock()
                response = test_client.post("/api/v1/fleet/ingest", content=body, headers=headers)

                # Verify token was stored in cache
                mock_cache.store.assert_called_once()
                stored_tokens = mock_cache.store.call_args[0][1]
                assert stored_tokens["oauth_token"] == oauth_token

        assert response.status_code == 200

    async def test_ingest_invalid_payload(self):
        """Test that invalid payloads are rejected with correct HMAC."""
        from fastapi.testclient import TestClient

        test_client = TestClient(app)
        test_key = "test-secret-key-for-ingest-tests"

        invalid_payload = {
            # Missing required 'provider' field (metrics is now optional)
            "metrics": []
        }

        body = json.dumps(invalid_payload)
        headers = self._get_hmac_headers(body, api_key=test_key)

        with patch("app.api.endpoints.fleet.settings") as mock_settings:
            mock_settings.INGEST_API_KEY = test_key
            mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False

            response = test_client.post("/api/v1/fleet/ingest", content=body, headers=headers)

        # Should reject invalid payload with 400 (per current implementation), NOT 401
        assert response.status_code == 400

    async def test_ingest_rejects_when_api_key_empty(self):
        """C1: ingest endpoint must return 503 when INGEST_API_KEY is empty."""
        from fastapi.testclient import TestClient

        test_client = TestClient(app)

        payload = {"provider": "claude", "metrics": []}
        body = json.dumps(payload)
        timestamp = str(int(time.time()))
        sig = hmac.new(b"", (timestamp + body).encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-Signature": sig,
            "X-Timestamp": timestamp,
            "Content-Type": "application/json",
        }

        with patch("app.api.endpoints.fleet.settings") as mock_settings:
            mock_settings.INGEST_API_KEY = ""
            response = test_client.post("/api/v1/fleet/ingest", content=body, headers=headers)

        assert response.status_code == 503
        assert "not configured" in response.json().get("detail", "").lower()

    async def test_ingest_rejects_when_api_key_is_default(self):
        """C3: ingest endpoint must return 503 when INGEST_API_KEY is the default insecure value."""
        from fastapi.testclient import TestClient

        from app.core.config import DEFAULT_INGEST_API_KEY

        test_client = TestClient(app)

        payload = {"provider": "claude", "metrics": []}
        body = json.dumps(payload)
        timestamp = str(int(time.time()))
        sig = hmac.new(
            DEFAULT_INGEST_API_KEY.encode(), (timestamp + body).encode(), hashlib.sha256
        ).hexdigest()
        headers = {
            "X-Signature": sig,
            "X-Timestamp": timestamp,
            "Content-Type": "application/json",
        }

        with patch("app.api.endpoints.fleet.settings") as mock_settings:
            mock_settings.INGEST_API_KEY = DEFAULT_INGEST_API_KEY
            mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = True
            response = test_client.post("/api/v1/fleet/ingest", content=body, headers=headers)

        assert response.status_code == 503
        assert "default" in response.json().get("detail", "").lower()


class TestCollectorOrchestration:
    """Tests for collector manager and orchestration logic."""

    @pytest.mark.asyncio
    async def test_concurrent_collector_execution(self):
        """Test that collectors run concurrently for better performance."""
        # This test needs revision to properly patch the manager's collect_all method
        pass

    @pytest.mark.asyncio
    async def test_collector_timeout_handling(self):
        """Test that individual collector timeouts don't block others."""
        # This test needs revision to properly patch the manager's collectors
        pass


class TestResponseValidation:
    """Tests for response schema validation."""

    @pytest.mark.asyncio
    async def test_limit_card_schema_validation(self):
        """Test that all responses conform to LimitCard schema."""

        valid_card = {
            "service_name": "Claude Pro",
            "icon": "🟠",
            "remaining": "45%",
            "unit": "capacity",
            "reset": "in 4h",
            "health": "good",
            "pace": "~5 days",
            "detail": "Details",
        }

        # Should validate successfully
        card = LimitCard(**valid_card)
        assert card.service_name == "Claude Pro"
        assert card.remaining == "45%"

    @pytest.mark.asyncio
    async def test_limit_card_missing_required_field(self):
        """Test that cards with missing required fields are rejected."""
        from pydantic import ValidationError

        # Now that many fields have defaults, we omit the mandatory 'service_name'
        invalid_card = {"icon": "❓"}

        with pytest.raises(ValidationError):
            LimitCard(**invalid_card)


@pytest.mark.asyncio
class TestErrorHandling:
    """Tests for error handling and recovery."""

    async def test_malformed_collector_response(self):
        """Test graceful handling of malformed collector responses."""

        # This test needs revision to properly patch the manager's collectors
        pass

    async def test_collector_exception_isolation(self):
        """Test that one collector exception doesn't crash the orchestrator."""

        # This test needs revision to properly patch the manager's collectors
        pass
