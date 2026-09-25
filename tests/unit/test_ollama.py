from datetime import UTC, datetime, timedelta
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
    assert collector._current_input_source == "config"
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
    assert collector._current_input_source == "server"


@pytest.mark.asyncio
async def test_get_cookie_header_sidecar_key_fallback():
    """Sidecar rules push `cookie_session`; settings UI stores `session_cookie`."""
    collector = OllamaCollector()

    with patch(
        "app.services.collectors.ollama.credential_provider.get_provider_session_cookie",
        return_value=None,
    ):
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", ""):
            with patch(
                "app.services.collectors.ollama.token_cache.get",
                AsyncMock(return_value={"cookie_session": "sidecar-value"}),
            ):
                header = await collector._get_cookie_header()

    assert header == "session=sidecar-value; __Secure-session=sidecar-value"
    assert collector._current_input_source == "sidecar"

    with patch(
        "app.services.collectors.ollama.credential_provider.get_provider_session_cookie",
        return_value=None,
    ):
        with patch.object(settings, "OLLAMA_SESSION_TOKEN", ""):
            with patch(
                "app.services.collectors.ollama.token_cache.get",
                AsyncMock(return_value={"session_cookie": "ui-value", "cookie_session": "other"}),
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
# Mirrors the TestOllamaApiCollector coverage that lives in test_collectors.py
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


def _make_response(body, status=200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json = MagicMock(return_value=body)
    resp.text = str(body)
    return resp


class TestOllamaApiCollector:
    """Bearer `GET /api/usage` path (primary when an API key is present)."""

    @pytest.mark.asyncio
    async def test_api_strategy_emits_monthly_card(self):
        collector = OllamaCollector(account_id="acc_test")

        async def fake_get_token(*args, **kwargs):
            return "test-key"

        with (
            patch(
                "app.services.collectors.ollama.token_cache.get_with_metadata",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.services.collectors.ollama.token_cache.get_token",
                new_callable=AsyncMock,
                side_effect=fake_get_token,
            ),
            patch(
                "app.services.collectors.ollama.http_request_with_retry",
                new_callable=AsyncMock,
                return_value=_make_response(_usage_body(usage=0.002)),
            ),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert len(cards) == 1
        assert cards[0]["provider_id"] == "ollama"
        assert cards[0]["window_type"] == "monthly"
        assert cards[0]["unit_type"] == "percent"
        assert cards[0]["data_source"] == OllamaCollector.DATA_SOURCE_API

    @pytest.mark.asyncio
    async def test_api_strategy_no_key_returns_empty_for_fallback(self):
        """No API key configured → return [] so the base collector falls
        through to the cookie-scrape fallback. The collector does not
        emit an error card itself; that's the base collector's job."""
        collector = OllamaCollector(account_id="acc_test")

        with patch(
            "app.services.collectors.ollama.token_cache.get_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert cards == []

    @pytest.mark.asyncio
    async def test_api_strategy_401_sets_invalid_api_key(self):
        collector = OllamaCollector(account_id="acc_test")

        async def fake_get_token(*args, **kwargs):
            return "bad-key"

        with (
            patch(
                "app.services.collectors.ollama.token_cache.get_token",
                new_callable=AsyncMock,
                side_effect=fake_get_token,
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

    @pytest.mark.asyncio
    async def test_api_strategy_timeout_returns_empty(self):
        """httpx.TimeoutException → empty list, no _last_error_reason set
        (base default treats empty as error → _error_handler runs)."""
        collector = OllamaCollector(account_id="acc_test")

        async def fake_http(*args, **kwargs):
            raise httpx.TimeoutException("boom")

        with (
            patch(
                "app.services.collectors.ollama.token_cache.get_token",
                new_callable=AsyncMock,
                return_value="test-key",
            ),
            patch(
                "app.services.collectors.ollama.http_request_with_retry",
                side_effect=fake_http,
            ),
        ):
            cards = await collector._get_ollama_api(MagicMock())

        assert cards == []

    def test_build_cards_from_api_usage_fraction(self):
        """Usage as 0..1 fraction → percentage card (e.g. free tier)."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage(_usage_body(usage=0.45))
        assert len(cards) == 1
        card = cards[0]
        assert card["unit_type"] == "percent"
        assert card["pct_used"] == pytest.approx(45.0)
        assert card["limit_value"] == 1.0

    def test_build_cards_from_api_usage_absolute_count(self):
        """Usage as absolute number (e.g. paid tier with credit cap) →
        token-count card. ``pct_used`` is clamped to [0, 100] but is NOT
        forced to 100 — it's the numeric usage itself when usage > 1
        (the dashboard renders ``<N> usage`` in that mode)."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage(_usage_body(usage=42.5))
        assert len(cards) == 1
        card = cards[0]
        assert card["unit_type"] == "token"
        assert card["limit_value"] is None
        assert card["pct_used"] == 42.5
        assert card["used_value"] == 42.5

    def test_build_cards_from_api_usage_missing_monthly(self):
        """Free-tier / no-cap response — limits.monthly missing → empty
        list with _last_error_reason='missing_data'."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage({"limits": {}, "activity": {}})
        assert cards == []
        assert collector._last_error_reason == "missing_data"

    def test_build_cards_from_api_usage_invalid_usage(self):
        """Non-numeric usage value → empty list with missing_data."""
        collector = OllamaCollector(account_id="acc_test")
        cards = collector._build_cards_from_api_usage(
            {"limits": {"monthly": {"usage": "not-a-number"}}, "activity": {}}
        )
        assert cards == []
        assert collector._last_error_reason == "missing_data"
