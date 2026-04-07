"""
Pytest configuration and shared fixtures for AI Usage Tracker tests.

Provides:
- Async test support via pytest-asyncio
- Mock httpx.AsyncClient for API testing
- Temporary environment variables for isolation
- Mock response data for various providers
"""

import pytest
import os
import json
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
import httpx


@pytest.fixture
def env_vars():
    """Provide a context manager for temporary environment variables during tests."""
    original = os.environ.copy()
    
    def set_vars(**kwargs):
        for key, value in kwargs.items():
            os.environ[key] = value
        return lambda: os.environ.update(original)
    
    yield set_vars
    os.environ.clear()
    os.environ.update(original)


@pytest.fixture
async def mock_http_client():
    """Provide a mock httpx.AsyncClient for testing API calls."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.fixture
def temp_env_file():
    """Create a temporary .env file for testing configuration loading."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
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
    return {
        "five_hour": {
            "utilization": 45.5,
            "resets_at": "2025-04-07T12:00:00Z"
        },
        "seven_day": {
            "utilization": 62.3,
            "resets_at": "2025-04-14T00:00:00Z"
        },
        "seven_day_sonnet": {
            "utilization": 30.1,
            "resets_at": "2025-04-14T00:00:00Z"
        }
    }


@pytest.fixture
def mock_claude_web_api_orgs_response():
    """Mock response from Claude Web API organizations endpoint."""
    return [
        {
            "uuid": "org_test_123",
            "id": "org_test_123",
            "name": "Test Org"
        }
    ]


@pytest.fixture
def mock_claude_web_api_usage_response():
    """Mock response from Claude Web API usage endpoint."""
    return {
        "current_window": {
            "percentUsed": 45.5,
            "resetsAt": "2025-04-07T12:00:00Z"
        },
        "current_week": {
            "percentUsed": 62.3,
            "resetsAt": "2025-04-14T00:00:00Z"
        },
        "current_week_sonnet": {
            "percentUsed": 30.1,
            "resetsAt": "2025-04-14T00:00:00Z"
        }
    }


@pytest.fixture
def mock_gemini_quota_response():
    """Mock response from Gemini quota API."""
    return {
        "buckets": [
            {
                "modelId": "gemini-2.0-flash",
                "remainingFraction": 0.75,
                "resetTime": "2025-04-08T00:00:00Z"
            },
            {
                "modelId": "gemini-1.5-pro",
                "remainingFraction": 0.82,
                "resetTime": "2025-04-08T00:00:00Z"
            }
        ]
    }


@pytest.fixture
def mock_github_copilot_response():
    """Mock response from GitHub Copilot API."""
    return {
        "limited_user_quotas": {
            "completions": 45,
            "chat": 120
        },
        "limited_user_reset_date": "2025-04-08T00:00:00Z",
        "quota_snapshots": [
            {
                "metric": "premium_interactions",
                "remaining": 450,
                "entitlement": 500
            },
            {
                "metric": "chat",
                "remaining": 890,
                "entitlement": 1000
            }
        ],
        "copilot_plan": "Pro"
    }


@pytest.fixture
def mock_chatgpt_usage_response():
    """Mock response from ChatGPT wham/usage API."""
    return {
        "primary": {
            "utilization_percent": 55.3,
            "resets_at": 1744876800  # Unix timestamp
        }
    }


@pytest.fixture
def mock_opencode_go_response():
    """Mock response from OpenCode Go API."""
    return {
        "total_usage_usd": 12.50,
        "hard_limit_usd": 50.00
    }


@pytest.fixture
def mock_zai_response():
    """Mock response from zAI (Zhipu) API."""
    return {
        "data": {
            "available_balance": 125.45
        }
    }


@pytest.fixture
def mock_kimi_response():
    """Mock response from Kimi (Moonshot) API."""
    return {
        "data": {
            "available_balance": 8.75
        }
    }


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


# Pytest asyncio configuration
def pytest_configure(config):
    """Configure pytest with asyncio mode."""
    config.addinivalue_line(
        "markers", "asyncio: mark test as asyncio"
    )
