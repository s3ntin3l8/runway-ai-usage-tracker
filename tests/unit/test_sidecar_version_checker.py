"""Unit tests for SidecarVersionChecker and is_update_available."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.sidecar_version_checker import (
    SidecarVersionChecker,
    is_update_available,
)


def _mock_client(*, status_code: int = 200, payload: dict | None = None, raise_exc=None):
    """Build a MagicMock that behaves like an `async with httpx.AsyncClient(...)`."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    if raise_exc is not None:
        client.get = AsyncMock(side_effect=raise_exc)
    else:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json = MagicMock(return_value=payload or {})
        client.get = AsyncMock(return_value=resp)
    return client


# ---------------------------------------------------------------------------
# is_update_available
# ---------------------------------------------------------------------------


class TestIsUpdateAvailable:
    def test_returns_true_when_latest_is_newer(self):
        assert is_update_available("1.0.0", "1.0.1") is True
        assert is_update_available("1.0.0", "1.1.0") is True
        assert is_update_available("1.0.0", "2.0.0") is True

    def test_returns_false_when_versions_equal(self):
        assert is_update_available("1.2.3", "1.2.3") is False

    def test_returns_false_when_current_is_newer(self):
        # Local dev build ahead of published release shouldn't be flagged.
        assert is_update_available("1.5.0", "1.4.0") is False

    def test_returns_false_when_latest_unknown(self):
        assert is_update_available("1.0.0", None) is False

    def test_returns_false_when_current_missing(self):
        assert is_update_available(None, "1.2.3") is False
        assert is_update_available("", "1.2.3") is False

    def test_returns_false_when_current_unparseable(self):
        # Unknown shape — refuse to flag rather than noisily false-positive.
        assert is_update_available("dev-snapshot", "1.2.3") is False

    def test_returns_false_when_latest_unparseable(self):
        assert is_update_available("1.0.0", "not-a-version") is False


# ---------------------------------------------------------------------------
# SidecarVersionChecker.check_now
# ---------------------------------------------------------------------------


class TestCheckNow:
    @pytest.mark.asyncio
    async def test_caches_tag_on_successful_response(self):
        checker = SidecarVersionChecker()
        client = _mock_client(payload={"tag_name": "v1.4.2"})
        with patch("app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client):
            result = await checker.check_now()
        assert result == "1.4.2"
        assert checker.get_latest() == "1.4.2"

    @pytest.mark.asyncio
    async def test_strips_v_prefix(self):
        checker = SidecarVersionChecker()
        client = _mock_client(payload={"tag_name": "v2.0.0"})
        with patch("app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client):
            await checker.check_now()
        assert checker.get_latest() == "2.0.0"

    @pytest.mark.asyncio
    async def test_accepts_tag_without_v_prefix(self):
        checker = SidecarVersionChecker()
        client = _mock_client(payload={"tag_name": "1.0.0"})
        with patch("app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client):
            await checker.check_now()
        assert checker.get_latest() == "1.0.0"

    @pytest.mark.asyncio
    async def test_keeps_cache_on_http_error(self):
        checker = SidecarVersionChecker()
        # Seed with a known good value.
        checker._latest = "1.0.0"
        client = _mock_client(status_code=503, payload={})
        with patch("app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client):
            result = await checker.check_now()
        # HTTP failure → cache untouched.
        assert result == "1.0.0"
        assert checker.get_latest() == "1.0.0"

    @pytest.mark.asyncio
    async def test_keeps_cache_on_network_exception(self):
        checker = SidecarVersionChecker()
        checker._latest = "1.0.0"
        client = _mock_client(raise_exc=ConnectionError("no network"))
        with patch("app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client):
            await checker.check_now()
        assert checker.get_latest() == "1.0.0"

    @pytest.mark.asyncio
    async def test_keeps_cache_when_tag_name_missing_or_empty(self):
        checker = SidecarVersionChecker()
        checker._latest = "1.0.0"
        client = _mock_client(payload={"tag_name": ""})
        with patch("app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client):
            await checker.check_now()
        assert checker.get_latest() == "1.0.0"

    @pytest.mark.asyncio
    async def test_returns_none_when_never_succeeded(self):
        checker = SidecarVersionChecker()
        client = _mock_client(raise_exc=RuntimeError("boom"))
        with patch("app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client):
            result = await checker.check_now()
        assert result is None
        assert checker.get_latest() is None
