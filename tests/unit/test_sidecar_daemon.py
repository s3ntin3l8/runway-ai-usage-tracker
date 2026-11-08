"""Unit tests for sidecar offline queue, Windows helpers, and DaemonRunner."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.unit._sidecar_helpers import (
    _REPO_ROOT,
    isolate_host_credentials,  # noqa: F401 — fixture
)

if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import sidecar


def _collection_result(
    metrics: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    error_count: int = 0,
) -> sidecar.CollectionResult:
    return sidecar.CollectionResult(metrics or [], events or [], error_count, [])


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
        entry = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert entry["payload"] == {"provider": "test", "metrics": []}

    def test_queue_push_retains_existing_entries_when_limit_is_reached(self, tmp_path):
        existing = tmp_path / "2026-01-01.jsonl"
        existing.write_bytes(b"x" * (10 * 1024 * 1024))

        with patch.object(sidecar, "get_queue_dir", return_value=tmp_path):
            with patch.object(sidecar, "ensure_dirs"):
                assert sidecar.queue_push({"events": [{"event_id": "retry-me"}]}) is False

        assert existing.stat().st_size == 10 * 1024 * 1024
        assert len(list(tmp_path.glob("*.jsonl"))) == 1

    def test_queue_push_honours_configured_max_size_mb(self, tmp_path):
        """B2: queue_max_size_mb used to be accepted by queue_rotate but never
        actually threaded from config into queue_push's own hardcoded 10 MB
        cap. A tiny configured limit must reject a push well under 10 MB."""
        with patch.object(sidecar, "get_queue_dir", return_value=tmp_path):
            with patch.object(sidecar, "ensure_dirs"):
                # 1 KB payload against a 1-byte limit — must reject.
                accepted = sidecar.queue_push({"events": ["x" * 1024]}, max_size_mb=1e-6)

        assert accepted is False
        # Nothing written at all — the very first push already exceeds the cap.
        assert list(tmp_path.glob("*.jsonl")) == []

    def test_queue_push_invalid_max_size_mb_falls_back_to_ten(self, tmp_path):
        """A non-positive or non-numeric max_size_mb must not silently
        disable the cap (or crash) — it falls back to the 10 MB default."""
        with patch.object(sidecar, "get_queue_dir", return_value=tmp_path):
            with patch.object(sidecar, "ensure_dirs"):
                assert sidecar.queue_push({"a": 1}, max_size_mb=0) is True
                assert sidecar.queue_push({"a": 1}, max_size_mb=-5) is True
                assert sidecar.queue_push({"a": 1}, max_size_mb="bogus") is True

    @pytest.mark.parametrize("bad_limit", [float("nan"), float("inf"), True, 10**1000])
    def test_queue_push_invalid_numeric_limit_keeps_default_cap(self, tmp_path, bad_limit):
        existing = tmp_path / "2026-01-01.jsonl"
        existing.write_bytes(b"x" * (10 * 1024 * 1024))

        with patch.object(sidecar, "get_queue_dir", return_value=tmp_path):
            with patch.object(sidecar, "ensure_dirs"):
                assert sidecar.queue_push({"events": ["new"]}, max_size_mb=bad_limit) is False

        assert existing.stat().st_size == 10 * 1024 * 1024


class TestQueueFlush:
    def test_event_ingest_error_is_retained_and_retried(self, tmp_path, monkeypatch):
        queue_file = tmp_path / "2026-01-01.jsonl"
        failed_payload = {"events": [{"event_id": "retry-me"}]}
        acknowledged_payload = {"events": [{"event_id": "stored"}]}
        queue_file.write_text(
            "".join(
                json.dumps({"ts": idx, "payload": payload}) + "\n"
                for idx, payload in enumerate([failed_payload, acknowledged_payload], start=1)
            )
        )
        monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path)
        replies = iter(
            [
                (True, {"events_error": True}, 200),
                (True, {"events_error": False}, 200),
                (True, {"events_error": False}, 200),
            ]
        )
        monkeypatch.setattr(
            sidecar,
            "http_post_signed_with_retry",
            lambda *args, **kwargs: next(replies),
        )

        assert sidecar.queue_flush("http://localhost", "synthetic-key") == 1
        retained = [
            json.loads(line)["payload"]
            for line in queue_file.read_text(encoding="utf-8").splitlines()
        ]
        assert retained == [failed_payload]

        assert sidecar.queue_flush("http://localhost", "synthetic-key") == 1
        assert not queue_file.exists()

    def test_stop_preserves_unprocessed_entries(self, tmp_path, monkeypatch):
        queue_file = tmp_path / "2026-01-01.jsonl"
        payloads = [{"metrics": [{"id": "sent"}]}, {"metrics": [{"id": "pending"}]}]
        queue_file.write_text(
            "".join(
                json.dumps({"ts": idx, "payload": payload}) + "\n"
                for idx, payload in enumerate(payloads)
            )
        )
        monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path)
        stop_event = threading.Event()

        def acknowledge_then_stop(*args, **kwargs):
            stop_event.set()
            return True, {}, 200

        monkeypatch.setattr(sidecar, "http_post_signed_with_retry", acknowledge_then_stop)

        assert sidecar.queue_flush("http://localhost", "synthetic-key", stop_event) == 1
        retained = [
            json.loads(line)["payload"]
            for line in queue_file.read_text(encoding="utf-8").splitlines()
        ]
        assert retained == [payloads[1]]


@pytest.mark.skipif(os.name == "nt", reason="Unix queue permission semantics")
class TestSecureQueueStorage:
    def _use_dirs(self, monkeypatch, root: Path) -> tuple[Path, Path]:
        sidecar_dir = root / "sidecar"
        queue_dir = sidecar_dir / "queue"
        monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: sidecar_dir)
        monkeypatch.setattr(sidecar, "get_queue_dir", lambda: queue_dir)
        return sidecar_dir, queue_dir

    def test_push_secures_directories_and_file_under_permissive_umask(self, tmp_path, monkeypatch):
        sidecar_dir, queue_dir = self._use_dirs(monkeypatch, tmp_path)
        old_umask = os.umask(0o022)
        try:
            sidecar.queue_push({"credential": "synthetic-secret"})
        finally:
            os.umask(old_umask)

        queue_file = next(queue_dir.glob("*.jsonl"))
        assert sidecar_dir.stat().st_mode & 0o777 == 0o700
        assert queue_dir.stat().st_mode & 0o777 == 0o700
        assert queue_file.stat().st_mode & 0o777 == 0o600
        assert "synthetic-secret" in queue_file.read_text(encoding="utf-8")

    def test_push_repairs_permissive_directory_and_file_preserving_content(
        self, tmp_path, monkeypatch
    ):
        sidecar_dir, queue_dir = self._use_dirs(monkeypatch, tmp_path)
        queue_dir.mkdir(parents=True, mode=0o777)
        queue_file = queue_dir / "2026-01-01.jsonl"
        original = '{"ts":1,"payload":{"credential":"keep-me"}}\n'
        queue_file.write_text(original)
        sidecar_dir.chmod(0o777)
        queue_dir.chmod(0o777)
        queue_file.chmod(0o644)

        sidecar.queue_push({"provider": "test"})

        assert sidecar_dir.stat().st_mode & 0o777 == 0o700
        assert queue_dir.stat().st_mode & 0o777 == 0o700
        assert queue_file.stat().st_mode & 0o777 == 0o600
        assert queue_file.read_text(encoding="utf-8").startswith(original)

    def test_push_rejects_symlinked_queue_file(self, tmp_path, monkeypatch):
        _sidecar_dir, queue_dir = self._use_dirs(monkeypatch, tmp_path)
        queue_dir.mkdir(parents=True)
        target = tmp_path / "outside.jsonl"
        target.write_text("private target\n")
        (
            queue_dir / f"{sidecar.datetime.datetime.now(sidecar.datetime.UTC):%Y-%m-%d}.jsonl"
        ).symlink_to(target)

        with pytest.raises(OSError):
            sidecar.queue_push({"credential": "secret"})
        assert target.read_text(encoding="utf-8") == "private target\n"

    def test_rejects_symlinked_sidecar_directory(self, tmp_path, monkeypatch):
        sidecar_dir = tmp_path / "sidecar"
        target = tmp_path / "outside"
        target.mkdir()
        sidecar_dir.symlink_to(target, target_is_directory=True)
        monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: sidecar_dir)
        monkeypatch.setattr(sidecar, "get_queue_dir", lambda: sidecar_dir / "queue")

        with pytest.raises(OSError):
            sidecar.queue_push({"credential": "secret"})
        assert list(target.iterdir()) == []

    def test_rejects_symlinked_queue_directory(self, tmp_path, monkeypatch):
        sidecar_dir, queue_dir = self._use_dirs(monkeypatch, tmp_path)
        sidecar_dir.mkdir()
        target = tmp_path / "outside-queue"
        target.mkdir()
        queue_dir.symlink_to(target, target_is_directory=True)

        with pytest.raises(OSError):
            sidecar.queue_push({"credential": "secret"})
        assert list(target.iterdir()) == []

    def test_flush_repairs_failed_entry_and_preserves_jsonl(self, tmp_path, monkeypatch):
        _sidecar_dir, queue_dir = self._use_dirs(monkeypatch, tmp_path)
        queue_dir.mkdir(parents=True)
        queue_file = queue_dir / "2026-01-01.jsonl"
        line = '{"ts":1,"payload":{"credential":"preserve"}}\n'
        queue_file.write_text(line)
        queue_file.chmod(0o644)
        monkeypatch.setattr(
            sidecar,
            "http_post_signed_with_retry",
            lambda *args, **kwargs: (False, 0, "offline"),
        )

        assert sidecar.queue_flush("http://localhost", "synthetic-key") == 0
        assert queue_file.stat().st_mode & 0o777 == 0o600
        assert queue_file.read_text(encoding="utf-8") == line

    def test_flush_and_rotation_leave_symlink_target_untouched(self, tmp_path, monkeypatch):
        _sidecar_dir, queue_dir = self._use_dirs(monkeypatch, tmp_path)
        queue_dir.mkdir(parents=True)
        target = tmp_path / "outside.jsonl"
        target.write_text('{"payload":{"credential":"do-not-read"}}\n')
        link = queue_dir / "2026-01-01.jsonl"
        link.symlink_to(target)

        assert sidecar.queue_flush("http://localhost", "synthetic-key") == 0
        sidecar.queue_rotate(max_size_mb=0)
        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == '{"payload":{"credential":"do-not-read"}}\n'


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

    def test_credential_manager_lookup_spawns_no_process(self, monkeypatch):
        """The lookup must not shell out.

        It used to run PowerShell's ``Get-StoredCredential``, which needs the
        CredentialManager PowerShell module — absent from a stock Windows
        install, so the lookup silently found nothing and the packaged
        windowed sidecar flashed a console. The native reader (advapi32) is
        both dependency-free and process-free, so no console can appear.
        """
        monkeypatch.setattr(sidecar.platform, "system", lambda: "Windows")
        target = "runway-test-no-console"
        sidecar._windows_cred_cache.pop(target, None)

        with (
            patch.object(sidecar.subprocess, "run") as run,
            patch(
                "scripts.sidecar_pkg.wincred.read_credential_text",
                return_value="secret",
            ),
        ):
            assert sidecar.get_windows_credential(target) == "secret"

        run.assert_not_called()
        sidecar._windows_cred_cache.pop(target, None)


class TestSidecarSubprocessWindows:
    def test_generic_exec_rule_hides_windows_console(self, monkeypatch):
        no_window = 0x08000000
        monkeypatch.setattr(sidecar.platform, "system", lambda: "Windows")
        monkeypatch.setattr(sidecar.subprocess, "CREATE_NO_WINDOW", no_window, raising=False)
        result = MagicMock(returncode=0, stdout="value\n")

        with patch.object(sidecar.subprocess, "run", return_value=result) as run:
            sidecar.GenericCollector.collect_provider(
                "test-provider",
                {
                    "rules": [
                        {
                            "type": "exec",
                            "command": ["git", "config", "--global", "user.email"],
                            "mapping": {"value": "api_key"},
                        }
                    ]
                },
            )

        assert run.call_args.args[0] == ["git", "config", "--global", "user.email"]
        assert run.call_args.kwargs["creationflags"] == no_window

    @pytest.mark.parametrize("system", ["Linux", "Darwin"])
    def test_non_windows_subprocess_flags_remain_zero(self, monkeypatch, system):
        monkeypatch.setattr(sidecar.platform, "system", lambda: system)

        assert sidecar._subprocess_creationflags() == 0


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
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
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

    def test_batch_instructions_survive_later_empty_response(self, monkeypatch):
        runner = _make_runner()
        events = [{"ts": "2026-01-01T00:00:00Z"} for _ in range(1001)]
        responses = [
            (True, {"trigger": True, "poll_providers": ["anthropic"], "update_now": True}, 200),
            (True, {}, 200),
        ]
        posts = MagicMock(side_effect=responses)
        refresh = MagicMock()
        monkeypatch.setattr(runner._trigger_event, "set", refresh)
        with (
            patch.object(sidecar, "run_collection", return_value=_collection_result(events=events)),
            patch.object(sidecar, "http_post_signed_with_retry", posts),
            patch.object(sidecar, "queue_flush"),
            patch("scripts.sidecar_pkg.self_update.self_update") as self_update,
        ):
            assert runner.run_once() is True

        assert [len(call.args[1]["events"]) for call in posts.call_args_list] == [1000, 1]
        refresh.assert_called_once()
        self_update.assert_called_once()
        assert runner._next_poll_providers is None

    def test_batch_poll_lists_are_merged_in_first_seen_order(self):
        runner = _make_runner()
        events = [{"event_id": str(i)} for i in range(1001)]
        with (
            patch.object(sidecar, "run_collection", return_value=_collection_result(events=events)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                side_effect=[
                    (True, {"poll_providers": ["anthropic", "openai"]}, 200),
                    (True, {"poll_providers": ["openai", "gemini"]}, 200),
                ],
            ),
            patch.object(sidecar, "queue_flush"),
        ):
            assert runner.run_once() is True

        assert runner._next_poll_providers == ["anthropic", "openai", "gemini"]

    def test_successful_batch_instructions_apply_when_later_batch_fails(self):
        runner = _make_runner()
        events = [{"event_id": str(i)} for i in range(1001)]
        with (
            patch.object(sidecar, "run_collection", return_value=_collection_result(events=events)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                side_effect=[
                    (True, {"poll_providers": ["anthropic"], "trigger": True}, 200),
                    (False, "server unavailable", 503),
                ],
            ),
            patch.object(sidecar, "queue_flush"),
            patch.object(sidecar, "queue_push"),
            patch.object(runner._trigger_event, "set") as refresh,
        ):
            assert runner.run_once() is False

        assert runner.status == "err"
        assert runner.last_http_code == 503
        assert runner.last_error == "server unavailable"
        refresh.assert_called_once()
        assert runner._next_poll_providers is None

    def test_events_error_response_keeps_instructions_and_watermark(self, monkeypatch):
        runner = _make_runner()
        event = {
            "ts": "2026-01-01T00:00:00Z",
            "provider_id": "anthropic",
            "account_id": "acct",
        }
        watermark = MagicMock()
        monkeypatch.setattr(
            "scripts.sidecar_pkg.event_watermark.EventWatermark",
            lambda _path: watermark,
        )
        with (
            patch.object(
                sidecar, "run_collection", return_value=_collection_result(events=[event])
            ),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(True, {"events_error": True, "poll_providers": ["anthropic"]}, 200),
            ),
            patch.object(sidecar, "queue_flush"),
        ):
            assert runner.run_once() is True

        assert runner._next_poll_providers == ["anthropic"]
        watermark.advance.assert_not_called()

    def test_run_once_empty_metrics_still_ok(self):
        """No metrics collected → still 'ok', heartbeat HTTP call made."""
        runner = _make_runner()
        with (
            patch.object(sidecar, "run_collection", return_value=_collection_result()),
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
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
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


class TestFrozenRuntimeMissing:
    """_frozen_runtime_missing(): the precise local-corruption signal.

    Distinguishes "my own unpacked _MEIPASS dir got swept out from under me"
    (fatal — retrying can never fix it) from ordinary network failures.
    """

    def test_not_frozen_never_reports_missing(self, monkeypatch):
        monkeypatch.delattr(sidecar.sys, "_MEIPASS", raising=False)
        assert sidecar._frozen_runtime_missing() is False

    def test_frozen_with_intact_dir_is_fine(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sidecar.sys, "_MEIPASS", str(tmp_path), raising=False)
        assert sidecar._frozen_runtime_missing() is False

    def test_frozen_with_reaped_dir_is_missing(self, monkeypatch, tmp_path):
        gone = tmp_path / "_MEIreaped"  # never created
        monkeypatch.setattr(sidecar.sys, "_MEIPASS", str(gone), raising=False)
        assert sidecar._frozen_runtime_missing() is True


class TestDaemonRunnerRuntimeCorruptionExit:
    """run_once() must hard-exit when the frozen runtime itself is gone —
    Restart=always can only heal a process that actually exits. Regression
    for the incident where a reaped /tmp/_MEIxxxx sent the daemon into an
    infinite `HTTP 0: [Errno 2]` retry loop with no supervisor intervention.
    """

    def test_code_zero_plus_missing_runtime_exits_process(self):
        runner = _make_runner()
        with (
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(False, "[Errno 2] No such file or directory", 0),
            ),
            patch.object(sidecar, "_frozen_runtime_missing", return_value=True),
            patch.object(sidecar, "queue_flush"),
            patch.object(sidecar, "queue_push"),
            patch.object(sidecar.os, "_exit") as mock_exit,
        ):
            runner.run_once()

        mock_exit.assert_called_once_with(70)

    def test_code_zero_with_intact_runtime_does_not_exit(self):
        """A code-0 failure alone (server down, DNS failure, etc.) must NOT
        exit — only a code-0 failure *combined with* a reaped runtime is
        treated as fatal, so a plain network/server outage can't crash-loop
        the daemon."""
        runner = _make_runner()
        with (
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(False, "Connection refused", 0),
            ),
            patch.object(sidecar, "_frozen_runtime_missing", return_value=False),
            patch.object(sidecar, "queue_flush"),
            patch.object(sidecar, "queue_push"),
            patch.object(sidecar.os, "_exit") as mock_exit,
        ):
            result = runner.run_once()

        mock_exit.assert_not_called()
        assert result is False

    def test_http_error_with_missing_runtime_does_not_exit(self):
        """A real HTTP response (non-zero code) means the network path works —
        even if the runtime dir check would report missing, that combination
        shouldn't occur, but code must gate strictly on code==0 so a genuine
        4xx/5xx from the server never triggers a process exit."""
        runner = _make_runner()
        with (
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(False, "Internal Server Error", 500),
            ),
            patch.object(sidecar, "_frozen_runtime_missing", return_value=True),
            patch.object(sidecar, "queue_flush"),
            patch.object(sidecar, "queue_push"),
            patch.object(sidecar.os, "_exit") as mock_exit,
        ):
            runner.run_once()

        mock_exit.assert_not_called()


class TestDaemonRunnerQueuedStatus:
    """When HTTP fails, payload is queued → status 'warn'."""

    def test_run_once_failure_queues_payload(self):
        runner = _make_runner()
        queued = []
        with (
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
            patch.object(
                sidecar,
                "http_post_signed_with_retry",
                return_value=(False, "timeout", 0),
            ),
            patch.object(sidecar, "queue_flush"),
            patch.object(
                sidecar,
                "queue_push",
                side_effect=lambda p, max_size_mb=None: (queued.append(p), True)[1],
            ),
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
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
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
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
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
            patch.object(sidecar, "run_collection", return_value=_collection_result()),
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
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
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
            patch.object(sidecar, "run_collection", return_value=_collection_result()),
            patch.object(sidecar, "queue_flush"),
        ):
            runner.run_once()  # must not raise

    def test_callback_receives_queued_warn_status(self):
        statuses: list[str] = []
        runner = sidecar.DaemonRunner(MINIMAL_CONFIG, on_status_change=statuses.append)

        with (
            patch.object(sidecar, "run_collection", return_value=_collection_result(FAKE_METRICS)),
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
            # First instance (a different live PID) claims it.
            with monkeypatch.context() as m:
                m.setattr(sidecar.os, "getpid", os.getppid)
                assert sidecar.write_pid_file() is True
            # Second instance: the holder is alive and is not us → refused.
            assert sidecar.write_pid_file() is False
        finally:
            sidecar.remove_pid_file()

    def test_own_pid_in_file_is_taken_over(self, tmp_path, monkeypatch):
        """A self-update re-exec keeps the PID, so a file holding it is ours (#546)."""
        monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: tmp_path)
        pid_file = tmp_path / "sidecar.pid"
        pid_file.write_text(str(os.getpid()))
        try:
            assert sidecar.write_pid_file() is True
            assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            sidecar.remove_pid_file()

    def test_other_live_pid_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: tmp_path)
        pid_file = tmp_path / "sidecar.pid"
        other = 999999  # never our PID; liveness is patched below
        pid_file.write_text(str(other))
        monkeypatch.setattr(sidecar, "_pid_is_alive", lambda pid: True)
        assert sidecar.write_pid_file() is False
        assert pid_file.read_text(encoding="utf-8").strip() == str(other)

    @pytest.mark.skipif(os.name != "nt", reason="Windows PIDs are multiples of 4")
    def test_a_stale_pid_is_reclaimed_without_a_patched_probe(self, tmp_path, monkeypatch):
        """The real liveness probe has to see a dead PID as dead.

        A Windows PID is always a multiple of 4, so this one can never exist.
        """
        monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: tmp_path)
        pid_file = tmp_path / "sidecar.pid"
        pid_file.write_text("999999")
        try:
            assert sidecar.write_pid_file() is True  # stale lock reclaimed
            assert pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
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
