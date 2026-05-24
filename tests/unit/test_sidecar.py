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
    "interval_seconds": 60,
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


def _load_sidecar_module():
    """Load scripts/sidecar.py as a module for testing."""
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location(
        "sidecar_mod",
        os.path.join(os.path.dirname(__file__), "../../scripts/sidecar.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ag_find_ports_unix_uses_a_flag_for_and_semantics():
    """lsof joins -i and -p with OR by default; -a flips to AND. Without -a,
    the regex would scrape every TCP listening socket on the box (e.g. the
    Runway server itself) and the LSP probe would POST to all of them.
    """
    from unittest.mock import patch

    mod = _load_sidecar_module()

    captured_args: list[list[str]] = []

    class _FakeResult:
        stdout = ""

    def _fake_run(args, **kwargs):
        captured_args.append(list(args))
        return _FakeResult()

    with patch("subprocess.run", side_effect=_fake_run):
        mod._ag_find_ports_unix(12345)

    assert captured_args, "_ag_find_ports_unix did not invoke subprocess.run"
    args = captured_args[0]
    assert args[0] == "lsof"
    assert "-a" in args, f"-a flag missing from lsof args: {args}"


def test_ag_parse_lsp_response_model_card():
    """Sidecar _ag_parse_lsp_response produces correct fields for model quota card."""
    mod = _load_sidecar_module()

    data = {
        "userStatus": {
            "email": "user@test.com",
            "planStatus": {"planInfo": {"planName": "Pro"}},
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {
                        "label": "claude-sonnet-4-5",
                        "modelOrAlias": "claude-sonnet-4-5-20251001",
                        "quotaInfo": {"remainingFraction": 0.6, "resetTime": 9999999999},
                    }
                ]
            },
            "userTier": {"availableCredits": []},
        }
    }

    cards = mod._ag_parse_lsp_response(data, "🛸")
    assert len(cards) == 1
    card = cards[0]
    assert card["service_name"] == "claude-sonnet-4-5"
    assert "AG:" not in card["service_name"]
    assert card["provider_id"] == "antigravity"
    assert card["account_label"] == "user@test.com"
    assert card["model_id"] == "claude-sonnet-4-5-20251001"
    assert card["used_value"] == pytest.approx(40.0, abs=0.1)
    assert card["limit_value"] == 100.0
    assert card["pct_used"] == pytest.approx(40.0, abs=0.1)
    assert card["unit_type"] == "percent"
    assert card["window_type"] == "weekly"  # resetTime: 9999999999 (year 2286) → cooldown → weekly
    assert card["reset_at"] is not None


def test_ag_parse_lsp_response_survives_bad_reset_time():
    """_ag_parse_lsp_response returns cards even when resetTime is not numeric."""
    mod = _load_sidecar_module()

    data = {
        "userStatus": {
            "email": "user@test.com",
            "planStatus": {"planInfo": {"planName": "Pro"}},
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {
                        "label": "claude-sonnet-4-5",
                        "modelOrAlias": "claude-sonnet-4-5-20251001",
                        "quotaInfo": {"remainingFraction": 0.6, "resetTime": "not-a-number"},
                    }
                ]
            },
            "userTier": {"availableCredits": []},
        }
    }

    cards = mod._ag_parse_lsp_response(data, "🛸")
    assert len(cards) == 1, "Should return 1 card even with bad resetTime"
    assert cards[0]["reset_at"] is None
    assert cards[0]["provider_id"] == "antigravity"


def test_ag_parse_lsp_response_parses_iso_reset_time():
    """LSP returns resetTime as ISO 8601 strings (not just Unix timestamps)."""
    mod = _load_sidecar_module()

    data = {
        "userStatus": {
            "email": "user@test.com",
            "planStatus": {"planInfo": {"planName": "Pro"}},
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {
                        "label": "iso-model",
                        "modelOrAlias": "iso-model",
                        "quotaInfo": {
                            "remainingFraction": 0.5,
                            "resetTime": "2026-05-09T13:03:17Z",
                        },
                    },
                    {
                        "label": "unix-model",
                        "modelOrAlias": "unix-model",
                        "quotaInfo": {"remainingFraction": 0.5, "resetTime": 9999999999},
                    },
                ]
            },
            "userTier": {"availableCredits": []},
        }
    }

    cards = mod._ag_parse_lsp_response(data, "🛸")
    assert len(cards) == 2
    assert cards[0]["reset_at"] == "2026-05-09T13:03:17+00:00"
    assert cards[1]["reset_at"] is not None  # Unix timestamp still parses


def test_ag_parse_lsp_response_falls_back_to_label_for_placeholder_model():
    """When modelOrAlias is an internal MODEL_* id (string OR dict), fall back to label."""
    mod = _load_sidecar_module()

    data = {
        "userStatus": {
            "email": "user@test.com",
            "planStatus": {"planInfo": {"planName": "Pro"}},
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {
                        "label": "Gemini 3.1 Pro (Low)",
                        "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M36"},
                        "quotaInfo": {"remainingFraction": 1.0, "resetTime": 9999999999},
                    },
                    {
                        "label": "GPT-OSS 120B (Medium)",
                        "modelOrAlias": "MODEL_OPENAI_GPT_OSS_120B_MEDIUM",
                        "quotaInfo": {"remainingFraction": 1.0, "resetTime": 9999999999},
                    },
                    {
                        "label": "Real Model",
                        "modelOrAlias": "claude-sonnet-4-5",
                        "quotaInfo": {"remainingFraction": 1.0, "resetTime": 9999999999},
                    },
                ]
            },
            "userTier": {"availableCredits": []},
        }
    }

    cards = mod._ag_parse_lsp_response(data, "🛸")
    assert len(cards) == 3
    assert cards[0]["model_id"] == "Gemini 3.1 Pro (Low)"  # dict placeholder → label
    assert cards[1]["model_id"] == "GPT-OSS 120B (Medium)"  # string placeholder → label
    assert cards[2]["model_id"] == "claude-sonnet-4-5"  # real id passed through


def test_ag_parse_lsp_response_treats_null_remaining_with_future_reset_as_exhausted():
    """Antigravity drops remainingFraction on exhaustion; future resetTime means 100% used."""
    mod = _load_sidecar_module()
    future_ts = int(time.time()) + 3600  # 1h ahead

    data = {
        "userStatus": {
            "email": "user@test.com",
            "planStatus": {"planInfo": {"planName": "Pro"}},
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {
                        "label": "Gemini 3.1 Pro (High)",
                        "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M16"},
                        "quotaInfo": {"resetTime": future_ts},  # no remainingFraction
                    },
                ]
            },
            "userTier": {"availableCredits": []},
        }
    }

    cards = mod._ag_parse_lsp_response(data, "🛸")
    assert len(cards) == 1
    card = cards[0]
    assert card["model_id"] == "Gemini 3.1 Pro (High)"
    assert card["pct_used"] == 100.0
    assert card["used_value"] == 100.0
    assert card["limit_value"] == 100.0
    assert card["unit_type"] == "percent"
    assert card["remaining"] == "0.0%"


def test_ag_parse_lsp_response_skips_null_remaining_with_no_reset():
    """No remainingFraction and no future reset = genuinely uninformative, skip."""
    mod = _load_sidecar_module()
    past_ts = int(time.time()) - 3600  # 1h ago

    data = {
        "userStatus": {
            "email": "user@test.com",
            "planStatus": {"planInfo": {"planName": "Pro"}},
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {"label": "No reset", "quotaInfo": {}},
                    {"label": "Past reset", "quotaInfo": {"resetTime": past_ts}},
                ]
            },
            "userTier": {"availableCredits": []},
        }
    }

    cards = mod._ag_parse_lsp_response(data, "🛸")
    assert cards == []


def test_ag_parse_lsp_response_marks_local_sidecar_origin():
    """LSP cards must carry data_source='local' and input_source='sidecar'."""
    mod = _load_sidecar_module()

    data = {
        "userStatus": {
            "email": "user@test.com",
            "planStatus": {"planInfo": {"planName": "Pro"}},
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {
                        "label": "any-model",
                        "modelOrAlias": "any-model",
                        "quotaInfo": {"remainingFraction": 0.5, "resetTime": 9999999999},
                    }
                ]
            },
            "userTier": {
                "availableCredits": [{"creditType": "ANTHROPIC_CREDIT", "creditAmount": "100"}]
            },
        }
    }

    cards = mod._ag_parse_lsp_response(data, "🛸")
    assert len(cards) == 2
    for card in cards:
        assert card["data_source"] == "local"
        assert card["input_source"] == "sidecar"


def test_ag_parse_lsp_response_credit_card():
    """Sidecar _ag_parse_lsp_response produces correct fields for credit card."""
    mod = _load_sidecar_module()

    data = {
        "userStatus": {
            "email": "user@test.com",
            "planStatus": {"planInfo": {"planName": "Pro"}},
            "cascadeModelConfigData": {"clientModelConfigs": []},
            "userTier": {
                "availableCredits": [{"creditType": "ANTHROPIC_CREDIT", "creditAmount": "500"}]
            },
        }
    }

    cards = mod._ag_parse_lsp_response(data, "🛸")
    assert len(cards) == 1
    card = cards[0]
    assert card["service_name"] == "Anthropic Credits"
    assert card["provider_id"] == "antigravity"
    assert card["account_label"] == "user@test.com"
    assert card["used_value"] is None
    assert card["limit_value"] is None
    assert card["unit_type"] == "credits"
    assert card["remaining"] == "500"
    assert card["model_id"] is None
    assert card["reset_at"] is None
