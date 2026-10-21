"""Cross-process lock tests use disposable paths, never the host cache."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.sidecar_pkg import claude_renewal_lock as locking


def test_threads_exclude_each_other_and_release_after_error(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with locking.claude_renewal_lock() as first:
        assert first
        with locking.claude_renewal_lock() as second:
            assert not second
    with pytest.raises(OSError, match="body failure"):
        with locking.claude_renewal_lock() as acquired:
            assert acquired
            raise OSError("body failure")
    with locking.claude_renewal_lock() as acquired:
        assert acquired


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS user lock")
def test_processes_share_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script = "from scripts.sidecar_pkg.claude_renewal_lock import claude_renewal_lock\nwith claude_renewal_lock() as acquired: print(acquired)"
    with locking.claude_renewal_lock() as acquired:
        assert acquired
        result = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS user lock")
def test_symlink_lock_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "Library/Caches/Runway/renewal"
    root.mkdir(parents=True, mode=0o700)
    target = tmp_path / "unrelated"
    target.write_text("untouched")
    (root / "claude.lock").symlink_to(target)
    with locking.claude_renewal_lock() as acquired:
        assert not acquired
    assert target.read_text() == "untouched"
