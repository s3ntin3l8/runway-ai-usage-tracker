"""Unit tests for sidecar critical bug fixes and DaemonRunner."""

import base64
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Import sidecar as a module (it lives in scripts/, not a package)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import sidecar

_REPO_ROOT = Path(__file__).parent.parent.parent


def _gemini_file_mapping(rules: list) -> dict:
    """The json-file rule mapping from a provider's detection rules."""
    for rule in rules:
        if rule.get("type") == "file" and rule.get("format") == "json":
            return rule.get("mapping", {})
    return {}


class TestGeminiCredentialMapping:
    """Regression: the Gemini account email lives inside the OAuth id_token JWT.

    If the sidecar credential mapping omits id_token, the server can't derive the
    email and resolves account_id="default" — which split the dashboard into two
    Gemini cards (one quota-only "default", one stale email-keyed). Both the baked
    sidecar registry and the canonical registry.json must ship the id_token.
    """

    def test_baked_sidecar_registry_ships_id_token(self):
        gemini = sidecar.__REGISTRY__["providers"]["gemini"]
        mapping = _gemini_file_mapping(gemini["rules"])
        assert mapping.get("id_token") == "id_token"
        assert mapping.get("refresh_token") == "refresh_token"
        # expiry_date is the freshness signal that stops a stale local token from
        # clobbering a server-refreshed one (opaque ya29.* tokens carry no exp).
        assert mapping.get("expiry_date") == "expiry_date"

    def test_canonical_registry_ships_id_token(self):
        registry = json.loads((_REPO_ROOT / "app" / "core" / "registry.json").read_text())
        gemini = registry["providers"]["gemini"]
        mapping = _gemini_file_mapping(gemini["rules"])
        assert mapping.get("id_token") == "id_token"
        assert mapping.get("refresh_token") == "refresh_token"
        assert mapping.get("expiry_date") == "expiry_date"
        # The email is not a top-level field in oauth_creds.json — it must come
        # from the id_token, never a phantom "email" -> account_id mapping.
        assert "email" not in mapping


class TestQueueRotate:
    """C3: queue_rotate must not crash when called with no arguments."""

    def test_queue_rotate_no_args_does_not_crash(self, tmp_path):
        """queue_rotate() with no args must not raise TypeError."""
        with patch.object(sidecar, "get_queue_dir", return_value=tmp_path):
            # Previously crashed with TypeError: NoneType * 1024
            sidecar.queue_rotate()

    def test_queue_rotate_no_args_uses_default_10mb_limit(self, tmp_path):
        """queue_rotate() with no args uses 10 MB as the size limit (small file survives)."""
        queue_file = tmp_path / "2026-01-01.jsonl"
        queue_file.write_text('{"ts": 1, "payload": {}}\n')

        with patch.object(sidecar, "get_queue_dir", return_value=tmp_path):
            sidecar.queue_rotate()

        # Small file must survive rotation
        assert queue_file.exists()

    def test_queue_push_does_not_crash(self, tmp_path):
        """queue_push must queue a payload without crashing."""
        with patch.object(sidecar, "get_queue_dir", return_value=tmp_path):
            with patch.object(sidecar, "ensure_dirs"):
                sidecar.queue_push({"provider": "test", "metrics": []})

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        entry = json.loads(files[0].read_text().strip())
        assert entry["payload"] == {"provider": "test", "metrics": []}


class TestWindowsCredCache:
    """C4: _windows_cred_cache must be a dict, not None, to support item assignment."""

    def test_windows_cred_cache_is_dict_not_none(self):
        """_windows_cred_cache must be initialized as a dict."""
        assert isinstance(sidecar._windows_cred_cache, dict), (
            f"_windows_cred_cache is {type(sidecar._windows_cred_cache)}, expected dict"
        )

    def test_get_windows_credential_write_does_not_crash(self):
        """Writing to _windows_cred_cache must not raise TypeError."""
        try:
            sidecar._windows_cred_cache["test_target"] = ("password", time.time() + 300)
        except TypeError as e:
            pytest.fail(f"Writing to _windows_cred_cache raised TypeError: {e}")
        finally:
            sidecar._windows_cred_cache.pop("test_target", None)

    def test_windows_cred_cache_stays_dict_after_clear(self):
        """After assigning _windows_cred_cache = {}, writes must not crash."""
        # Simulate what shutdown_sidecar or start_daemon_mode does
        original = sidecar._windows_cred_cache
        try:
            sidecar._windows_cred_cache = {}  # simulate reset
            # Write must not crash
            sidecar._windows_cred_cache["test_target"] = ("pw", time.time() + 300)
            assert "test_target" in sidecar._windows_cred_cache
        finally:
            sidecar._windows_cred_cache = original

    def test_no_none_assignments_to_cred_cache_at_runtime(self):
        """shutdown_sidecar and start_daemon_mode must reset to {} not None."""
        import inspect
        import re

        source = inspect.getsource(sidecar)
        # Count assignments of _windows_cred_cache = None (should be 0 after fix)
        matches = re.findall(r"_windows_cred_cache\s*=\s*None", source)
        assert len(matches) == 0, (
            f"Found {len(matches)} assignment(s) of _windows_cred_cache = None: {matches}"
        )


# ---------------------------------------------------------------------------
# DaemonRunner tests
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = {
    "api_url": "http://localhost:8765",
    "api_key": "test-key",
    "retry_attempts": 1,
    "retry_backoff_seconds": 0,
}

FAKE_METRICS = [{"provider": "test", "value": 1}]


def _make_runner(**kwargs) -> sidecar.DaemonRunner:
    """Helper: create a DaemonRunner with MINIMAL_CONFIG."""
    config = {**MINIMAL_CONFIG, **kwargs}
    return sidecar.DaemonRunner(config)


class TestDaemonRunnerInitialState:
    """DaemonRunner starts in 'starting' status."""

    def test_initial_status_is_starting(self):
        runner = _make_runner()
        assert runner.status == "starting"

    def test_initial_attributes_are_none(self):
        runner = _make_runner()
        assert runner.last_cycle_at is None
        assert runner.last_metrics_count == 0
        assert runner.last_http_code is None
        assert runner.last_error is None


class TestDaemonRunnerRunOnceSuccess:
    """run_once() with a successful HTTP 2xx response."""

    def test_run_once_success_status_ok(self):
        runner = _make_runner()
        with (
            patch.object(sidecar, "run_collection", return_value=(FAKE_METRICS, [], 0)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(True, {"poll_providers": []}, 200),
            ),
            patch.object(sidecar, "queue_flush"),
        ):
            result = runner.run_once()

        assert result is True
        assert runner.status == "ok"
        assert runner.last_http_code == 200
        assert runner.last_metrics_count == len(FAKE_METRICS)
        assert runner.last_error is None
        assert runner.last_cycle_at is not None

    def test_run_once_empty_metrics_still_ok(self):
        """No metrics collected → still 'ok', heartbeat HTTP call made."""
        runner = _make_runner()
        with (
            patch.object(sidecar, "run_collection", return_value=([], [], 0)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(True, {"poll_providers": []}, 200),
            ),
        ):
            result = runner.run_once()

        assert result is True
        assert runner.status == "ok"
        assert runner.last_metrics_count == 0
        assert runner.last_http_code == 200


class TestDaemonRunnerRunOnceFailure:
    """run_once() with a non-2xx HTTP response."""

    def test_run_once_http_500_status_err(self):
        runner = _make_runner()
        with (
            patch.object(sidecar, "run_collection", return_value=(FAKE_METRICS, [], 0)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(False, "Internal Server Error", 500),
            ),
            patch.object(sidecar, "queue_flush"),
            patch.object(sidecar, "queue_push"),
        ):
            result = runner.run_once()

        assert result is False
        assert runner.status == "err"
        assert runner.last_http_code == 500
        assert runner.last_error is not None

    def test_run_once_exception_status_err(self):
        """An unexpected exception in run_collection → status 'err'."""
        runner = _make_runner()
        with patch.object(sidecar, "run_collection", side_effect=RuntimeError("boom")):
            result = runner.run_once()

        assert result is False
        assert runner.status == "err"
        assert "boom" in runner.last_error


class TestDaemonRunnerQueuedStatus:
    """When HTTP fails, payload is queued → status 'warn'."""

    def test_run_once_failure_queues_payload(self):
        runner = _make_runner()
        queued = []
        with (
            patch.object(sidecar, "run_collection", return_value=(FAKE_METRICS, [], 0)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(False, "timeout", 0),
            ),
            patch.object(sidecar, "queue_flush"),
            patch.object(sidecar, "queue_push", side_effect=lambda p: queued.append(p)),
        ):
            runner.run_once()

        assert runner.status == "warn"
        assert len(queued) == 1


class TestDaemonRunnerPauseResume:
    """pause() / resume() cycle."""

    def test_pause_sets_paused_status(self):
        runner = _make_runner()
        runner.pause()
        assert runner.status == "paused"

    def test_resume_after_pause_restores_starting(self):
        """Resuming before any cycle → 'starting'."""
        runner = _make_runner()
        runner.pause()
        runner.resume()
        assert runner.status == "starting"

    def test_resume_after_successful_cycle(self):
        """Resuming after a completed cycle → 'ok'."""
        runner = _make_runner()
        with (
            patch.object(sidecar, "run_collection", return_value=(FAKE_METRICS, [], 0)),
            patch.object(sidecar, "http_post_signed_with_retry", return_value=(True, {}, 200)),
            patch.object(sidecar, "queue_flush"),
        ):
            runner.run_once()

        runner.pause()
        assert runner.status == "paused"
        runner.resume()
        # After resume, last_cycle_at is recent → "ok"
        assert runner.status == "ok"


class TestDaemonRunnerStartStop:
    """start() launches a background thread; stop() cleans up."""

    def test_start_transitions_to_ok_after_cycle(self):
        """start() → after first successful cycle → status 'ok'."""
        cycle_done = threading.Event()

        def on_status(status):
            if status == "ok":
                cycle_done.set()

        runner = sidecar.DaemonRunner(MINIMAL_CONFIG, on_status_change=on_status)

        with (
            patch.object(sidecar, "run_collection", return_value=(FAKE_METRICS, [], 0)),
            patch.object(sidecar, "http_post_signed_with_retry", return_value=(True, {}, 200)),
            patch.object(sidecar, "queue_flush"),
        ):
            runner.start()
            reached = cycle_done.wait(timeout=5)
            runner.stop()

        assert reached, "DaemonRunner did not complete a successful cycle within 5 seconds"
        assert runner.status == "ok"

    def test_stop_exits_cleanly(self):
        """stop() joins the thread within a reasonable timeout."""
        runner = _make_runner()

        with (
            patch.object(sidecar, "run_collection", return_value=([], [], 0)),
            patch.object(sidecar, "queue_flush"),
        ):
            runner.start()
            runner.stop()

        assert runner._thread is None


class TestDaemonRunnerOnStatusChange:
    """on_status_change callback fires on each status transition."""

    def test_callback_fires_on_run_once_success(self):
        statuses: list[str] = []
        runner = sidecar.DaemonRunner(MINIMAL_CONFIG, on_status_change=statuses.append)

        with (
            patch.object(sidecar, "run_collection", return_value=(FAKE_METRICS, [], 0)),
            patch.object(sidecar, "http_post_signed_with_retry", return_value=(True, {}, 200)),
            patch.object(sidecar, "queue_flush"),
        ):
            runner.run_once()

        assert "ok" in statuses

    def test_callback_fires_on_pause_and_resume(self):
        statuses: list[str] = []
        runner = sidecar.DaemonRunner(MINIMAL_CONFIG, on_status_change=statuses.append)

        runner.pause()
        runner.resume()

        assert "paused" in statuses
        assert statuses[-1] != "paused"  # resume changes status away from paused

    def test_callback_fires_on_error(self):
        statuses: list[str] = []
        runner = sidecar.DaemonRunner(MINIMAL_CONFIG, on_status_change=statuses.append)

        with patch.object(sidecar, "run_collection", side_effect=RuntimeError("fail")):
            runner.run_once()

        assert "err" in statuses

    def test_no_callback_does_not_crash(self):
        """DaemonRunner without callback must not raise on status changes."""
        runner = _make_runner()

        with (
            patch.object(sidecar, "run_collection", return_value=([], [], 0)),
            patch.object(sidecar, "queue_flush"),
        ):
            runner.run_once()  # must not raise

    def test_callback_receives_queued_warn_status(self):
        statuses: list[str] = []
        runner = sidecar.DaemonRunner(MINIMAL_CONFIG, on_status_change=statuses.append)

        with (
            patch.object(sidecar, "run_collection", return_value=(FAKE_METRICS, [], 0)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(False, "net error", 0),
            ),
            patch.object(sidecar, "queue_flush"),
            patch.object(sidecar, "queue_push"),
        ):
            runner.run_once()

        assert "warn" in statuses


# ---------------------------------------------------------------------------
# Account email helpers (JWT id_token extraction)
# ---------------------------------------------------------------------------


def _make_jwt(payload: dict) -> str:
    """Build a minimal unsigned JWT with the given payload dict."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.fakesig"


class TestDecodeIdTokenEmail:
    """_decode_id_token_email: extract email from JWT payload."""

    def test_valid_jwt_returns_email(self):
        token = _make_jwt({"email": "user@example.com", "sub": "12345"})
        result = sidecar._decode_id_token_email(token)
        assert result == "user@example.com"

    def test_jwt_missing_email_returns_none(self):
        token = _make_jwt({"sub": "12345", "name": "Test User"})
        result = sidecar._decode_id_token_email(token)
        assert result is None

    def test_invalid_string_returns_none(self):
        result = sidecar._decode_id_token_email("not-a-jwt")
        assert result is None

    def test_empty_string_returns_none(self):
        result = sidecar._decode_id_token_email("")
        assert result is None

    def test_email_without_at_sign_returns_none(self):
        """A payload with a non-email 'email' field must not pass through."""
        token = _make_jwt({"email": "notanemail"})
        result = sidecar._decode_id_token_email(token)
        assert result is None


class TestGeminiAccountEmail:
    """_gemini_account_email: reads ~/.gemini/oauth_creds.json."""

    def test_returns_default_when_file_missing(self, tmp_path):
        missing = str(tmp_path / "nonexistent.json")
        with patch.object(sidecar.os.path, "expanduser", return_value=missing):
            result = sidecar._gemini_account_email()
        assert result == "default"

    def test_returns_email_from_valid_creds(self, tmp_path):
        token = _make_jwt({"email": "gemini@example.com"})
        creds_file = tmp_path / "oauth_creds.json"
        creds_file.write_text(json.dumps({"id_token": token, "access_token": "tok"}))
        with patch.object(sidecar.os.path, "expanduser", return_value=str(creds_file)):
            result = sidecar._gemini_account_email()
        assert result == "gemini@example.com"

    def test_returns_default_when_id_token_missing(self, tmp_path):
        creds_file = tmp_path / "oauth_creds.json"
        creds_file.write_text(json.dumps({"access_token": "tok"}))
        with patch.object(sidecar.os.path, "expanduser", return_value=str(creds_file)):
            result = sidecar._gemini_account_email()
        assert result == "default"


class TestCodexAccountEmail:
    """_codex_account_email: reads ~/.codex/auth.json."""

    def test_returns_default_when_file_missing(self, tmp_path):
        missing = str(tmp_path / "nonexistent.json")
        with patch.object(sidecar.os.path, "expanduser", return_value=missing):
            result = sidecar._codex_account_email()
        assert result == "default"

    def test_returns_email_from_valid_auth(self, tmp_path):
        token = _make_jwt({"email": "codex@example.com"})
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {"id_token": token, "access_token": "tok"},
                }
            )
        )
        with patch.object(sidecar.os.path, "expanduser", return_value=str(auth_file)):
            result = sidecar._codex_account_email()
        assert result == "codex@example.com"

    def test_returns_default_when_tokens_missing(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(json.dumps({"auth_mode": "chatgpt"}))
        with patch.object(sidecar.os.path, "expanduser", return_value=str(auth_file)):
            result = sidecar._codex_account_email()
        assert result == "default"


class TestSelfUpdateCLIFlag:
    """`--self-update` runs one synchronous update and exits before the PID lock."""

    def test_self_update_flag_exits_without_pid_lock(self, monkeypatch):
        import scripts.sidecar_pkg.self_update as su_mod

        monkeypatch.setattr(sys, "argv", ["sidecar", "--self-update"])
        monkeypatch.setattr(
            sidecar, "load_config", lambda *a, **k: {"api_url": "x", "api_key": "y"}
        )
        monkeypatch.setattr(sidecar, "setup_logging", lambda *a, **k: None)

        pid_called = {"n": 0}
        monkeypatch.setattr(
            sidecar, "write_pid_file", lambda: pid_called.__setitem__("n", pid_called["n"] + 1)
        )

        su_calls = {"n": 0}

        def _fake_self_update(version, channel, *, restart=True):
            su_calls["n"] += 1
            assert restart is False  # one-shot manual run must not re-exec
            return False

        monkeypatch.setattr(su_mod, "self_update", _fake_self_update)

        with pytest.raises(SystemExit) as exc:
            sidecar.main()

        assert exc.value.code == 1  # self_update returned False
        assert su_calls["n"] == 1
        assert pid_called["n"] == 0  # never reached write_pid_file


class TestSingleInstanceLock:
    """The pid-file lock the tray's single-instance guard relies on.

    `sidecar_app/__main__.py` calls `write_pid_file()` before starting the tray;
    a second instance (login-launched copy, manual double launch, or tray-vs-CLI)
    must be refused while the holder is alive.
    """

    def test_second_acquire_fails_while_holder_alive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: tmp_path)
        try:
            assert sidecar.write_pid_file() is True  # first instance claims it
            # Second instance: current process is alive and holds the file → refused.
            assert sidecar.write_pid_file() is False
        finally:
            sidecar.remove_pid_file()

    def test_stale_pid_is_reclaimed(self, tmp_path, monkeypatch):
        """A pid file left by a dead process must not block a new instance."""
        monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: tmp_path)
        # Write a pid that is guaranteed dead (process 999999 doesn't exist here).
        (tmp_path / "sidecar.pid").write_text("999999")
        monkeypatch.setattr(sidecar, "_pid_is_alive", lambda pid: False)
        try:
            assert sidecar.write_pid_file() is True  # stale lock reclaimed
        finally:
            sidecar.remove_pid_file()


class TestAutoUpdatePrecedence:
    """Local `auto_update` config overrides the server's fleet-wide flag."""

    def _set(self, monkeypatch, local, server):
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_LOCAL", local)
        monkeypatch.setattr(sidecar, "_AUTO_UPDATE_SERVER", server)

    def test_local_true_overrides_server_false(self, monkeypatch):
        self._set(monkeypatch, True, False)
        assert sidecar._auto_update_enabled() is True

    def test_local_false_overrides_server_true(self, monkeypatch):
        self._set(monkeypatch, False, True)
        assert sidecar._auto_update_enabled() is False

    def test_unset_local_defers_to_server_true(self, monkeypatch):
        self._set(monkeypatch, None, True)
        assert sidecar._auto_update_enabled() is True

    def test_unset_local_defers_to_server_false(self, monkeypatch):
        self._set(monkeypatch, None, False)
        assert sidecar._auto_update_enabled() is False
