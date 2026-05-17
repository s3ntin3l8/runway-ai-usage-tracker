"""
Pytest configuration and shared fixtures for AI Usage Tracker tests.

Provides:
- Async test support via pytest-asyncio
- Mock httpx.AsyncClient for API testing
- Temporary environment variables for isolation
- Mock response data for various providers
"""

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

# Force a localhost / un-secured configuration for tests, BEFORE any app
# module is imported. config.py runs _validate_security_invariants at import
# time, so an operator's .env that legitimately sets APP_HOST=0.0.0.0 would
# otherwise refuse to load the test suite. Tests that want to exercise the
# multi-host gates monkeypatch the live settings explicitly.
os.environ["APP_HOST"] = "127.0.0.1"
os.environ.pop("ADMIN_API_KEY", None)
os.environ.pop("CORS_ORIGINS", None)
os.environ.pop("TLS_TERMINATED", None)

import httpx
import pytest

from app.core.config import settings
from app.main import app
from app.services.token_cache import token_cache
from tests.fixtures.mock_data import (
    ANTHROPIC_OAUTH_RESPONSE,
    BIGMODEL_ZAI_RESPONSE,
    CHATGPT_USAGE_RESPONSE,
    CLAUDE_WEB_API_ORGS_RESPONSE,
    CLAUDE_WEB_API_USAGE_RESPONSE,
    GEMINI_QUOTA_RESPONSE,
    GITHUB_COPILOT_RESPONSE,
    KIMI_RESPONSE,
    OPENCODE_GO_RESPONSE,
)


@pytest.fixture(autouse=True)
def setup_test_settings(monkeypatch):
    """Ensure consistent settings for all tests, isolating them from the local .env."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", None)


@pytest.fixture(autouse=True)
async def clear_token_cache():
    """Clear the global token cache before each test to ensure isolation."""
    await token_cache.reset()


@pytest.fixture(autouse=True)
def mock_db_session():
    """Mock the DB session to prevent tests from hitting the local database."""
    # We patch sqlmodel.Session directly as it is used via context manager in CredentialProvider
    with patch("sqlmodel.Session") as mock_session:
        mock_db = MagicMock()
        # Mock .exec() (SQLModel style)
        mock_db.exec.return_value.all.return_value = []
        mock_db.exec.return_value.first.return_value = None
        # Mock .query() (Legacy SQLAlchemy style)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        # Make the context manager return our mock DB
        mock_session.return_value.__enter__.return_value = mock_db
        yield mock_db


@pytest.fixture
def env_vars(monkeypatch):
    """Provide a helper to set environment variables during tests using monkeypatch."""

    def set_vars(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setenv(key, value)

    return set_vars


@pytest.fixture
async def api_client():
    """Provide an asynchronous httpx client for testing the FastAPI application."""
    async with httpx.AsyncClient(app=app, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def mock_http_client():
    """Provide a mock httpx.AsyncClient for testing API calls."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.fixture
def temp_env_file():
    """Create a temporary .env file for testing configuration loading."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("CLAUDE_CODE_OAUTH_TOKEN=test_token\n")
        f.write("GITHUB_TOKEN=github_test_token\n")
        temp_path = f.name

    yield temp_path

    # Cleanup
    if os.path.exists(temp_path):
        os.unlink(temp_path)


@pytest.fixture
def mock_anthropic_oauth_response():
    """Mock response from Anthropic OAuth API."""
    return ANTHROPIC_OAUTH_RESPONSE


@pytest.fixture
def mock_claude_web_api_orgs_response():
    """Mock response from Claude Web API organizations endpoint."""
    return CLAUDE_WEB_API_ORGS_RESPONSE


@pytest.fixture
def mock_claude_web_api_usage_response():
    """Mock response from Claude Web API usage endpoint."""
    return CLAUDE_WEB_API_USAGE_RESPONSE


@pytest.fixture
def mock_gemini_quota_response():
    """Mock response from Gemini quota API."""
    return GEMINI_QUOTA_RESPONSE


@pytest.fixture
def mock_github_copilot_response():
    """Mock response from GitHub Copilot API."""
    return GITHUB_COPILOT_RESPONSE


@pytest.fixture
def mock_chatgpt_usage_response():
    """Mock response from ChatGPT wham/usage API."""
    return CHATGPT_USAGE_RESPONSE


@pytest.fixture
def mock_opencode_go_response():
    """Mock response from OpenCode Go API."""
    return OPENCODE_GO_RESPONSE


@pytest.fixture
def mock_zai_response():
    """Mock response from zAI (Zhipu) API."""
    return BIGMODEL_ZAI_RESPONSE


@pytest.fixture
def mock_kimi_response():
    """Mock response from Kimi (Moonshot) API."""
    return KIMI_RESPONSE


@pytest.fixture
def error_response():
    """Provide common error response mocks."""

    def _create(status_code=500, detail="Internal Server Error"):
        response = MagicMock(spec=httpx.Response)
        response.status_code = status_code
        response.json.return_value = {"detail": detail}
        response.text = json.dumps({"detail": detail})
        return response

    return _create


@pytest.fixture
def mock_http_response(monkeypatch):
    """Factory fixture to create mock httpx.Response objects."""

    def create_response(status_code=200, json_data=None, text=""):
        response = MagicMock(spec=httpx.Response)
        response.status_code = status_code
        response.json.return_value = json_data or {}
        response.text = text
        return response

    return create_response


@pytest.fixture
def mock_keyring():
    """Provide a mock keyring module for testing token retrieval."""
    with patch("app.core.config.keyring") as mock_keyring:
        mock_keyring.get_password.return_value = None
        yield mock_keyring


# Pytest asyncio configuration
def pytest_configure(config):
    """Configure pytest with asyncio mode."""
    config.addinivalue_line("markers", "asyncio: mark test as asyncio")
