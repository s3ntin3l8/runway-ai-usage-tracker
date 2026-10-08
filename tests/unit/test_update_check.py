"""Unit tests for the sidecar-side update check (scripts/sidecar_pkg/update_check.py)."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

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

    def test_numbered_beta_version(self):
        assert parse_channel("3.0.0-beta.1") == ("beta", None)

    @pytest.mark.parametrize(
        "version",
        [" 3.0.0-beta.1", "\ufeff3.0.0-beta.1", "vv3.0.0-beta.1"],
    )
    def test_normalizes_beta_version(self, version):
        assert parse_channel(version) == ("beta", None)


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


class TestCheckOnceBeta:
    def test_reports_newest_numbered_beta(self):
        releases = [
            {"tag_name": "v3.0.0-beta.2", "prerelease": True},
            {"tag_name": "v3.0.0-beta.1", "prerelease": True},
            {"tag_name": "v3.0.0-rc.1", "prerelease": True},
            {"tag_name": "v3.0.0", "prerelease": False},
        ]
        opener = _make_urlopen({"releases?per_page=100": releases})
        with patch("scripts.sidecar_pkg.update_check.request.urlopen", side_effect=opener):
            assert check_once("3.0.0-beta.1", channel="beta") == "v3.0.0-beta.2"

    def test_none_when_beta_head_is_current(self):
        releases = [{"tag_name": "v3.0.0-beta.1", "prerelease": True}]
        opener = _make_urlopen({"releases?per_page=100": releases})
        with patch("scripts.sidecar_pkg.update_check.request.urlopen", side_effect=opener):
            assert check_once("3.0.0-beta.1", channel="beta") is None

    def test_none_when_no_numbered_beta_exists(self):
        opener = _make_urlopen({"releases?per_page=100": []})
        with patch("scripts.sidecar_pkg.update_check.request.urlopen", side_effect=opener):
            assert check_once("3.0.0-beta.1", channel="beta") is None


class TestRecheckAfterFirstCheckIn:
    """The first update check runs before any check-in has delivered the fleet's
    auto-update flag; a check-in that changes it must trigger another check."""

    def test_poke_runs_another_check_without_waiting_the_interval(self):
        import threading

        from scripts.sidecar_pkg.update_check import UpdateCheckThread

        seen = []
        second = threading.Event()

        def on_available(desc):
            seen.append(desc)
            if len(seen) == 2:
                second.set()

        with patch("scripts.sidecar_pkg.update_check.check_once", return_value="v9"):
            t = UpdateCheckThread("1.0.0", on_update_available=on_available, interval=3600)
            t.start()
            try:
                deadline = time.monotonic() + 5
                while not seen and time.monotonic() < deadline:
                    time.sleep(0.01)
                t.poke()
                assert second.wait(5), "poke() did not trigger a second check"
            finally:
                t.stop()

    def test_stop_wakes_the_thread(self):
        from scripts.sidecar_pkg.update_check import UpdateCheckThread

        with patch("scripts.sidecar_pkg.update_check.check_once", return_value=None):
            t = UpdateCheckThread("1.0.0", interval=3600)
            t.start()
            t.stop()
            t._thread.join(5)
            assert not t._thread.is_alive()

    def test_ingest_triggers_a_recheck_when_the_fleet_flag_or_channel_changes(self, monkeypatch):
        from scripts import sidecar

        calls = []
        monkeypatch.setattr(sidecar, "_UPDATE_RECHECK", lambda: calls.append(1))
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_SERVER", False)
        monkeypatch.setattr(sidecar, "_UPDATE_CHANNEL", None)
        runner = sidecar.DaemonRunner.__new__(sidecar.DaemonRunner)

        runner._apply_ingest_instructions({"sidecar_auto_update": False}, [], False, False, False)
        assert calls == []  # nothing changed
        runner._apply_ingest_instructions({"sidecar_auto_update": True}, [], False, False, False)
        assert calls == [1]
        runner._apply_ingest_instructions({"sidecar_auto_update": True}, [], False, False, False)
        assert calls == [1]  # unchanged
        runner._apply_ingest_instructions(
            {"sidecar_auto_update": True, "sidecar_update_channel": "edge"}, [], False, False, False
        )
        assert calls == [1, 1]


class TestReportedUpdateSettings:
    """What the sidecar tells the dashboard it effectively uses."""

    def test_nothing_is_reported_before_the_first_check_in(self, monkeypatch):
        from scripts import sidecar

        monkeypatch.delenv("RUNWAY_UPDATE_CHANNEL", raising=False)
        monkeypatch.setattr(sidecar, "_UPDATE_CHANNEL", None)
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_LOCAL", None)
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_SERVER", False)
        assert sidecar._reported_update_settings() == (None, None)

    def test_the_fleet_values_are_reported_once_synced(self, monkeypatch):
        from scripts import sidecar

        monkeypatch.delenv("RUNWAY_UPDATE_CHANNEL", raising=False)
        monkeypatch.setattr(sidecar, "_UPDATE_CHANNEL", "beta")
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_LOCAL", None)
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_SERVER", True)
        assert sidecar._reported_update_settings() == ("beta", True)

    def test_local_config_and_env_win_and_are_reported_as_such(self, monkeypatch):
        from scripts import sidecar

        monkeypatch.setenv("RUNWAY_UPDATE_CHANNEL", "edge")
        monkeypatch.setattr(sidecar, "_UPDATE_CHANNEL", "stable")
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_LOCAL", False)
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_SERVER", True)
        assert sidecar._reported_update_settings() == ("edge", False)

    def test_local_auto_update_is_reported_even_before_the_first_check_in(self, monkeypatch):
        from scripts import sidecar

        monkeypatch.delenv("RUNWAY_UPDATE_CHANNEL", raising=False)
        monkeypatch.setattr(sidecar, "_UPDATE_CHANNEL", None)
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_LOCAL", True)
        assert sidecar._reported_update_settings() == (None, True)


class TestServerLogLevel:
    """The dashboard can raise the sidecar's log level without a restart."""

    def _reset(self, monkeypatch, base="INFO"):
        import logging

        from scripts import sidecar

        monkeypatch.setattr(sidecar, "_BASE_LOG_LEVEL", base)
        monkeypatch.setattr(sidecar, "_LOG_LEVEL_SERVER", None)
        root = logging.getLogger()
        monkeypatch.setattr(root, "level", logging.INFO)
        return sidecar, root

    def test_the_server_level_applies_immediately_and_null_restores_the_startup_level(
        self, monkeypatch
    ):
        import logging

        sidecar, root = self._reset(monkeypatch, base="WARNING")
        assert sidecar._apply_server_log_level("debug") is True
        assert root.level == logging.DEBUG and sidecar._effective_log_level() == "DEBUG"
        assert sidecar._apply_server_log_level("DEBUG") is False  # unchanged
        assert sidecar._apply_server_log_level(None) is True
        assert root.level == logging.WARNING and sidecar._effective_log_level() == "WARNING"

    def test_junk_falls_back_to_the_startup_level(self, monkeypatch):
        sidecar, root = self._reset(monkeypatch)
        assert sidecar._apply_server_log_level("chatty") is False
        assert sidecar._apply_server_log_level(5) is False
        assert sidecar._effective_log_level() == "INFO"

    def test_ingest_response_applies_the_level_and_an_absent_key_is_untouched(self, monkeypatch):
        sidecar, _root = self._reset(monkeypatch)
        runner = sidecar.DaemonRunner.__new__(sidecar.DaemonRunner)
        runner._apply_ingest_instructions({"log_level": "DEBUG"}, [], False, False, False)
        assert sidecar._effective_log_level() == "DEBUG"
        runner._apply_ingest_instructions({}, [], False, False, False)  # older server
        assert sidecar._effective_log_level() == "DEBUG"
        runner._apply_ingest_instructions({"log_level": None}, [], False, False, False)
        assert sidecar._effective_log_level() == "INFO"

    def test_setup_logging_records_the_configured_level_as_the_fallback(self, monkeypatch):
        import logging

        sidecar, root = self._reset(monkeypatch)
        monkeypatch.setattr(sidecar, "ensure_dirs", lambda: None)
        sidecar.setup_logging("error", False)
        try:
            assert sidecar._BASE_LOG_LEVEL == "ERROR" and root.level == logging.ERROR
            sidecar._apply_server_log_level("DEBUG")
            assert root.level == logging.DEBUG
        finally:
            monkeypatch.undo()
            logging.basicConfig(level=logging.INFO, force=True)


class TestAutoUpdateSkipReason:
    def test_it_names_where_the_off_setting_came_from(self, monkeypatch):
        from scripts import sidecar

        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_LOCAL", None)
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_SERVER", False)
        assert "the fleet default" in sidecar._auto_update_skip_reason()
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_LOCAL", False)
        assert "config.json" in sidecar._auto_update_skip_reason()

    def test_there_is_no_reason_when_auto_update_is_on(self, monkeypatch):
        from scripts import sidecar

        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_LOCAL", None)
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_SERVER", True)
        assert sidecar._auto_update_skip_reason() is None
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_LOCAL", True)
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_SERVER", False)
        assert sidecar._auto_update_skip_reason() is None
