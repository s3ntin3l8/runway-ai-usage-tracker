"""Unit tests for sidecar critical bug fixes."""

import json
import sys
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
        assert isinstance(
            sidecar._windows_cred_cache, dict
        ), f"_windows_cred_cache is {type(sidecar._windows_cred_cache)}, expected dict"

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
        assert (
            len(matches) == 0
        ), f"Found {len(matches)} assignment(s) of _windows_cred_cache = None: {matches}"
