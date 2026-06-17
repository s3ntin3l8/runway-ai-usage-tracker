"""Unit tests for app/core/utils.py"""

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.utils import (
    IdentityExtractor,
    PaceCalculator,
    extract_token_regex,
    http_request_with_retry,
    human_delta,
    safe_write_json,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_jwt(payload: dict) -> str:
    """Build a minimal unsigned JWT with the given payload."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{payload_b64}.fakesig"


def _make_mock_response(status_code: int, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    return resp


# ─── IdentityExtractor ────────────────────────────────────────────────────────


class TestIdentityExtractorJwtPayload:
    def test_valid_jwt_returns_payload_dict(self):
        payload = {"sub": "user123", "email": "test@example.com"}
        token = _make_jwt(payload)
        result = IdentityExtractor.extract_jwt_payload(token)
        assert result["sub"] == "user123"
        assert result["email"] == "test@example.com"

    def test_invalid_short_string_returns_empty_dict(self):
        assert IdentityExtractor.extract_jwt_payload("not_a_jwt") == {}

    def test_only_two_parts_returns_empty_dict(self):
        # Two dots => 3 parts is valid, one dot => 2 parts is invalid
        assert IdentityExtractor.extract_jwt_payload("header.") == {}

    def test_handles_missing_padding(self):
        # Build a JWT whose payload has padding stripped — the function should fix it
        payload = {"sub": "abc"}
        token = _make_jwt(payload)  # already strips padding
        result = IdentityExtractor.extract_jwt_payload(token)
        assert result == {"sub": "abc"}

    def test_non_base64_payload_returns_empty_dict(self):
        result = IdentityExtractor.extract_jwt_payload("header.!!!invalid!!!.sig")
        assert result == {}


class TestIdentityExtractorGetEmail:
    def test_returns_email_claim(self):
        token = _make_jwt({"email": "user@example.com", "sub": "123"})
        assert IdentityExtractor.get_email_from_jwt(token) == "user@example.com"

    def test_returns_none_if_no_email_claim(self):
        token = _make_jwt({"sub": "123"})
        assert IdentityExtractor.get_email_from_jwt(token) is None


class TestIdentityExtractorGetClientId:
    def test_returns_azp_claim(self):
        token = _make_jwt({"azp": "my-client-id", "aud": "other-aud"})
        assert IdentityExtractor.get_client_id_from_jwt(token) == "my-client-id"

    def test_falls_back_to_aud_when_no_azp(self):
        token = _make_jwt({"aud": "my-audience"})
        assert IdentityExtractor.get_client_id_from_jwt(token) == "my-audience"

    def test_returns_none_when_neither_azp_nor_aud(self):
        token = _make_jwt({"sub": "user"})
        assert IdentityExtractor.get_client_id_from_jwt(token) is None


class TestIdentityExtractorExtractJwtExp:
    def test_extracts_exp_from_valid_jwt(self):
        exp = 1_780_000_000.0
        assert IdentityExtractor.extract_jwt_exp(_make_jwt({"exp": exp})) == exp

    def test_returns_none_when_exp_missing(self):
        assert IdentityExtractor.extract_jwt_exp(_make_jwt({"sub": "x"})) is None

    def test_returns_none_for_malformed_or_opaque_token(self):
        assert IdentityExtractor.extract_jwt_exp("not.a.jwt.with.extra.parts") is None
        assert IdentityExtractor.extract_jwt_exp("onlyone") is None
        assert IdentityExtractor.extract_jwt_exp("") is None
        assert IdentityExtractor.extract_jwt_exp("sk-ant-api03-abcdefg") is None

    def test_coerces_string_exp_to_float(self):
        assert (
            IdentityExtractor.extract_jwt_exp(_make_jwt({"exp": "1780000000"})) == 1_780_000_000.0
        )


class TestIdentityExtractorExpFromTokens:
    def test_reads_exp_from_oauth_token(self):
        tokens = {"oauth_token": _make_jwt({"exp": 100.0})}
        assert IdentityExtractor.exp_from_tokens(tokens) == 100.0

    def test_prefers_oauth_token_over_id_token(self):
        tokens = {
            "oauth_token": _make_jwt({"exp": 100.0}),
            "id_token": _make_jwt({"exp": 200.0}),
        }
        assert IdentityExtractor.exp_from_tokens(tokens) == 100.0

    def test_falls_through_to_id_token_when_oauth_is_opaque(self):
        tokens = {"oauth_token": "opaque-no-exp", "id_token": _make_jwt({"exp": 200.0})}
        assert IdentityExtractor.exp_from_tokens(tokens) == 200.0

    def test_returns_none_when_no_field_carries_exp(self):
        assert IdentityExtractor.exp_from_tokens({"api_key": "sk-opaque"}) is None
        assert IdentityExtractor.exp_from_tokens({}) is None

    def test_skips_empty_values(self):
        tokens = {"oauth_token": "", "access_token": _make_jwt({"exp": 50.0})}
        assert IdentityExtractor.exp_from_tokens(tokens) == 50.0

    def test_prefers_expiry_date_ms_over_jwt_exp(self):
        # expiry_date (ms epoch) tracks the access token and outranks a stale
        # id_token exp — Gemini's opaque access token has no JWT exp of its own.
        tokens = {
            "id_token": _make_jwt({"exp": 100.0}),  # seconds
            "expiry_date": "500000",  # ms → 500.0 s
        }
        assert IdentityExtractor.exp_from_tokens(tokens) == 500.0

    def test_falls_back_to_jwt_exp_when_expiry_date_unparseable(self):
        tokens = {"id_token": _make_jwt({"exp": 100.0}), "expiry_date": "not-a-number"}
        assert IdentityExtractor.exp_from_tokens(tokens) == 100.0


# ─── PaceCalculator ───────────────────────────────────────────────────────────


class TestPaceCalculatorEstimateLongevity:
    def test_pct_used_zero_returns_stable(self):
        assert PaceCalculator.estimate_longevity(0, None) == "Stable"

    def test_no_reset_at_returns_sustainable(self):
        assert PaceCalculator.estimate_longevity(50, None) == "Sustainable"

    def test_reset_already_passed_returns_pending_reset(self):
        past = datetime.now(UTC) - timedelta(hours=1)
        assert PaceCalculator.estimate_longevity(50, past) == "Pending Reset"

    def test_remaining_pct_zero_returns_exhausted(self):
        future = datetime.now(UTC) + timedelta(hours=1)
        assert PaceCalculator.estimate_longevity(100, future) == "Exhausted"

    def test_remaining_pct_5_returns_fast_burn(self):
        future = datetime.now(UTC) + timedelta(hours=1)
        assert PaceCalculator.estimate_longevity(95, future) == "Fast Burn"

    def test_remaining_pct_20_returns_moderate_burn(self):
        future = datetime.now(UTC) + timedelta(hours=1)
        assert PaceCalculator.estimate_longevity(80, future) == "Moderate Burn"

    def test_remaining_pct_50_returns_sustainable(self):
        future = datetime.now(UTC) + timedelta(hours=1)
        assert PaceCalculator.estimate_longevity(50, future) == "Sustainable"

    def test_reset_at_without_tzinfo_gets_utc_applied(self):
        # naive datetime in future — should be treated as UTC
        future_naive = datetime.now() + timedelta(hours=2)
        # Should not raise and should return a valid pace
        result = PaceCalculator.estimate_longevity(50, future_naive)
        assert result in ("Sustainable", "Moderate Burn", "Fast Burn", "Exhausted", "Pending Reset")


# ─── human_delta ──────────────────────────────────────────────────────────────


class TestHumanDelta:
    def test_none_returns_dash(self):
        assert human_delta(None) == "—"

    def test_past_datetime_returns_just_now(self):
        past = datetime.now(UTC) - timedelta(seconds=10)
        assert human_delta(past) == "Just now"

    def test_less_than_60s_returns_seconds(self):
        future = datetime.now(UTC) + timedelta(seconds=30)
        result = human_delta(future)
        assert result.endswith("s")
        assert "30" in result or int(result.rstrip("s")) <= 30

    def test_less_than_3600s_returns_minutes(self):
        future = datetime.now(UTC) + timedelta(minutes=5, seconds=5)
        result = human_delta(future)
        assert result.endswith("m")
        assert "5" in result

    def test_less_than_86400s_returns_hours_and_minutes(self):
        future = datetime.now(UTC) + timedelta(hours=2, minutes=30)
        result = human_delta(future)
        assert "h" in result and "m" in result
        assert "2h" in result

    def test_more_than_86400s_returns_days_and_hours(self):
        future = datetime.now(UTC) + timedelta(days=1, hours=6)
        result = human_delta(future)
        assert "d" in result and "h" in result
        assert "1d" in result

    def test_datetime_without_tzinfo_is_handled(self):
        naive = datetime.now() + timedelta(hours=1)
        # Should not raise
        result = human_delta(naive)
        assert isinstance(result, str)


# ─── extract_token_regex ──────────────────────────────────────────────────────


class TestExtractTokenRegex:
    def test_finds_token_after_prefix(self):
        detail = "Bearer abc123 · more info"
        result = extract_token_regex(detail, "Bearer")
        assert result == "abc123"

    def test_returns_none_if_prefix_not_found(self):
        detail = "No token here"
        result = extract_token_regex(detail, "Bearer")
        assert result is None

    def test_extracts_token_with_special_prefix(self):
        detail = "sk-ant-api03: abcXYZ info"
        result = extract_token_regex(detail, "sk-ant-api03:")
        assert result == "abcXYZ"


# ─── http_request_with_retry ─────────────────────────────────────────────────


class TestHttpRequestWithRetry:
    async def test_success_on_first_try(self):
        client = MagicMock(spec=httpx.AsyncClient)
        resp = _make_mock_response(200)
        client.request = AsyncMock(return_value=resp)

        result = await http_request_with_retry(client, "GET", "https://example.com")
        assert result.status_code == 200
        assert client.request.call_count == 1

    async def test_429_then_success_retries(self):
        client = MagicMock(spec=httpx.AsyncClient)
        resp_429 = _make_mock_response(429)
        resp_200 = _make_mock_response(200)
        client.request = AsyncMock(side_effect=[resp_429, resp_200])

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await http_request_with_retry(
                client, "GET", "https://example.com", max_retries=3, initial_delay=0.1
            )

        assert result.status_code == 200
        assert client.request.call_count == 2
        mock_sleep.assert_called_once()

    async def test_429_with_retry_after_header_uses_that_wait_time(self):
        client = MagicMock(spec=httpx.AsyncClient)
        resp_429 = _make_mock_response(429, headers={"Retry-After": "2"})
        resp_200 = _make_mock_response(200)
        client.request = AsyncMock(side_effect=[resp_429, resp_200])

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await http_request_with_retry(
                client, "GET", "https://example.com", max_retries=3, initial_delay=0.1
            )

        # Wait time should be 2 + 0.5 buffer = 2.5
        mock_sleep.assert_called_once_with(2.5)

    async def test_429_wait_time_over_5s_aborts_immediately(self):
        client = MagicMock(spec=httpx.AsyncClient)
        # Retry-After=10 → wait_time=10.5 > 5.0 → abort
        resp_429 = _make_mock_response(429, headers={"Retry-After": "10"})
        client.request = AsyncMock(return_value=resp_429)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await http_request_with_retry(
                client, "GET", "https://example.com", max_retries=3, initial_delay=0.1
            )

        assert result.status_code == 429
        # Should NOT have slept — aborted before sleeping
        mock_sleep.assert_not_called()
        # Only one request made
        assert client.request.call_count == 1

    async def test_all_attempts_exhausted_returns_last_429(self):
        client = MagicMock(spec=httpx.AsyncClient)
        resp_429 = _make_mock_response(429)
        client.request = AsyncMock(return_value=resp_429)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await http_request_with_retry(
                client, "GET", "https://example.com", max_retries=3, initial_delay=0.1
            )

        assert result.status_code == 429
        assert client.request.call_count == 3

    async def test_429_with_retry_on_429_false_returns_immediately(self):
        client = MagicMock(spec=httpx.AsyncClient)
        resp_429 = _make_mock_response(429)
        client.request = AsyncMock(return_value=resp_429)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await http_request_with_retry(
                client, "GET", "https://example.com", retry_on_429=False
            )

        assert result.status_code == 429
        mock_sleep.assert_not_called()
        assert client.request.call_count == 1

    async def test_non_429_exception_on_non_final_attempt_retries(self):
        client = MagicMock(spec=httpx.AsyncClient)
        resp_200 = _make_mock_response(200)
        client.request = AsyncMock(side_effect=[httpx.ConnectError("connection refused"), resp_200])

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await http_request_with_retry(
                client, "GET", "https://example.com", max_retries=3, initial_delay=0.1
            )

        assert result.status_code == 200

    async def test_non_429_exception_on_final_attempt_raises(self):
        client = MagicMock(spec=httpx.AsyncClient)
        client.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.ConnectError):
                await http_request_with_retry(
                    client, "GET", "https://example.com", max_retries=2, initial_delay=0.1
                )


# ─── safe_write_json ─────────────────────────────────────────────────────────


class TestSafeWriteJson:
    def test_writes_valid_json_to_path(self, tmp_path):
        data = {"key": "value", "number": 42}
        out_file = str(tmp_path / "subdir" / "output.json")
        safe_write_json(out_file, data)
        assert os.path.exists(out_file)

    def test_file_content_is_valid_json_matching_input(self, tmp_path):
        data = {"hello": "world", "nested": {"a": 1}}
        out_file = str(tmp_path / "output.json")
        safe_write_json(out_file, data)
        with open(out_file) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_handles_write_failure_gracefully(self, tmp_path):
        data = {"key": "value"}
        out_file = str(tmp_path / "output.json")

        with patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                safe_write_json(out_file, data)

        # Original file should not exist (we never wrote it successfully)
        # and temp file should be cleaned up
        assert not os.path.exists(out_file)
