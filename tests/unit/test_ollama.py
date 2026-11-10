import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.services.collectors.ollama import OllamaCollector
from app.services.smart_collector import SmartCollector


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

    session_card = next(c for c in cards if c.get("window_type") == "session")
    assert session_card["remaining"] == "54.5%"
    assert session_card["used_value"] == 45.5
    assert session_card["tier"] == "pro"
    assert "user@example.com" in session_card["detail"]
    assert session_card["reset_at"] == "2026-04-12T15:00:00+00:00"

    weekly_card = next(c for c in cards if c.get("window_type") == "weekly")
    assert weekly_card["remaining"] == "88.0%"
    assert weekly_card["used_value"] == 12.0
    assert weekly_card["tier"] == "pro"
    assert weekly_card["reset_at"] == "2026-04-15T00:00:00+00:00"

    assert session_card.get("window_type") == "session"
    assert weekly_card.get("window_type") == "weekly"


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
    session_card = next(c for c in cards if c.get("window_type") == "session")
    weekly_card = next(c for c in cards if c.get("window_type") == "weekly")
    # width: 100% → remaining bar → 100% remaining → 0% used
    assert session_card["used_value"] == 0.0
    assert session_card["remaining"] == "100.0%"
    assert session_card["health"] == "good"
    # width: 68.9% → remaining bar → 68.9% remaining → 31.1% used
    assert abs(weekly_card["used_value"] - 31.1) < 0.01
    assert weekly_card["remaining"] == "68.9%"
    assert weekly_card["tier"] == "free"


@pytest.mark.asyncio
@pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
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
@pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
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
                assert cards[0]["service_name"] == "Ollama"
                assert cards[0].get("window_type") == "session"


@pytest.mark.asyncio
@pytest.mark.skip(reason="browser-cookie / local fallback moved to sidecar")
async def test_ollama_cookie_header_selection():
    collector = OllamaCollector()

    # Test that it finds __Secure-session if others are missing
    with patch("app.services.collectors.ollama.get_session_cookies") as mock_get:
        mock_get.side_effect = lambda domain, name: ["val"] if name == "__Secure-session" else []
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", ""):
            with patch(
                "app.services.collectors.ollama.credential_provider.get_provider_session_cookie",
                return_value=None,
            ):
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

    session_card = next(c for c in cards if c.get("window_type") == "session")
    assert session_card["used_value"] == 0.0
    assert session_card["remaining"] == "100.0%"
    assert session_card["tier"] == "free"
    assert "s3ntin3l8@gmail.com" in session_card["detail"]

    weekly_card = next(c for c in cards if c.get("window_type") == "weekly")
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


WORKOS_SETTINGS_HTML = """
<html>
  <head><title>Usage · Settings</title></head>
  <body>
    <h2 id="header-email" class="text-neutral-800 text-sm truncate">s3ntin3l8@gmail.com</h2>
    <h2 class="text-xl font-medium flex items-center space-x-2">
      <span>Included usage</span>
      <span
        class="text-xs font-normal px-2 py-0.5 rounded-full bg-neutral-100 text-neutral-600 capitalize"
        >free</span
      >
    </h2>
    <div class="relative group" data-usage-meter>
      <div
        class="relative h-3 overflow-hidden rounded-full bg-neutral-200"
        data-usage-track
        aria-label="Free usage 0% used"
      >
        <div class="flex h-full overflow-hidden bg-neutral-950" style="width: 0%; "></div>
      </div>
      <div class="text-xs text-neutral-500 mt-1 local-time" data-time="2026-10-12T06:45:12Z">
        Resets in 2 weeks.
      </div>
    </div>
  </body>
</html>
"""


def test_wrap_cookie_bare_and_named_values():
    """Bare values are sent under both cookie names WorkOS accepts."""
    collector = OllamaCollector()

    assert collector._wrap_cookie("abc123") == "session=abc123; __Secure-session=abc123"
    assert (
        collector._wrap_cookie("b64padded==") == "session=b64padded==; __Secure-session=b64padded=="
    )
    assert collector._wrap_cookie("__Secure-session=abc123") == "__Secure-session=abc123"
    assert collector._wrap_cookie("session=abc123") == "session=abc123"
    assert collector._wrap_cookie("a=1; b=2") == "a=1; b=2"
    # A nameless pair makes it a header, not a bare value — never wrap it
    # (wrapping would produce `session=a=1; flag; …`, which validation accepts).
    assert collector._wrap_cookie("a=1; flag") == "a=1; flag"
    assert collector._validate_cookie_header("a=1; flag") is False


@pytest.mark.asyncio
async def test_get_cookie_header_wraps_bare_db_value():
    """A bare stored cookie (no name) must be wrapped for both names."""
    collector = OllamaCollector()

    with patch(
        "app.services.collectors.ollama.credential_provider.get_provider_session_cookie",
        return_value="  bare-value-123  ",
    ):
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", ""):
            header = await collector._get_cookie_header()

    assert header == "session=bare-value-123; __Secure-session=bare-value-123"
    assert collector.current_input_source == "config"
    assert collector._validate_cookie_header(header) is True


@pytest.mark.asyncio
async def test_get_cookie_header_wraps_env_token():
    collector = OllamaCollector()

    with patch(
        "app.services.collectors.ollama.credential_provider.get_provider_session_cookie",
        return_value=None,
    ):
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", "env-value"):
            header = await collector._get_cookie_header()

    assert header == "session=env-value; __Secure-session=env-value"
    assert collector.current_input_source == "server"


@pytest.mark.asyncio
async def test_get_cookie_header_sidecar_key_fallback():
    """Sidecar rules push `cookie_session`; settings UI stores `session_cookie`."""
    collector = OllamaCollector()

    with patch(
        "app.services.collectors.ollama.credential_provider.get_provider_session_cookie",
        return_value=None,
    ):
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", "env-value"):
            with patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                AsyncMock(
                    return_value=(
                        {"cookie_session": "sidecar-value"},
                        {"source": "sidecar-host"},
                    )
                ),
            ):
                header = await collector._get_cookie_header()

    assert header == "session=sidecar-value; __Secure-session=sidecar-value"
    assert collector.current_input_source == "sidecar"

    with patch(
        "app.services.collectors.ollama.credential_provider.get_provider_session_cookie",
        return_value=None,
    ):
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", ""):
            with patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                AsyncMock(
                    return_value=(
                        {"session_cookie": "ui-value", "cookie_session": "other"},
                        {"source": "config"},
                    )
                ),
            ):
                header = await collector._get_cookie_header()

    assert header == "session=ui-value; __Secure-session=ui-value"


def test_validate_cookie_header_rejects_wrapped_api_key():
    """The API-key guard must see the value, not the `session=` prefix."""
    collector = OllamaCollector()
    api_key = "sk-" + "a" * 24  # pragma: allowlist secret

    assert collector._validate_cookie_header(f"session={api_key}") is False
    assert collector._last_error_reason == "invalid_credential_type"
    assert (
        collector._validate_cookie_header(f"session={api_key}; __Secure-session={api_key}") is False
    )
    assert collector._last_error_reason == "invalid_credential_type"
    assert collector._validate_cookie_header(api_key) is False

    assert collector._validate_cookie_header("session=abc123; __Secure-session=abc123") is True


def test_workos_meter_parsing():
    """WorkOS redesign: aria-label meters, `Included usage` badge, monthly window.

    The reset date is injected relative to today so the horizon bucket stays
    stable (a fixed date would rot into `daily` once it passes).
    """
    collector = OllamaCollector()
    reset_dt = datetime.now(UTC).replace(microsecond=0) + timedelta(days=18)
    html = WORKOS_SETTINGS_HTML.replace(
        "2026-10-12T06:45:12Z", reset_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    cards = collector._parse_html(html)

    assert len(cards) == 1
    card = cards[0]
    assert card["window_type"] == "monthly"
    assert card["used_value"] == 0.0
    assert card["remaining"] == "100.0%"
    assert card["tier"] == "free"
    assert card["reset_at"] == reset_dt.isoformat()
    assert "s3ntin3l8@gmail.com" in card["detail"]
    assert collector._last_error_reason == "unknown"


def test_workos_meter_unknown_label_uses_reset_horizon():
    """No label keyword → window type inferred from the reset horizon."""
    collector = OllamaCollector()
    reset_at = (datetime.now(UTC) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = WORKOS_SETTINGS_HTML.replace("Free usage 0% used", "Balance 50% used").replace(
        "2026-10-12T06:45:12Z", reset_at
    )

    cards = collector._parse_html(html)

    assert len(cards) == 1
    assert cards[0]["window_type"] == "weekly"
    assert cards[0]["used_value"] == 50.0


def test_workos_duplicate_window_meter_deduped_with_log(caplog):
    """Two meters mapping to the same window type: first wins, drop is logged."""
    collector = OllamaCollector()
    stamp = (datetime.now(UTC) + timedelta(days=18)).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = WORKOS_SETTINGS_HTML.replace("2026-10-12T06:45:12Z", stamp).replace(
        "</body>",
        f"""
    <div data-usage-meter>
      <div data-usage-track aria-label="Balance 50% used"></div>
      <div class="local-time" data-time="{stamp}">Resets later.</div>
    </div>
    </body>""",
    )

    with caplog.at_level("DEBUG", logger="app.services.collectors.ollama"):
        cards = collector._parse_html(html)

    assert len(cards) == 1
    assert cards[0]["window_type"] == "monthly"
    assert cards[0]["used_value"] == 0.0
    assert any("Balance 50% used" in message for message in caplog.messages)


def test_workos_signin_page_detected():
    """The WorkOS sign-in page must not be mistaken for usage data."""
    collector = OllamaCollector()
    signin_html = """
    <html><body>
      <form action="javascript:throw new Error('React form unexpectedly submitted.')">
        <input type="email" name="email">
      </form>
      <a href="/login">Log in</a>
    </body></html>
    """
    assert collector._looks_signed_out(signin_html) is True
    assert collector._looks_signed_out(WORKOS_SETTINGS_HTML) is False

    cards = collector._parse_html(signin_html)
    assert cards == []
    assert collector._last_error_reason == "not_logged_in"


@pytest.mark.parametrize(
    ("label", "days_out", "expected"),
    [
        ("Hourly usage 10% used", None, "session"),
        ("Session usage 10% used", None, "session"),
        ("Weekly usage 10% used", None, "weekly"),
        ("Free usage 0% used", None, "monthly"),
        # Strong keywords beat the horizon even when it disagrees
        ("Weekly usage 10% used", 0.2, "weekly"),
        # The weak `free` keyword yields to a concrete reset horizon
        ("Free usage 0% used", 0.5, "daily"),
        ("Free usage 0% used", 5, "weekly"),
        ("Free usage 0% used", 18, "monthly"),
        ("Mystery quota", 0.2, "daily"),
        ("Mystery quota", 5, "weekly"),
        ("Mystery quota", 18, "monthly"),
        ("Mystery quota", 60, "rolling"),
        ("Mystery quota", None, "session"),
    ],
)
def test_window_type_for(label, days_out, expected):
    collector = OllamaCollector()
    now = datetime.now(UTC)
    resets_at = now + timedelta(days=days_out) if days_out is not None else None

    assert collector._window_type_for(label, resets_at, now) == expected


# ---------------------------------------------------------------------------
# Bearer /api/usage path (primary strategy when an API key is configured).
# Mirrors the TestOllamaApiCollector coverage that previously lived in test_collectors.py
# in some branches; kept here on PR #340 so the new code path has tests on
# its own branch (no dependency on a later merge).
# ---------------------------------------------------------------------------


def _usage_body(usage: float = 0.002) -> dict:
    return {
        "activity": {
            "cost": "0.00000",
            "period": {
                "type": "last_4_weeks",
                "starting_at": "2026-08-31T00:00:00Z",
                "ending_at": "2026-09-24T22:29:17Z",
            },
            "models": [],
        },
        "limits": {"monthly": {"usage": usage, "models": []}},
    }


def _balance_body(
    balance: float = 2.5,
    allowance: float = 2.5,
    purchased: float | None = 0.0,
    until: str = "2026-11-12T06:45:12Z",
) -> dict:
    """The `/api/balance` payload of a plan with a monthly credit allowance."""
    body: dict = {
        "included": {
            "balance_usd": balance,
            "allowance_usd": allowance,
            "period": {"from": "2026-10-12T06:45:12Z", "until": until},
        }
    }
    if purchased is not None:
        body["purchased"] = {"balance_usd": purchased}
    return body


def _make_response(body, status=200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json = MagicMock(return_value=body)
    resp.text = str(body)
    return resp


class TestOllamaApiCollector:
    """Bearer `GET /api/usage` path (primary when an API key is present)."""

    @pytest.mark.asyncio
    async def test_api_key_resolution_uses_server_env_for_default_account(self):
        collector = OllamaCollector()
        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.services.collectors.ollama.settings.OLLAMA_API_KEY", "server-key"),
        ):
            assert await collector._get_api_key() == "server-key"
        assert collector.current_input_source == "server"

    @pytest.mark.asyncio
    async def test_api_key_cache_uses_credential_scope_after_identity_promotion(self):
        collector = OllamaCollector(account_id="alice@example.com", credential_account_id="default")
        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=(
                    {"api_key": "sidecar-key"},
                    {"source": "sidecar-host"},
                ),  # pragma: allowlist secret
            ) as get_cached,
            patch("app.services.collectors.ollama.settings.OLLAMA_API_KEY", ""),
        ):
            assert await collector._get_api_key() == "sidecar-key"

        get_cached.assert_awaited_once_with("ollama", account_id="default")

    @pytest.mark.asyncio
    async def test_cookie_cache_uses_credential_scope_after_identity_promotion(self):
        collector = OllamaCollector(account_id="alice@example.com", credential_account_id="default")
        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_session_cookie",
                return_value=None,
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=({"cookie_session": "sidecar-cookie"}, {"source": "host"}),
            ) as get_cached,
            patch("app.services.collectors.ollama.settings.OLLAMA_SESSION_TOKEN", ""),
        ):
            assert await collector._get_cookie_header() == (
                "session=sidecar-cookie; __Secure-session=sidecar-cookie"
            )

        get_cached.assert_awaited_once_with("ollama", account_id="default")

    @pytest.mark.asyncio
    async def test_saved_api_key_precedes_sidecar_key(self):
        collector = OllamaCollector(account_id="acc_test")
        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value="saved-key",
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=(
                    {"api_key": "sidecar-key"},
                    {"source": "sidecar-host"},
                ),  # pragma: allowlist secret
            ),
        ):
            assert await collector._get_api_key() == "saved-key"
        assert collector.current_input_source == "config"

    @pytest.mark.asyncio
    async def test_api_strategy_emits_monthly_allowance_card(self):
        collector = OllamaCollector(account_id="acc_test")
        # Stale label from a prior poll cycle must not survive when
        # get_with_metadata returns None (PR #340 review, finding 4).
        collector.current_input_source = "sidecar"

        async def fake_get_token(*args, **kwargs):
            return ({"api_key": "test-key"}, {"source": "sidecar-host"})  # pragma: allowlist secret

        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.ollama.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=_make_response(_balance_body(balance=2.0, allowance=2.5)),
            ) as http_mock,
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert len(cards) == 1
        card = cards[0]
        assert card["provider_id"] == "ollama"
        assert card["window_type"] == "monthly"
        assert card["unit_type"] == "currency"
        assert card["currency"] == "USD"
        assert card["data_source"] == OllamaCollector.DATA_SOURCE_API
        assert card["input_source"] == "sidecar"
        # $0.50 spent of a $2.50 allowance = 20% used.
        assert card["used_value"] == pytest.approx(0.5)
        assert card["limit_value"] == pytest.approx(2.5)
        assert card["pct_used"] == pytest.approx(20.0)
        assert card["remaining"] == "80.0%"
        assert card["is_unlimited"] is False
        assert card["reset_at"] == "2026-11-12T06:45:12+00:00"
        assert card["detail"].startswith("$0.50 of $2.50 used")
        assert collector.current_input_source == "sidecar"
        # The quota lives on /api/balance — /api/usage is a spend report.
        assert http_mock.await_args.args[2] == OllamaCollector.BALANCE_URL

    @pytest.mark.asyncio
    async def test_api_strategy_reports_purchased_credits_in_detail(self):
        collector = OllamaCollector(account_id="acc_test")

        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value="test-key",
            ),
            patch(
                "app.services.collectors.ollama.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=_make_response(_balance_body(purchased=25.0)),
            ),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert len(cards) == 1
        assert "+$25.00 usage credits" in cards[0]["detail"]

    @pytest.mark.asyncio
    async def test_api_strategy_falls_back_to_usage_when_balance_endpoint_missing(self):
        """Deployments without /api/balance still get the legacy limit card."""
        collector = OllamaCollector(account_id="acc_test")

        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value="test-key",
            ),
            patch(
                "app.services.collectors.ollama.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[
                    _make_response({"error": "not found"}, status=404),
                    _make_response(_usage_body(usage=0.25)),
                ],
            ) as http_mock,
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert len(cards) == 1
        assert cards[0]["pct_used"] == pytest.approx(25.0)
        assert cards[0]["unit_type"] == "percent"
        assert http_mock.await_args_list[0].args[2] == OllamaCollector.BALANCE_URL
        assert http_mock.await_args_list[1].args[2] == OllamaCollector.USAGE_URL

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("cache_source", "expected"),
        [
            ("config", "config"),
            ("manual_config", "config"),
            ("some-hostname", "sidecar"),  # sidecar push: source = sidecar id
            (None, "sidecar"),  # missing source → zai/minimax default
        ],
    )
    async def test_api_strategy_input_source_from_cache_metadata(self, cache_source, expected):
        """Cache metadata drives input_source with the sibling-collector
        mapping: config only for UI-stored creds, sidecar otherwise."""
        collector = OllamaCollector(account_id="acc_test")

        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=({"api_key": "test-key"}, {"source": cache_source}),
            ),
            patch(
                "app.services.collectors.ollama.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=_make_response(_balance_body()),
            ),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert len(cards) == 1
        assert cards[0]["input_source"] == expected
        assert collector.current_input_source == expected

    @pytest.mark.asyncio
    async def test_api_strategy_no_key_returns_empty_for_fallback(self):
        """No API key configured → return [] so the base collector falls
        through to the cookie-scrape fallback. The collector does not
        emit an error card itself; that's the base collector's job."""
        collector = OllamaCollector(account_id="acc_test")

        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.services.collectors.ollama.settings.OLLAMA_API_KEY", ""),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert cards == []

    @pytest.mark.asyncio
    async def test_api_strategy_401_sets_invalid_api_key(self):
        collector = OllamaCollector(account_id="acc_test")

        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=(
                    {"api_key": "bad-key"},
                    {"source": "sidecar"},
                ),  # pragma: allowlist secret
            ),
            patch(
                "app.services.collectors.ollama.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=_make_response({"error": "unauthenticated"}, status=401),
            ),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert cards == []
        assert collector._last_error_reason == "invalid_api_key"
        card = (await collector._error_handler())[0]
        assert card["error_type"] == "auth_failed"
        assert "API key was rejected" in card["detail"]

    @pytest.mark.asyncio
    async def test_api_key_error_survives_less_specific_web_auth_failure(self):
        collector = OllamaCollector()
        collector._set_error_reason("invalid_api_key")
        collector._set_error_reason("not_logged_in")

        assert collector._last_error_reason == "invalid_api_key"

    @pytest.mark.asyncio
    async def test_api_strategy_timeout_returns_empty(self):
        """httpx.TimeoutException → empty list, no _last_error_reason set
        (base default treats empty as error → _error_handler runs)."""
        collector = OllamaCollector(account_id="acc_test")

        async def fake_http(*args, **kwargs):
            raise httpx.TimeoutException("boom")

        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value=None,
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=({"api_key": "test-key"}, {"source": "sidecar"}),
            ),
            patch(
                "app.services.collectors.ollama.http_request_with_retry",
                side_effect=fake_http,
            ),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert cards == []

    @pytest.mark.parametrize(("usage", "expected_pct"), [(0, 0), (0.5, 50), (1, 100)])
    def test_build_cards_from_api_usage_fraction(self, usage, expected_pct):
        """Usage at each supported boundary is shown as a percentage."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage(_usage_body(usage=usage))
        assert len(cards) == 1
        card = cards[0]
        assert card["unit_type"] == "percent"
        assert card["pct_used"] == pytest.approx(expected_pct)
        assert card["used_value"] == pytest.approx(expected_pct)
        assert card["limit_value"] == 100.0
        assert card["reset_at"] is None

    @pytest.mark.parametrize("usage", [1.01, 42.5, -0.01, float("nan"), float("inf")])
    def test_build_cards_from_api_usage_outside_fraction_falls_back(self, usage):
        """Only a finite 0..1 fraction is supported by the API strategy."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage(_usage_body(usage=usage))
        assert cards == []
        assert collector._last_error_reason == "missing_data"

    def test_build_cards_from_api_usage_missing_monthly(self):
        """Free-tier / no-cap response → empty list without a parse error."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage({"limits": {}, "activity": {}})
        assert cards == []
        assert collector._last_error_reason == "unknown"

    def test_build_cards_from_api_usage_missing_monthly_cap_is_successful(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage({"limits": {"monthly": None}, "activity": {}})
        assert cards == []
        assert collector._last_error_reason == "unknown"

    def test_build_cards_from_api_usage_monthly_object_without_usage_is_invalid(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage({"limits": {"monthly": {}}, "activity": {}})
        assert cards == []
        assert collector._last_error_reason == "missing_data"

    def test_build_cards_from_api_usage_spend_report_without_limits_is_malformed(self):
        """A spend report means the account has usage whose quota we cannot
        read here — that is malformed data, not a plan without a cap."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage(
            {"range": "7d", "totals": {"request_count": 3, "usage_usd": 0.5}, "buckets": []}
        )
        assert cards == []
        assert collector._last_error_reason == "missing_data"

    def test_build_cards_from_api_usage_payload_without_limits_is_malformed(self):
        """Only an explicit limits block proves there is no monthly cap."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage({})
        assert cards == []
        assert collector._no_monthly_cap is False
        assert collector._last_error_reason == "missing_data"

    @pytest.mark.asyncio
    async def test_collect_no_cap_response_returns_no_parse_error_card(self):
        collector = OllamaCollector(account_id="acc_test")
        smart = SmartCollector(collector, "Ollama", ttl=0, error_retry_delay=0)
        no_cap_response = _make_response({"included": {"balance_usd": 0, "allowance_usd": 0}})
        malformed_response = _make_response(
            {"included": {"balance_usd": "not-a-number", "allowance_usd": 2.5}}
        )

        with (
            patch(
                "app.services.collectors.ollama.credential_provider.get_provider_api_key",
                return_value="test-key",
            ),
            patch(
                "app.services.collectors.ollama.http_request_with_retry",
                new_callable=AsyncMock,
                side_effect=[no_cap_response, malformed_response],
            ),
        ):
            cards = await smart.collect(MagicMock())
            assert cards == []
            assert collector._last_error_reason == "unknown"
            assert smart.last_result == []
            assert smart.consecutive_errors == 0
            assert collector.successful_empty_result is True
            assert collector.complete_snapshot([]) is True
            assert smart.last_collection_state == "complete"

            # A later malformed response in the same collector instance must
            # not inherit the previous cycle's no-cap success marker.
            cards = await smart.collect(MagicMock())

        assert cards and cards[0]["remaining"] == "ERR"
        assert collector._last_error_reason == "missing_data"

    def test_build_cards_from_api_usage_invalid_usage(self):
        """Non-numeric usage value → empty list with missing_data."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage(
            {"limits": {"monthly": {"usage": "not-a-number"}}, "activity": {}}
        )
        assert cards == []
        assert collector._last_error_reason == "missing_data"


class TestOllamaBalanceCollector:
    """`GET /api/balance` — the endpoint that carries the quota."""

    def test_modern_shape_reports_allowance_and_reset(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance(_balance_body(balance=2.0, allowance=2.5))
        assert cards[0]["period_from"] == "2026-10-12T06:45:12+00:00"
        assert len(cards) == 1
        card = cards[0]
        assert card["service_name"] == "Ollama Cloud"
        assert card["provider_id"] == "ollama"
        assert card["window_type"] == "monthly"
        assert card["unit_type"] == "currency"
        assert card["currency"] == "USD"
        assert card["used_value"] == pytest.approx(0.5)
        assert card["limit_value"] == pytest.approx(2.5)
        assert card["pct_used"] == pytest.approx(20.0)
        assert card["remaining"] == "80.0%"
        assert card["health"] == "good"
        assert card["reset_at"] == "2026-11-12T06:45:12+00:00"
        assert card["reset"] != "—"
        assert card["detail"].startswith("$0.50 of $2.50 used")
        assert card["data_source"] == OllamaCollector.DATA_SOURCE_API

    def test_full_allowance_reports_nothing_used(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance(_balance_body())
        assert len(cards) == 1
        card = cards[0]
        assert card["pct_used"] == pytest.approx(0.0)
        assert card["remaining"] == "100.0%"
        assert card["used_value"] == pytest.approx(0.0)
        assert card["health"] == "good"

    def test_exhausted_allowance_is_critical(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance(_balance_body(balance=0.0, allowance=2.5))
        assert len(cards) == 1
        card = cards[0]
        assert card["pct_used"] == pytest.approx(100.0)
        assert card["remaining"] == "0.0%"
        assert card["health"] == "critical"

    def test_purchased_credits_appear_in_detail(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance(_balance_body(purchased=25.0))
        assert "+$25.00 usage credits" in cards[0]["detail"]

    def test_legacy_session_and_weekly_cards(self):
        collector = OllamaCollector(account_id="acc_test")
        body = {
            "included": {
                "session": {"remaining_percent": 75, "resets_at": "2026-10-01T07:00:00Z"},
                "weekly": {"remaining_percent": 40, "resets_at": "2026-10-05T00:00:00Z"},
            },
            "purchased": {"balance_usd": 25},
        }
        cards = collector._build_cards_from_api_balance(body)
        assert len(cards) == 2
        session, weekly = cards
        assert session["window_type"] == "session"
        assert session["remaining"] == "75.0%"
        assert session["pct_used"] == pytest.approx(25.0)
        assert session["unit_type"] == "percent"
        assert session["reset_at"] == "2026-10-01T07:00:00+00:00"
        assert weekly["window_type"] == "weekly"
        assert weekly["remaining"] == "40.0%"
        assert weekly["pct_used"] == pytest.approx(60.0)

    def test_purchased_only_account_reports_credits_balance(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance({"purchased": {"balance_usd": 25}})
        assert len(cards) == 1
        card = cards[0]
        assert card["remaining"] == "$25.00"
        assert card["unit_type"] == "currency"
        assert card["pct_used"] is None
        assert card["window_type"] == "rolling"

    def test_exhausted_credits_still_report_a_card(self):
        """An account with no credits left is exactly when the card matters."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance({"purchased": {"balance_usd": 0}})
        assert len(cards) == 1
        assert cards[0]["remaining"] == "$0.00"
        assert cards[0]["health"] == "critical"

    def test_zero_allowance_falls_back_to_purchased_credits(self):
        """No allowance to meter, but purchased credits are still the balance."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance(
            {"included": {"balance_usd": 0, "allowance_usd": 0}, "purchased": {"balance_usd": 25}}
        )
        assert len(cards) == 1
        assert cards[0]["remaining"] == "$25.00"
        assert cards[0]["variant"] == "credits"

    @pytest.mark.parametrize(
        "body",
        [
            {},  # not a balance payload at all
            {"included": None},  # no purchased block to prove there is no allowance
            {"included": None, "purchased": {"balance_usd": "not-a-number"}},
        ],
    )
    def test_payload_without_evidence_is_malformed_not_no_quota(self, body):
        """Only an explicit purchased block proves there is no included
        allowance. Anything else is unreadable data — reporting it as "no
        quota" would let the snapshot reconciliation retire a working card."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance(body)
        assert cards == []
        assert collector._no_monthly_cap is False
        assert collector._last_error_reason == "missing_data"

    def test_allowance_card_uses_its_own_pool(self):
        """A distinct variant: the stored percent card (legacy monthly limit)
        would otherwise freeze its quota fields when the unit changes."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance(_balance_body())
        assert cards[0]["variant"] == "allowance"

    def test_credits_card_uses_its_own_pool(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance({"purchased": {"balance_usd": 25}})
        assert cards[0]["variant"] == "credits"

    def test_unreadable_legacy_window_does_not_cost_the_sibling(self):
        """One broken window must not discard the account's other meter."""
        collector = OllamaCollector(account_id="acc_test")
        body = {
            "included": {
                "session": {"remaining_percent": 75, "resets_at": "2026-10-01T07:00:00Z"},
                "weekly": {"remaining_percent": "not-a-number"},
            }
        }
        cards = collector._build_cards_from_api_balance(body)
        assert len(cards) == 1
        assert cards[0]["window_type"] == "session"
        assert cards[0]["remaining"] == "75.0%"

    def test_all_legacy_windows_unreadable_is_malformed(self):
        collector = OllamaCollector(account_id="acc_test")
        body = {"included": {"session": {"remaining_percent": "bad"}}}
        cards = collector._build_cards_from_api_balance(body)
        assert cards == []
        assert collector._last_error_reason == "missing_data"

    @pytest.mark.parametrize(
        "included",
        [
            {"balance_usd": "not-a-number", "allowance_usd": 2.5},
            {"balance_usd": 2.5, "allowance_usd": "not-a-number"},
            {"balance_usd": -1.0, "allowance_usd": 2.5},
            {"balance_usd": 2.5, "allowance_usd": float("nan")},
            {"balance_usd": 2.5},  # allowance missing
            {"unexpected": True},
        ],
    )
    def test_malformed_included_is_a_parse_error(self, included):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance({"included": included})
        assert cards == []
        assert collector._last_error_reason == "missing_data"

    def test_malformed_included_container_is_a_parse_error(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance({"included": "nope"})
        assert cards == []
        assert collector._last_error_reason == "missing_data"

    def test_zero_allowance_is_a_successful_empty_result(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance(
            {"included": {"balance_usd": 0, "allowance_usd": 0}}
        )
        assert cards == []
        assert collector._no_monthly_cap is True
        assert collector._last_error_reason != "missing_data"

    def test_unparsable_reset_timestamp_is_tolerated(self):
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_balance(_balance_body(until="not-a-date"))
        assert len(cards) == 1
        assert cards[0]["reset_at"] is None
        assert cards[0]["reset"] == "—"


def test_the_page_email_is_reported_as_verified_identity(ollama_html):
    """The collector proves which account a cookie is signed into (#462): the server compares
    it with the account the cookie's origin is tagged to."""
    collector = OllamaCollector()
    assert collector.verified_identity is None

    collector._parse_html(ollama_html)

    assert collector.verified_identity == "user@example.com"


# --- ollama.com/settings fixture (#1287) -----------------------------------

SETTINGS_FIXTURE = Path(__file__).parent.parent / "fixtures" / "ollama-settings-included-usage.html"

_FREE_MODELS = (
    "beam",
    "gemma4:31b",
    "gpt-oss:120b",
    "gpt-oss:20b",
    "nemotron-3-super",
    "nemotron-3-ultra",
)


def test_settings_fixture_carries_the_free_model_list():
    """The captured page names the free-usage models in a stable, server-rendered element.

    This anchors the free-model-list parser tracked in #708: the list sits in
    ``<div id="free-plan-models">`` as one link per model, in the sentence
    "Free usage covers a, b, c. Upgrade to use any cloud model."
    """
    html = SETTINGS_FIXTURE.read_text(encoding="utf-8")

    block = re.search(r'<div id="free-plan-models"[^>]*>(.*?)</div>', html, re.DOTALL)
    assert block is not None
    assert "Free usage covers" in block.group(1)
    models = re.findall(r'<a href="/library/([^"]+)"', block.group(1))
    assert tuple(models) == _FREE_MODELS


def test_settings_fixture_holds_no_account_data():
    html = SETTINGS_FIXTURE.read_text(encoding="utf-8")

    assert re.search(r"\S+@\S+\.\S+", html) is None
    assert "mailto:" not in html
    assert "header-email" not in html
    assert "<script" not in html.lower()


def test_live_settings_page_reports_that_the_allowance_is_not_on_the_page():
    """The signed-in Usage page (#1287) carries no allowance meter (#1296).

    Its figures load through a script, so the raw response has no usage meter,
    plan badge or account-email element. That is a page that no longer shows the
    quota, not a layout the parser failed to read, so the reason says so. If
    Ollama renders the meter server-side again this fails: recapture the fixture.
    """
    html = SETTINGS_FIXTURE.read_text(encoding="utf-8")
    collector = OllamaCollector()

    assert collector._looks_signed_out(html) is False
    assert collector._parse_html(html) == []
    assert collector._last_error_reason == "usage_not_on_page"


def test_a_blank_signed_in_page_stays_a_parse_failure():
    """Only the known Usage page gets the specific reason; anything else is a parse error."""
    collector = OllamaCollector()

    assert collector._parse_html("<html><body><p>Settings</p></body></html>") == []
    assert collector._last_error_reason == "missing_data"


DASHBOARD_FIXTURE = Path(__file__).parent.parent / "fixtures" / "ollama-dashboard.html"


def _page_response(text, status=200):
    resp = _make_response({}, status)
    resp.text = text
    return resp


def _dashboard_html(balance="$0", used="&lt;$0.01"):
    return f'<span id="extra-usage-balance">{balance}</span><p id="dashboard-usage">{used}</p>'


def test_parse_dashboard_fixture():
    cards = OllamaCollector()._parse_dashboard(DASHBOARD_FIXTURE.read_text(encoding="utf-8"))

    assert len(cards) == 1
    card = cards[0]
    assert card["variant"] == "credits"
    assert card["remaining"] == "$0.00"
    assert card["data_source"] == "web"
    assert card["usage_url"] == OllamaCollector.DASHBOARD_URL
    assert card["detail"].endswith("Used this month <$0.01")


def test_parse_dashboard_plain_amounts():
    card = OllamaCollector()._parse_dashboard(_dashboard_html("$12.50", "$3.20"))[0]

    assert card["remaining"] == "$12.50"
    assert card["detail"].endswith("Used this month $3.20")


def test_parse_dashboard_matches_the_api_credits_card():
    api = OllamaCollector()._make_credits_card(12.5)
    web = OllamaCollector()._parse_dashboard(_dashboard_html("$12.50"))[0]

    differing = {k for k in api if k not in ("updated_at",) and api[k] != web.get(k)}
    assert differing == {"data_source", "usage_url", "detail"}


def test_parse_dashboard_missing_or_unreadable_usage_keeps_the_card():
    collector = OllamaCollector()

    card = collector._parse_dashboard('<span id="extra-usage-balance">$4.00</span>')[0]
    assert card["remaining"] == "$4.00"
    assert "Used this month" not in card["detail"]
    card = collector._parse_dashboard(_dashboard_html("$4.00", "lots"))[0]
    assert "Used this month" not in card["detail"]


@pytest.mark.parametrize("balance", ["", "n/a", "€4", "$"])
def test_parse_dashboard_unreadable_balance_yields_no_card(balance):
    assert OllamaCollector()._parse_dashboard(_dashboard_html(balance)) == []


def test_parse_dashboard_missing_element():
    assert OllamaCollector()._parse_dashboard("<html><body>Dashboard</body></html>") == []


def test_parse_dashboard_signed_out_page():
    collector = OllamaCollector()

    assert collector._parse_dashboard("<html><h1>Sign in to Ollama</h1></html>") == []
    assert collector._last_error_reason == "not_logged_in"


def _web_transport(dashboard):
    """Settings page = the live usage report; ``dashboard`` answers /dashboard."""
    requested: list[str] = []

    async def fake_http(client, method, url, **kwargs):
        requested.append(url)
        if url == OllamaCollector.DASHBOARD_URL:
            if isinstance(dashboard, Exception):
                raise dashboard
            return dashboard
        return _page_response(SETTINGS_FIXTURE.read_text(encoding="utf-8"))

    return fake_http, requested


@pytest.mark.asyncio
async def test_cookie_only_account_gets_the_dashboard_credits_card():
    """Entry point: cookie only, settings page shows no figure -> the dashboard credits
    card, and the profile lookup still sets ``verified_identity``."""
    collector = OllamaCollector(account_id="acc_test")
    collector.current_input_source = "sidecar"
    html = DASHBOARD_FIXTURE.read_text(encoding="utf-8")
    profile = PROFILE_FIXTURE.read_text(encoding="utf-8")
    dashboard_resp = _page_response(html)
    fake_http, requested = _web_transport(dashboard_resp)

    async def routed(client, method, url, **kwargs):
        if url == OllamaCollector.PROFILE_URL:
            requested.append(url)
            return _page_response(profile)
        return await fake_http(client, method, url, **kwargs)

    with (
        patch.object(
            collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
        ),
        patch("app.services.collectors.ollama.http_request_with_retry", new=routed),
    ):
        cards = await collector._get_ollama_web(MagicMock())

    assert [c["variant"] for c in cards] == ["credits"]
    assert cards[0]["input_source"] == "sidecar"
    assert collector.verified_identity == "user@example.com"
    assert OllamaCollector.DASHBOARD_URL in requested
    assert not any("usage/report" in u for u in requested)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dashboard",
    [_page_response("", 500), _page_response("<html></html>"), httpx.ConnectError("boom")],
    ids=["http-500", "no-figure", "unreachable"],
)
async def test_unreachable_dashboard_falls_back_to_the_existing_error(dashboard):
    collector = OllamaCollector(account_id="acc_test")
    fake_http, _ = _web_transport(dashboard)

    with (
        patch.object(
            collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
        ),
        patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
    ):
        assert await collector._get_ollama_web(MagicMock()) == []
        cards = await collector._error_handler()

    assert len(cards) == 1
    assert cards[0]["error_type"] == "missing_config"
    assert cards[0]["detail"] == OllamaCollector.ERROR_MESSAGES["usage_not_on_page"]


@pytest.mark.asyncio
async def test_api_card_wins_and_the_dashboard_is_not_requested():
    collector = OllamaCollector(account_id="acc_test")
    requested: list[str] = []

    async def fake_http(client, method, url, **kwargs):
        requested.append(url)
        if url == OllamaCollector.BALANCE_URL:
            return _make_response(_balance_body(balance=40, allowance=60))
        return _page_response("")

    with (
        patch.object(collector, "_get_api_key", new=AsyncMock(return_value="k")),
        patch.object(
            collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
        ),
        patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
    ):
        cards = await collector.collect(MagicMock())

    assert len(cards) == 1
    assert cards[0]["data_source"] == "api"
    assert OllamaCollector.DASHBOARD_URL not in requested


SETTINGS_FIXTURE = Path(__file__).parent.parent / "fixtures" / "ollama-settings-included-usage.html"
FIXTURE_MODELS = [
    "beam",
    "gemma4:31b",
    "gpt-oss:120b",
    "gpt-oss:20b",
    "nemotron-3-super",
    "nemotron-3-ultra",
]


@pytest.mark.parametrize(
    ("allowance", "tier"),
    [(60, "pro"), (300, "max"), (1000, "team"), (2.5, "free"), (12.34, "cloud")],
)
def test_tier_from_allowance(allowance, tier):
    cards = OllamaCollector()._build_cards_from_api_balance(
        _balance_body(balance=allowance, allowance=allowance)
    )
    assert cards[0]["tier"] == tier


class TestOllamaSettingsEnrichment:
    @staticmethod
    async def _run(settings_response, cookie="session=abc.def-123"):
        """Run _get_ollama_api with a mocked balance + settings transport."""
        collector = OllamaCollector(account_id="acc_test")
        requested: list[str] = []

        async def fake_http(client, method, url, **kwargs):
            requested.append(url)
            if url == OllamaCollector.BALANCE_URL:
                return _make_response(_balance_body(balance=40, allowance=60))
            if isinstance(settings_response, Exception):
                raise settings_response
            return settings_response

        with (
            patch.object(collector, "_get_api_key", new=AsyncMock(return_value="k")),
            patch.object(collector, "_get_cookie_header", new=AsyncMock(return_value=cookie)),
            patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
        ):
            collector.current_input_source = "config"
            cards = await collector._get_ollama_api(MagicMock())
        return cards, requested, collector

    @staticmethod
    def _page(text, status=200):
        resp = _make_response({}, status)
        resp.text = text
        return resp

    @pytest.mark.asyncio
    async def test_cookie_adds_free_models_and_keeps_input_source(self):
        cards, requested, collector = await self._run(self._page(SETTINGS_FIXTURE.read_text()))
        assert cards[0]["free_models"] == FIXTURE_MODELS
        assert cards[0]["tier"] == "pro"
        assert cards[0]["input_source"] == "config"
        # The identity lookup is side information: it follows the data requests.
        assert requested == [
            OllamaCollector.BALANCE_URL,
            collector.target_url,
            OllamaCollector.PROFILE_URL,
        ]

    @pytest.mark.asyncio
    async def test_plan_badge_wins_over_allowance_tier(self):
        html = (
            '<span>Included usage</span> <span class="capitalize">max</span>'
            + SETTINGS_FIXTURE.read_text()
        )
        cards, _, _ = await self._run(self._page(html))
        assert cards[0]["tier"] == "max"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response",
        [
            403,
            500,
            "signed-out",
            httpx.ConnectError("boom"),
        ],
    )
    async def test_failed_settings_fetch_leaves_card_unchanged(self, response):
        if response == 403 or response == 500:
            response = self._page("", response)
        elif response == "signed-out":
            response = self._page("<html><h1>Sign in</h1></html>")
        cards, _, _ = await self._run(response)
        assert len(cards) == 1
        assert cards[0]["tier"] == "pro"
        assert cards[0].get("free_models") is None

    @pytest.mark.asyncio
    async def test_no_cookie_never_requests_settings(self):
        cards, requested, _ = await self._run(self._page("x"), cookie=None)
        assert requested == [OllamaCollector.BALANCE_URL]
        assert cards[0]["tier"] == "pro"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http_500", "timeout"])
@pytest.mark.parametrize("order", [["api", "web"], ["web", "api"]], ids=["api-first", "web-first"])
async def test_api_key_with_a_failing_balance_call_is_not_told_to_add_a_key(failure, order):
    """An account that already has a key must not get "Add an Ollama API key".

    The balance call fails for another reason (a 500, a timeout) and the cookie
    path reads the Usage page. That must stay a parse error whichever order the
    strategies run in, including a persisted web-first order, so this goes through
    ``collect()`` rather than calling the strategies in a fixed sequence.
    """
    collector = OllamaCollector(account_id="acc_test")
    collector.apply_strategy_config([{"id": s_id, "enabled": True} for s_id in order])
    page = _make_response({})
    page.text = SETTINGS_FIXTURE.read_text(encoding="utf-8")

    async def fake_http(client, method, url, **kwargs):
        if url == OllamaCollector.BALANCE_URL:
            if failure == "timeout":
                raise httpx.TimeoutException("slow")
            return _make_response({}, 500)
        return page

    with (
        patch.object(collector, "_get_api_key", new=AsyncMock(return_value="k")),
        patch.object(
            collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
        ),
        patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
    ):
        cards = await collector.collect(MagicMock())

    assert len(cards) == 1
    assert cards[0]["error_type"] == "parse_error"
    assert cards[0]["detail"] != OllamaCollector.ERROR_MESSAGES["usage_not_on_page"]


@pytest.mark.asyncio
async def test_the_web_strategy_keeps_the_cookies_input_source_when_checking_for_a_key():
    """Resolving key presence must not overwrite where the cookie came from."""
    collector = OllamaCollector(account_id="acc_test")
    collector.current_input_source = "sidecar"

    async def fake_get_api_key():
        # Like the real method, record where the key came from.
        collector.current_input_source = "config"
        return "k"

    with patch.object(collector, "_get_api_key", new=fake_get_api_key):
        assert await collector._has_api_key() is True

    assert collector.current_input_source == "sidecar"


def test_usage_page_marker_accepts_either_quote_style():
    collector = OllamaCollector()

    for html in ('<div id="free-plan-models">x</div>', "<div id='free-plan-models'>x</div>"):
        assert collector._parse_html(html) == []
        assert collector._last_error_reason == "usage_not_on_page"
        collector._last_error_reason = "unknown"


# --- cookie identity from the profile page (#462) --------------------------

PROFILE_FIXTURE = Path(__file__).parent.parent / "fixtures" / "ollama-settings-profile.html"


def _html_response(text, status=200):
    resp = _make_response({}, status)
    resp.text = text
    return resp


class TestOllamaCookieIdentity:
    """The usage page has no ``header-email``; the profile page names the account."""

    @staticmethod
    def _routes(*, profile=None, settings=None, balance=None):
        """A transport keyed by URL; ``None`` for a route means the request must not happen."""
        requested: list[str] = []

        async def fake_http(client, method, url, **kwargs):
            requested.append(url)
            routes = {
                OllamaCollector.BALANCE_URL: balance,
                OllamaCollector.PROFILE_URL: profile,
                "https://ollama.com/settings": settings,
            }
            route = routes.get(url)
            assert route is not None, f"unexpected request to {url}"
            if isinstance(route, Exception):
                raise route
            return route

        return fake_http, requested

    @staticmethod
    def _profile():
        return _html_response(PROFILE_FIXTURE.read_text(encoding="utf-8"))

    @staticmethod
    def _settings():
        return _html_response(SETTINGS_FIXTURE.read_text(encoding="utf-8"))

    def test_the_profile_element_yields_the_normalised_email(self):
        html = PROFILE_FIXTURE.read_text(encoding="utf-8")

        assert OllamaCollector()._parse_profile_email(html) == "User@Example.com"

    def test_the_email_input_is_the_fallback(self):
        html = '<input id="email-input" type="email" value="a.b@example.org">'

        assert OllamaCollector()._parse_profile_email(html) == "a.b@example.org"

    @pytest.mark.parametrize(
        "html",
        [
            "",
            "<html><body>Profile</body></html>",
            '<span id="email-content">not an email</span>',
            '<span id="email-content">a@b</span>',
            '<input id="email-input" value="nope">',
        ],
    )
    def test_anything_that_is_not_an_email_is_ignored(self, html):
        assert OllamaCollector()._parse_profile_email(html) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "profile",
        [
            _make_response({}, 403),
            _make_response({}, 500),
            httpx.TimeoutException("slow"),
            _html_response("<html><h1>Sign in to Ollama</h1></html>"),
            _html_response("<html><body>Profile</body></html>"),
        ],
        ids=["403", "500", "timeout", "signed-out", "no-email"],
    )
    async def test_an_unreadable_profile_leaves_the_identity_unset(self, profile):
        collector = OllamaCollector(account_id="acc_test")
        fake_http, _ = self._routes(profile=profile)

        with patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http):
            await collector._promote_cookie_identity(MagicMock(), "session=abc.def-123")

        assert collector.verified_identity is None

    @pytest.mark.asyncio
    async def test_a_key_and_a_cookie_report_the_cookies_account(self):
        """Entry point: the API path fetches the settings page for the cookie, and now
        also records which account that cookie is signed into."""
        collector = OllamaCollector(account_id="acc_test")
        fake_http, requested = self._routes(
            balance=_make_response(_balance_body(balance=40, allowance=60)),
            settings=self._settings(),
            profile=self._profile(),
        )

        with (
            patch.object(collector, "_get_api_key", new=AsyncMock(return_value="k")),
            patch.object(
                collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
            ),
            patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
        ):
            collector.current_input_source = "config"
            cards = await collector._get_ollama_api(MagicMock())

        assert collector.verified_identity == "user@example.com"
        assert cards[0]["free_models"] == FIXTURE_MODELS
        assert cards[0]["input_source"] == "config"
        assert OllamaCollector.PROFILE_URL in requested

    @pytest.mark.asyncio
    async def test_a_failing_profile_does_not_cost_the_card_its_free_models(self):
        collector = OllamaCollector(account_id="acc_test")
        fake_http, _ = self._routes(
            balance=_make_response(_balance_body(balance=40, allowance=60)),
            settings=self._settings(),
            profile=httpx.TimeoutException("slow"),
        )

        with (
            patch.object(collector, "_get_api_key", new=AsyncMock(return_value="k")),
            patch.object(
                collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
            ),
            patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert collector.verified_identity is None
        assert cards[0]["free_models"] == FIXTURE_MODELS

    @pytest.mark.asyncio
    async def test_a_cookie_only_account_still_reports_its_identity_through_collect(self):
        """The usage page yields no cards (an error card), but the identity is recorded."""
        collector = OllamaCollector(account_id="acc_test")
        fake_http, _ = self._routes(settings=self._settings(), profile=self._profile())

        with (
            patch.object(collector, "_get_api_key", new=AsyncMock(return_value=None)),
            patch.object(
                collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
            ),
            patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
        ):
            cards = await collector.collect(MagicMock())

        assert cards[0]["error_type"] == "missing_config"
        assert collector.verified_identity == "user@example.com"

    @pytest.mark.asyncio
    async def test_no_cookie_never_requests_the_profile(self):
        collector = OllamaCollector(account_id="acc_test")
        fake_http, requested = self._routes(
            balance=_make_response(_balance_body(balance=40, allowance=60))
        )

        with (
            patch.object(collector, "_get_api_key", new=AsyncMock(return_value="k")),
            patch.object(collector, "_get_cookie_header", new=AsyncMock(return_value=None)),
            patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
        ):
            await collector._get_ollama_api(MagicMock())

        assert requested == [OllamaCollector.BALANCE_URL]
        assert collector.verified_identity is None

    @pytest.mark.asyncio
    async def test_a_prepaid_only_balance_still_records_the_identity(self):
        """No allowance card, so no settings fetch, but the cookie is still signed in."""
        collector = OllamaCollector(account_id="acc_test")
        fake_http, requested = self._routes(
            balance=_make_response({"purchased": {"balance_usd": 25}}),
            profile=self._profile(),
        )

        with (
            patch.object(collector, "_get_api_key", new=AsyncMock(return_value="k")),
            patch.object(
                collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
            ),
            patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert [c["variant"] for c in cards] == ["credits"]
        assert collector.verified_identity == "user@example.com"
        assert requested == [OllamaCollector.BALANCE_URL, OllamaCollector.PROFILE_URL]

    @pytest.mark.asyncio
    async def test_the_legacy_usage_fallback_still_records_the_identity(self):
        """A deployment without /api/balance takes the /api/usage path; the cookie is
        just as signed in there."""
        collector = OllamaCollector(account_id="acc_test")
        fake_http, requested = self._routes(
            balance=MagicMock(status_code=404), profile=self._profile()
        )
        legacy = AsyncMock(return_value=[{"variant": "percent"}])

        with (
            patch.object(collector, "_get_api_key", new=AsyncMock(return_value="k")),
            patch.object(
                collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
            ),
            patch.object(collector, "_get_ollama_usage_legacy", new=legacy),
            patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert cards == [{"variant": "percent"}]
        legacy.assert_awaited_once()
        assert collector.verified_identity == "user@example.com"
        assert requested == [OllamaCollector.BALANCE_URL, OllamaCollector.PROFILE_URL]

    @pytest.mark.asyncio
    async def test_a_signed_in_profile_with_an_email_form_and_a_login_link_still_reads(self):
        """``_looks_signed_out`` fires on any email form plus a /login link; the profile
        page has both, so the email element itself decides."""
        html = (
            PROFILE_FIXTURE.read_text(encoding="utf-8")
            + '<form><input type="email" name="email"></form><a href="/login">Log in</a>'
        )
        assert OllamaCollector()._looks_signed_out(html) is True
        collector = OllamaCollector(account_id="acc_test")
        fake_http, _ = self._routes(profile=_html_response(html))

        with patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http):
            await collector._promote_cookie_identity(MagicMock(), "session=abc.def-123")

        assert collector.verified_identity == "user@example.com"

    def test_an_unrelated_attribute_cannot_name_the_account(self):
        collector = OllamaCollector()

        assert (
            collector._parse_profile_email('<input id="email-input" data-value="d@evil.example">')
            is None
        )
        assert (
            collector._parse_profile_email(
                '<input id="email-input" data-value="d@evil.example" value="real@example.org">'
            )
            == "real@example.org"
        )

    @pytest.mark.asyncio
    async def test_the_web_strategy_requests_the_profile_after_the_settings_page(self):
        collector = OllamaCollector(account_id="acc_test")
        fake_http, requested = self._routes(settings=self._settings(), profile=self._profile())

        with (
            patch.object(collector, "_get_api_key", new=AsyncMock(return_value=None)),
            patch.object(
                collector, "_get_cookie_header", new=AsyncMock(return_value="session=abc.def-123")
            ),
            patch("app.services.collectors.ollama.http_request_with_retry", new=fake_http),
        ):
            await collector._get_ollama_web(MagicMock())

        # The settings page shows no figure here, so the dashboard is read before
        # the identity lookup.
        assert requested == [
            "https://ollama.com/settings",
            OllamaCollector.DASHBOARD_URL,
            OllamaCollector.PROFILE_URL,
        ]

    @pytest.mark.asyncio
    async def test_a_hanging_profile_is_cut_off_and_never_costs_more_than_the_bound(self):
        """A slow /settings/profile must not hold the quota collection back for the
        15 s page timeout: the lookup is bounded and leaves the identity unset."""
        collector = OllamaCollector(account_id="acc_test")

        async def hanging_http(client, method, url, **kwargs):
            await asyncio.sleep(30)

        with (
            patch.object(OllamaCollector, "IDENTITY_TIMEOUT_SECONDS", 0.05),
            patch("app.services.collectors.ollama.http_request_with_retry", new=hanging_http),
        ):
            started = asyncio.get_running_loop().time()
            await collector._promote_cookie_identity(MagicMock(), "session=abc.def-123")
            elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 2
        assert collector.verified_identity is None
