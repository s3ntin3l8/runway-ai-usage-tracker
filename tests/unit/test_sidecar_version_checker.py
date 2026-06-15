"""Unit tests for SidecarVersionChecker and is_update_available."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.sidecar_version_checker import (
    SidecarVersionChecker,
    is_update_available,
    parse_channel,
)


def _mock_client_by_url(responses: dict):
    """async-with httpx client whose .get dispatches by URL substring.

    responses maps a URL substring -> (status_code, payload). Unmatched URLs
    return a 404 with an empty body.
    """
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    async def _get(url, headers=None):
        for key, (status, payload) in responses.items():
            if key in url:
                resp = MagicMock()
                resp.status_code = status
                resp.json = MagicMock(return_value=payload)
                return resp
        resp = MagicMock()
        resp.status_code = 404
        resp.json = MagicMock(return_value={})
        return resp

    client.get = AsyncMock(side_effect=_get)
    return client


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

    @pytest.mark.asyncio
    async def test_follows_redirects(self):
        # Regression guard: the repo was renamed, so GitHub 301-redirects the
        # API. httpx must be told to follow, or the whole update check silently
        # fails (every sidecar stops being flagged).
        checker = SidecarVersionChecker()
        client = _mock_client(payload={"tag_name": "v1.4.2"})
        with patch(
            "app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client
        ) as mock_cls:
            await checker.check_now()
        assert mock_cls.call_args.kwargs.get("follow_redirects") is True


# ---------------------------------------------------------------------------
# parse_channel
# ---------------------------------------------------------------------------


class TestParseChannel:
    def test_edge_version_returns_sha(self):
        assert parse_channel("1.1.0+edge.abc1234") == ("edge", "abc1234")

    def test_stable_version(self):
        assert parse_channel("1.1.0") == ("stable", None)

    def test_none(self):
        assert parse_channel(None) == ("stable", None)

    def test_edge_marker_without_sha(self):
        assert parse_channel("1.1.0+edge.") == ("edge", None)


# ---------------------------------------------------------------------------
# is_update_available — edge channel (sha comparison)
# ---------------------------------------------------------------------------


class TestIsUpdateAvailableEdge:
    def test_flags_when_edge_tag_moved(self):
        # Embedded short sha is not a prefix of the current edge tag sha.
        assert is_update_available("1.1.0+edge.aaa1111", None, "bbb2222ffffffff") is True

    def test_no_update_when_same_edge_build(self):
        # Edge tag sha starts with the embedded short sha → same build.
        assert is_update_available("1.1.0+edge.aaa1111", None, "aaa1111ffffffff") is False

    def test_no_update_when_edge_head_unknown(self):
        assert is_update_available("1.1.0+edge.aaa1111", None, None) is False

    def test_no_update_when_embedded_sha_missing(self):
        assert is_update_available("1.1.0+edge.", None, "bbb2222ffffffff") is False

    def test_stable_path_ignores_edge_sha(self):
        # A stable build still compares by version even if an edge sha is cached.
        assert is_update_available("1.0.0", "1.1.0", "deadbeef") is True
        assert is_update_available("1.1.0", "1.1.0", "deadbeef") is False


# ---------------------------------------------------------------------------
# check_now — edge tag sha caching
# ---------------------------------------------------------------------------


class TestCheckNowEdge:
    @pytest.mark.asyncio
    async def test_caches_edge_sha_alongside_stable(self):
        checker = SidecarVersionChecker()
        client = _mock_client_by_url(
            {
                "releases/latest": (200, {"tag_name": "v1.4.2"}),
                "git/refs/tags/edge": (200, {"object": {"sha": "ccc3333ffffffff"}}),
            }
        )
        with patch("app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client):
            await checker.check_now()
        assert checker.get_latest() == "1.4.2"
        assert checker.get_latest_edge_sha() == "ccc3333ffffffff"

    @pytest.mark.asyncio
    async def test_missing_edge_tag_keeps_sha_none(self):
        checker = SidecarVersionChecker()
        client = _mock_client_by_url(
            {"releases/latest": (200, {"tag_name": "v1.4.2"})}  # edge ref → 404
        )
        with patch("app.services.sidecar_version_checker.httpx.AsyncClient", return_value=client):
            await checker.check_now()
        assert checker.get_latest() == "1.4.2"
        assert checker.get_latest_edge_sha() is None
