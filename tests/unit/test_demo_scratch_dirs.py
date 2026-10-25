"""The demo's scratch-dir sweep (scripts/demo/instance.py).

A demo that is killed cannot delete its own scratch dir, so a later demo
sweeps it. The sweep has to tell three things apart: a dir a live demo
owns, a dir a dead demo left behind, and a dir the operator asked to keep.
"""

import os
import time
from pathlib import Path

import pytest

from scripts.demo import instance
from scripts.demo.instance import KEEP_MARKER, LOCK_FILE, STALE_AFTER_SECONDS


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the sweep at an empty directory of its own."""
    monkeypatch.setattr(instance.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _demo_dir(root: Path, name: str) -> Path:
    demo = root / name
    demo.mkdir(parents=True, exist_ok=True)
    return demo


def _claim(demo: Path) -> int:
    """Take the dir's lock, the way a running demo does."""
    return instance._claim_demo_dir(demo)


class _exited_server:
    """A server that has already stopped, so `Demo.stop()` has nothing to do."""

    def poll(self) -> int:
        return 0


def test_a_claimed_dir_is_kept(tmp_root: Path) -> None:
    # A demo that is running holds the dir's lock, however long it has been
    # running: the lock, not the dir's age, says it is still there.
    demo = _demo_dir(tmp_root, "runway-demo-live")
    lock_fd = _claim(demo)
    try:
        stamp = time.time() - STALE_AFTER_SECONDS - 60
        os.utime(demo / LOCK_FILE, (stamp, stamp))
        instance.clean_stale_scratch_dirs()
        assert demo.exists()
    finally:
        os.close(lock_fd)


def test_a_dir_whose_owner_is_gone_is_removed(tmp_root: Path) -> None:
    # The demo was killed: its lock was released with it, and the dir is
    # left behind.
    demo = _demo_dir(tmp_root, "runway-demo-dead")
    lock_fd = _claim(demo)
    os.close(lock_fd)
    instance.clean_stale_scratch_dirs()
    assert not demo.exists()


def test_a_kept_dir_is_kept_even_though_nothing_holds_it(tmp_root: Path) -> None:
    # `--keep` is the operator's decision: the dir outlives the process, and
    # the operator says so itself.
    demo = _demo_dir(tmp_root, "runway-demo-kept")
    lock_fd = _claim(demo)
    os.close(lock_fd)
    (demo / KEEP_MARKER).write_text("kept")
    instance.clean_stale_scratch_dirs()
    assert demo.exists()


def test_a_dir_that_was_never_claimed_is_only_swept_once_it_is_old(tmp_root: Path) -> None:
    # The demo died between making the dir and locking it: there is no lock
    # to read, so the age decides. A young dir is a demo starting now.
    young = _demo_dir(tmp_root, "runway-demo-young")
    old = _demo_dir(tmp_root, "runway-demo-old")
    stamp = time.time() - STALE_AFTER_SECONDS - 60
    os.utime(old, (stamp, stamp))
    instance.clean_stale_scratch_dirs()
    assert young.exists()
    assert not old.exists()


def test_a_dir_that_is_not_ours_is_left_alone(tmp_root: Path) -> None:
    other = tmp_root / "something-else"
    other.mkdir()
    instance.clean_stale_scratch_dirs()
    assert other.exists()


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="root reads a file whatever its permission bits say",
)
def test_a_lock_that_cannot_be_read_keeps_the_dir(tmp_root: Path) -> None:
    # Not being able to read the lock is not evidence that nobody owns the
    # dir -- it is evidence that we cannot tell. The dir is left alone.
    demo = _demo_dir(tmp_root, "runway-demo-unreadable")
    lock_fd = _claim(demo)
    os.close(lock_fd)
    os.chmod(demo / LOCK_FILE, 0o000)
    try:
        instance.clean_stale_scratch_dirs()
        assert demo.exists()
    finally:
        os.chmod(demo / LOCK_FILE, 0o600)


def test_a_kept_demo_leaves_a_marked_dir_and_releases_its_lock(tmp_path: Path) -> None:
    # The marker has to be in place before the lock is released: a dir with
    # neither is what the sweep takes.
    demo_dir = tmp_path / "runway-demo-kept"
    demo_dir.mkdir()
    lock_fd = _claim(demo_dir)
    demo = instance.Demo(
        demo_dir=demo_dir,
        url="",
        server=_exited_server(),
        keep=True,
        lock_fd=lock_fd,
    )
    demo.stop()
    assert (demo_dir / KEEP_MARKER).exists()
    assert demo_dir.exists()
    assert not instance._dir_is_claimed(demo_dir)
