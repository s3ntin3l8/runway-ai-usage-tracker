"""Unit tests for the sidecar-side update check (scripts/sidecar_pkg/update_check.py)."""

import json
from unittest.mock import MagicMock, patch

from scripts.sidecar_pkg.update_check import check_once, parse_channel


def _urlopen_returning(payload: dict):
    cm = MagicMock()
    reader = MagicMock(read=MagicMock(return_value=json.dumps(payload).encode()))
    cm.__enter__ = MagicMock(return_value=reader)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _make_urlopen(by_url: dict):
    """Dispatch urlopen by a substring of the requested URL; OSError otherwise."""

    def _open(req, timeout=None, context=None):
        url = getattr(req, "full_url", req)
        for key, payload in by_url.items():
            if key in url:
                return _urlopen_returning(payload)
        raise OSError("404 not found")

    return _open


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
# check_once
# ---------------------------------------------------------------------------


class TestCheckOnceStable:
    def test_reports_newer_stable(self):
        opener = _make_urlopen({"releases/latest": {"tag_name": "v1.2.0"}})
        with patch("scripts.sidecar_pkg.update_check.request.urlopen", side_effect=opener):
            assert check_once("1.1.0") == "v1.2.0"

    def test_none_when_up_to_date(self):
        opener = _make_urlopen({"releases/latest": {"tag_name": "v1.1.0"}})
        with patch("scripts.sidecar_pkg.update_check.request.urlopen", side_effect=opener):
            assert check_once("1.1.0") is None

    def test_none_on_network_failure(self):
        with patch(
            "scripts.sidecar_pkg.update_check.request.urlopen",
            side_effect=OSError("no network"),
        ):
            assert check_once("1.1.0") is None


class TestCheckOnceEdge:
    def test_reports_new_edge_build(self):
        opener = _make_urlopen({"git/refs/tags/edge": {"object": {"sha": "bbbbbbb2222ffff"}}})
        with patch("scripts.sidecar_pkg.update_check.request.urlopen", side_effect=opener):
            result = check_once("1.1.0+edge.aaa1111")
        assert result == "edge build bbbbbbb2222f"

    def test_none_when_same_edge_build(self):
        # Tag sha starts with the embedded short sha → same build.
        opener = _make_urlopen({"git/refs/tags/edge": {"object": {"sha": "aaa1111ffffffff"}}})
        with patch("scripts.sidecar_pkg.update_check.request.urlopen", side_effect=opener):
            assert check_once("1.1.0+edge.aaa1111") is None

    def test_stable_binary_on_edge_channel_falls_back_to_stable(self):
        # A stable build (no embedded sha) asked to track edge can't diff shas,
        # so it should still surface stable releases rather than go blind.
        opener = _make_urlopen({"releases/latest": {"tag_name": "v1.3.0"}})
        with patch("scripts.sidecar_pkg.update_check.request.urlopen", side_effect=opener):
            assert check_once("1.1.0", channel="edge") == "v1.3.0"
