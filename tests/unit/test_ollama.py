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
           <span>Cloud Usage</span>
           <span class="badge">Pro</span>
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
            header = collector._get_cookie_header()
            assert header == "__Secure-session=val"
            assert mock_get.call_count > 1  # Should have tried previous names
