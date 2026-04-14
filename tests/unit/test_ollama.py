from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.services.collectors.ollama import OllamaCollector


@pytest.fixture
def ollama_html():
    return """
    <html>
      <head>
        <span id="header-email">user@example.com</span>
      </head>
      <body>
        <div>
           Cloud Usage
           <span class="text-xs font-normal px-2 py-0.5 rounded-full bg-neutral-100 text-neutral-600 capitalize">Pro</span>
        </div>
        <div class="usage-block">
          <span>Session usage</span>
          <div class="bar" style="width: 45.5%"></div>
          <span class="text">45.5% used</span>
          <span data-time="2026-04-12T15:00:00Z">Resets in 2 hours</span>
        </div>
        <div class="usage-block">
          <span>Weekly usage</span>
          <div class="bar" style="width: 12.0%"></div>
          <span class="text">12.0% used</span>
          <span data-time="2026-04-15T00:00:00Z">Resets in 3 days</span>
        </div>
      </body>
    </html>
    """


@pytest.mark.asyncio
async def test_ollama_parsing(ollama_html):
    collector = OllamaCollector()

    # Test internal parsing logic
    cards = collector._parse_html(ollama_html)

    assert len(cards) == 2

    session_card = next(c for c in cards if c["service_name"] == "Ollama Session")
    assert session_card["remaining"] == "54.5%"
    assert session_card["used_value"] == 45.5
    assert session_card["tier"] == "pro"
    assert "user@example.com" in session_card["detail"]
    assert session_card["reset_at"] == "2026-04-12T15:00:00+00:00"

    weekly_card = next(c for c in cards if c["service_name"] == "Ollama Weekly")
    assert weekly_card["remaining"] == "88.0%"
    assert weekly_card["used_value"] == 12.0
    assert weekly_card["tier"] == "pro"
    assert weekly_card["reset_at"] == "2026-04-15T00:00:00+00:00"


@pytest.mark.asyncio
async def test_ollama_remaining_bars_and_free_tier():
    """Real-world: Ollama bars show remaining width; free badge has full class string."""
    collector = OllamaCollector()
    html = """
    <html>
      <body>
        <span id="header-email">user@example.com</span>
        <div>
          <span>Cloud Usage</span>
          <span class="text-xs font-normal px-2 py-0.5 rounded-full bg-neutral-100 text-neutral-600 capitalize">free</span>
        </div>
        <div>
          <span>Session usage</span>
          <div style="width: 100%"></div>
          <span data-time="2026-04-14T12:00:00Z">Resets soon</span>
        </div>
        <div>
          <span>Weekly usage</span>
          <div style="width: 68.9%"></div>
          <span data-time="2026-04-15T00:00:00Z">Resets tomorrow</span>
        </div>
      </body>
    </html>
    """
    cards = collector._parse_html(html)
    session_card = next(c for c in cards if c["service_name"] == "Ollama Session")
    weekly_card = next(c for c in cards if c["service_name"] == "Ollama Weekly")
    # width: 100% → remaining bar → 100% remaining → 0% used
    assert session_card["used_value"] == 0.0
    assert session_card["remaining"] == "100.0%"
    assert session_card["health"] == "good"
    # width: 68.9% → remaining bar → 68.9% remaining → 31.1% used
    assert abs(weekly_card["used_value"] - 31.1) < 0.01
    assert weekly_card["remaining"] == "68.9%"
    assert weekly_card["tier"] == "free"


@pytest.mark.asyncio
async def test_ollama_no_auth():
    collector = OllamaCollector()

    with patch("app.services.collectors.ollama.get_session_cookies", return_value=[]):
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", ""):
            client = AsyncMock(spec=httpx.AsyncClient)
            results = await collector.collect(client)

            # BaseCollector should call _error_handler if primary returns empty list and no fallbacks
            assert len(results) == 1
            assert results[0]["remaining"] == "ERR"
            assert "Not logged in" in results[0]["detail"]


@pytest.mark.asyncio
async def test_ollama_primary_strategy(ollama_html):
    collector = OllamaCollector()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = ollama_html

    with patch("app.services.collectors.ollama.get_session_cookies", return_value=["fake_cookie"]):
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", ""):
            with patch(
                "app.services.collectors.ollama.http_request_with_retry",
                AsyncMock(return_value=mock_resp),
            ):
                client = AsyncMock(spec=httpx.AsyncClient)
                cards = await collector._primary_strategy(client)

                assert len(cards) == 2
                assert cards[0]["service_name"] == "Ollama Session"


@pytest.mark.asyncio
async def test_ollama_cookie_header_selection():
    collector = OllamaCollector()

    # Test that it finds __Secure-session if others are missing
    with patch("app.services.collectors.ollama.get_session_cookies") as mock_get:
        mock_get.side_effect = lambda domain, name: ["val"] if name == "__Secure-session" else []
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", ""):
            with patch("app.services.collectors.ollama.credential_provider.get_provider_session_cookie", return_value=None):
                header = collector._get_cookie_header()
                assert header == "__Secure-session=val"
                assert mock_get.call_count > 1  # Should have tried previous names


def test_looks_signed_out_with_login_page():
    """Test that login page HTML is detected as signed out."""
    collector = OllamaCollector()
    login_html = """
    <html><body>
        <h1>Sign in to Ollama</h1>
        <form action="/signin">
            <input type="email" name="email">
            <input type="password" name="password">
        </form>
    </body></html>
    """
    assert collector._looks_signed_out(login_html) is True


def test_looks_signed_out_log_in_heading():
    """Test 'Log in to Ollama' heading detection."""
    collector = OllamaCollector()
    html = """
    <html><body>
        <h1>Log in to Ollama</h1>
        <form action="/login">
            <input type="email">
            <input type="password">
        </form>
    </body></html>
    """
    assert collector._looks_signed_out(html) is True


def test_looks_signed_out_logged_in_returns_false():
    """Test that logged-in HTML returns False."""
    collector = OllamaCollector()
    logged_in_html = """
    <html><body>
        <h2 id="header-email">user@example.com</h2>
        <div>Cloud Usage</div>
        <span>Session usage</span>
    </body></html>
    """
    assert collector._looks_signed_out(logged_in_html) is False


def test_validate_cookie_header():
    """Test cookie header validation."""
    collector = OllamaCollector()

    assert collector._validate_cookie_header("session=abc123") is True
    assert collector._validate_cookie_header("ollama_session=xyz") is True
    assert collector._validate_cookie_header("__Secure-session=token") is True
    assert collector._validate_cookie_header("next-auth.session-token=auth") is True
    assert collector._validate_cookie_header("__Host-ollama_session=value") is True

    assert collector._validate_cookie_header("") is False
    assert collector._validate_cookie_header(None) is False
    assert collector._validate_cookie_header("random=value") is False
    assert collector._validate_cookie_header("foo=bar; baz=qux") is False


def test_parse_real_html():
    """Test parsing with real HTML from ollama.com/settings (logged in user)."""
    collector = OllamaCollector()
    real_html = """<html>
<head><title>Usage · Settings</title></head>
<body>
<h2 id="header-email" class="text-neutral-800 text-sm truncate">s3ntin3l8@gmail.com</h2>

<h2 class="text-xl font-medium flex items-center space-x-2">
    <span>Cloud Usage</span>
    <span class="text-xs font-normal px-2 py-0.5 rounded-full bg-neutral-100 text-neutral-600 capitalize">free</span>
</h2>

<div>
    <div class="flex justify-between mb-2">
        <span class="text-sm">Session usage</span>
        <span class="text-sm">0% used</span>
    </div>
    <div class="w-full border border-1 border-neutral-200 rounded-full h-2 overflow-hidden">
        <div class="h-full rounded-full bg-neutral-300" style="width: 0%"></div>
    </div>
    <div class="text-xs text-neutral-500 mt-1 local-time" data-time="2026-04-14T12:00:00Z">
        Resets in 3 minutes
    </div>
</div>

<div>
    <div class="flex justify-between mb-2">
        <span class="text-sm">Weekly usage</span>
        <span class="text-sm">31.1% used</span>
    </div>
    <div class="w-full border border-1 border-neutral-200 rounded-full h-2 overflow-hidden">
        <div class="h-full rounded-full bg-neutral-300" style="width: 31.1%"></div>
    </div>
    <div class="text-xs text-neutral-500 mt-1 local-time" data-time="2026-04-20T00:00:00Z">
        Resets in 5 days
    </div>
</div>

</body></html>"""

    cards = collector._parse_html(real_html)

    assert len(cards) == 2

    session_card = next(c for c in cards if c["service_name"] == "Ollama Session")
    assert session_card["used_value"] == 0.0
    assert session_card["remaining"] == "100.0%"
    assert session_card["tier"] == "free"
    assert "s3ntin3l8@gmail.com" in session_card["detail"]

    weekly_card = next(c for c in cards if c["service_name"] == "Ollama Weekly")
    assert abs(weekly_card["used_value"] - 31.1) < 0.01
    assert weekly_card["remaining"] == "68.9%"


def test_ollama_missing_data_error():
    """Test that missing usage data returns proper error type."""
    collector = OllamaCollector()
    html = """
    <html><body>
        <h2>Some other page</h2>
    </body></html>
    """
    cards = collector._parse_html(html)
    assert cards == []


@pytest.mark.asyncio
async def test_ollama_error_handler_not_logged_in():
    """Test error handler with not_logged_in reason."""
    collector = OllamaCollector()
    collector._last_error_reason = "not_logged_in"

    results = await collector._error_handler()

    assert len(results) == 1
    assert results[0]["error_type"] == "auth_required"
    assert "Not logged in" in results[0]["detail"]


@pytest.mark.asyncio
async def test_ollama_error_handler_missing_data():
    """Test error handler with missing_data reason."""
    collector = OllamaCollector()
    collector._last_error_reason = "missing_data"

    results = await collector._error_handler()

    assert len(results) == 1
    assert results[0]["error_type"] == "parse_error"
    assert "Could not parse" in results[0]["detail"]
