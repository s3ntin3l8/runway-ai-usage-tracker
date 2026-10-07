"""Unit tests for the optional Antigravity (agy) keep-alive thread.

The thread renews the one-hour agy access token with `agy models` — the only
renewal path available (no OAuth client_id reaches Runway). Verified behavior
it relies on: `agy models` rewrites the token file only once the access token
has lapsed, rotating the access token but never the refresh token.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.sidecar_pkg import keep_alive


def _write_token(path: Path, expiry: datetime) -> Path:
    path.write_text(
        json.dumps(
            {
                "auth_method": "consumer",
                "token": {
                    "access_token": "ya29.x",
                    "refresh_token": "1//r",
                    "expiry": expiry.isoformat(),
                },
            }
        )
    )
    return path


class TestRefreshDue:
    def test_missing_file_is_not_due(self, tmp_path):
        # A machine that never logged into agy must not be prodded into a
        # login flow by the keep-alive.
        assert keep_alive.refresh_due(tmp_path / "antigravity-oauth-token") is False

    def test_expired_token_is_due(self, tmp_path):
        tok = _write_token(tmp_path / "t", datetime.now(UTC) - timedelta(minutes=5))
        assert keep_alive.refresh_due(tok) is True

    def test_fresh_token_is_not_due(self, tmp_path):
        tok = _write_token(tmp_path / "t", datetime.now(UTC) + timedelta(hours=1))
        assert keep_alive.refresh_due(tok) is False

    def test_token_within_lead_window_is_due(self, tmp_path):
        tok = _write_token(tmp_path / "t", datetime.now(UTC) + timedelta(seconds=30))
        assert keep_alive.refresh_due(tok, lead=60) is True

    def test_unreadable_expiry_counts_as_due(self, tmp_path):
        tok = tmp_path / "t"
        tok.write_text("{not json")
        assert keep_alive.refresh_due(tok) is True

    def test_missing_expiry_field_counts_as_due(self, tmp_path):
        tok = tmp_path / "t"
        tok.write_text(json.dumps({"token": {"access_token": "ya29.x"}}))
        assert keep_alive.refresh_due(tok) is True


class TestCycleOnce:
    def test_waits_a_normal_tick_when_not_due(self, tmp_path):
        tok = _write_token(tmp_path / "t", datetime.now(UTC) + timedelta(hours=1))
        thread = keep_alive.KeepAliveThread(token_path=tok, tick_seconds=42)
        assert thread.cycle_once() == 42

    def test_runs_agy_models_when_due(self, tmp_path, monkeypatch):
        tok = _write_token(tmp_path / "t", datetime.now(UTC) - timedelta(minutes=1))
        calls: list[list[str]] = []
        monkeypatch.setattr(keep_alive, "run_refresh", lambda cmd: calls.append(cmd) or True)
        thread = keep_alive.KeepAliveThread(
            token_path=tok, command=["/usr/bin/agy", "models"], tick_seconds=42
        )

        assert thread.cycle_once() == 42
        assert calls == [["/usr/bin/agy", "models"]]

    def test_backs_off_after_a_failed_refresh(self, tmp_path, monkeypatch):
        tok = _write_token(tmp_path / "t", datetime.now(UTC) - timedelta(minutes=1))
        monkeypatch.setattr(keep_alive, "run_refresh", lambda cmd: False)
        thread = keep_alive.KeepAliveThread(
            token_path=tok, command=["agy", "models"], retry_tick_seconds=99
        )

        assert thread.cycle_once() == 99

    def test_backs_off_when_agy_is_not_installed(self, tmp_path, monkeypatch):
        tok = _write_token(tmp_path / "t", datetime.now(UTC) - timedelta(minutes=1))
        monkeypatch.setattr(keep_alive, "resolve_agy", lambda: None)
        thread = keep_alive.KeepAliveThread(token_path=tok, retry_tick_seconds=99)

        assert thread.cycle_once() == 99

    def test_backs_off_when_refresh_succeeds_but_the_file_stays_unreadable(
        self, tmp_path, monkeypatch
    ):
        # A corrupt file reads as "due" on every tick; even a CLI that exits 0
        # without repairing the expiry must not be invoked at the normal cadence.
        tok = tmp_path / "t"
        tok.write_text("{not json")
        monkeypatch.setattr(keep_alive, "run_refresh", lambda cmd: True)
        thread = keep_alive.KeepAliveThread(
            token_path=tok, command=["agy", "models"], tick_seconds=42, retry_tick_seconds=99
        )

        assert thread.cycle_once() == 99


class TestKeepAliveThread:
    def test_stop_joins_promptly_and_survives_failures(self, tmp_path, monkeypatch):
        tok = _write_token(tmp_path / "t", datetime.now(UTC) - timedelta(minutes=1))

        def explode(_cmd):
            raise RuntimeError("keep-alive must never take the sidecar down")

        monkeypatch.setattr(keep_alive, "run_refresh", explode)
        thread = keep_alive.KeepAliveThread(
            token_path=tok, command=["agy", "models"], tick_seconds=0.01, retry_tick_seconds=0.01
        )
        thread.start()
        time.sleep(0.05)
        thread.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_enable_is_visible_to_the_sidecar_warning(self, monkeypatch):
        monkeypatch.setattr(keep_alive, "_enabled", False)
        assert keep_alive.is_enabled() is False
        keep_alive.enable()
        assert keep_alive.is_enabled() is True


def test_run_refresh_reports_exit_codes_without_token_material(monkeypatch, caplog):
    """Failures log a truncated stderr tail; success never logs stdout (the
    CLI can echo account details there)."""

    class _Result:
        returncode = 3
        stderr = "auth error " + "x" * 500
        stdout = ""

    import logging

    monkeypatch.setattr(keep_alive.subprocess, "run", lambda *a, **k: _Result())
    with caplog.at_level(logging.WARNING):
        assert keep_alive.run_refresh(["agy", "models"]) is False
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    message = record.getMessage()
    assert "exited 3" in message
    # A 510-char stderr is truncated to the last 200 chars, never dumped whole.
    assert "x" * 201 not in message
    assert "ya29" not in message


def test_run_refresh_success_is_logged_at_info(monkeypatch, caplog):
    import logging

    class _Result:
        returncode = 0
        stderr = ""
        stdout = "model list"

    monkeypatch.setattr(keep_alive.subprocess, "run", lambda *a, **k: _Result())
    with caplog.at_level(logging.DEBUG):
        assert keep_alive.run_refresh(["agy", "models"]) is True
    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("renewed the agy login" in m for m in info)
    # stdout must never be echoed into the log
    assert not any("model list" in r.getMessage() for r in caplog.records)


class _FakeThread:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class TestKeepAliveController:
    def _controller(self, monkeypatch):
        monkeypatch.setattr(keep_alive, "_enabled", False)
        made: list[_FakeThread] = []

        def make():
            made.append(_FakeThread())
            return made[-1]

        return keep_alive.KeepAliveController(make), made

    def test_unarmed_reports_unknown_armed_reports_bool(self, monkeypatch):
        controller, _ = self._controller(monkeypatch)
        assert controller.reported() is None  # tray app: never armed
        controller.arm(False)
        assert controller.reported() is False
        controller.set_remote(True)
        assert controller.reported() is True
        controller.stop()
        assert controller.reported() is None

    def test_inert_until_armed(self, monkeypatch):
        controller, made = self._controller(monkeypatch)
        controller.set_remote(True)
        assert made == [] and keep_alive.is_enabled() is False

    def test_local_flag_starts_it(self, monkeypatch):
        controller, made = self._controller(monkeypatch)
        controller.arm(True)
        assert len(made) == 1 and made[0].started == 1
        assert keep_alive.is_enabled() is True and controller.effective is True

    def test_local_off_starts_nothing(self, monkeypatch):
        controller, made = self._controller(monkeypatch)
        controller.arm(False)
        assert made == [] and keep_alive.is_enabled() is False

    def test_remote_on_overrides_a_local_off(self, monkeypatch):
        controller, made = self._controller(monkeypatch)
        controller.arm(False)
        controller.set_remote(True)
        assert len(made) == 1 and keep_alive.is_enabled() is True

    def test_remote_off_overrides_a_local_on(self, monkeypatch):
        controller, made = self._controller(monkeypatch)
        controller.arm(True)
        controller.set_remote(False)
        assert made[0].stopped == 1 and keep_alive.is_enabled() is False

    def test_remote_none_falls_back_to_the_local_flag(self, monkeypatch):
        controller, made = self._controller(monkeypatch)
        controller.arm(True)
        controller.set_remote(False)
        controller.set_remote(None)  # operator cleared the override
        assert len(made) == 2 and made[1].started == 1
        assert keep_alive.is_enabled() is True

    def test_repeating_the_same_setting_is_a_noop(self, monkeypatch):
        controller, made = self._controller(monkeypatch)
        controller.arm(False)
        for _ in range(3):
            controller.set_remote(True)
        assert len(made) == 1 and made[0].started == 1

    def test_garbage_from_the_server_counts_as_no_preference(self, monkeypatch):
        controller, made = self._controller(monkeypatch)
        controller.arm(False)
        controller.set_remote("yes")
        assert made == [] and controller.effective is False

    def test_stop_ends_the_thread_and_disarms(self, monkeypatch):
        controller, made = self._controller(monkeypatch)
        controller.arm(True)
        controller.stop()
        assert made[0].stopped == 1 and keep_alive.is_enabled() is False
        controller.set_remote(True)  # disarmed: ignored
        assert len(made) == 1


def test_sidecar_applies_the_servers_setting_from_an_ingest_response(monkeypatch):
    from scripts import sidecar

    calls = []
    monkeypatch.setattr(sidecar._KEEP_ALIVE, "set_remote", lambda v: calls.append(v))
    runner = sidecar.DaemonRunner.__new__(sidecar.DaemonRunner)
    runner._apply_ingest_instructions({"keep_alive_desired": True}, [], False, False, False)
    runner._apply_ingest_instructions({"keep_alive_desired": None}, [], False, False, False)
    runner._apply_ingest_instructions({}, [], False, False, False)  # absent: untouched
    assert calls == [True, None]


def test_sidecar_builds_its_keep_alive_thread_with_the_xai_claude_and_codex_renewers():
    from scripts import sidecar
    from scripts.sidecar_pkg.anthropic_renewer import AnthropicRenewer
    from scripts.sidecar_pkg.codex_renewer import CodexRenewer
    from scripts.sidecar_pkg.xai_renewer import XaiRenewer

    thread = sidecar._make_keep_alive_thread()
    assert [type(r) for r in thread._renewers] == [XaiRenewer, AnthropicRenewer, CodexRenewer]
    # The Claude and Codex renewers are handed the sidecar's own discovery, re-evaluated on
    # every tick.
    assert thread._renewers[1]._targets is sidecar._claude_login_paths
    assert thread._renewers[2]._targets is sidecar._codex_login_paths


def test_codex_login_paths_follow_the_chatgpt_file_rule(tmp_path, monkeypatch):
    """~/.codex plus every extra login dir (CODEX_HOME / codex_home), re-read on each call."""
    from scripts import sidecar

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    default = home / ".codex" / "auth.json"
    default.write_text("{}")
    extra_dir = tmp_path / "work-codex"
    extra_dir.mkdir()
    extra = extra_dir / "auth.json"
    extra.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(extra_dir))

    paths = {p.resolve() for p in sidecar._codex_login_paths()}
    assert default.resolve() in paths and extra.resolve() in paths

    monkeypatch.delenv("CODEX_HOME")
    assert extra.resolve() not in {p.resolve() for p in sidecar._codex_login_paths()}


def test_claude_login_paths_follow_the_claude_file_rule(tmp_path, monkeypatch):
    """The renewer keeps exactly the logins the sidecar pushes: ~/.claude plus every extra
    login dir (CLAUDE_CONFIG_DIR / claude_config_dirs), re-read on each call."""
    from scripts import sidecar

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    default = home / ".claude" / ".credentials.json"
    default.write_text("{}")
    extra_dir = tmp_path / "work-claude"
    extra_dir.mkdir()
    extra = extra_dir / ".credentials.json"
    extra.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(extra_dir))

    paths = {p.resolve() for p in sidecar._claude_login_paths()}
    assert default.resolve() in paths and extra.resolve() in paths

    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert extra.resolve() not in {p.resolve() for p in sidecar._claude_login_paths()}
