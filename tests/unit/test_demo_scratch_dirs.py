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
from scripts.demo.instance import KEEP_MARKER, STALE_AFTER_SECONDS


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the sweep at an empty directory of its own."""
    monkeypatch.setattr(instance.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _demo_dir(root: Path, name: str, *, pid: int | None = None, age: float = 0.0) -> Path:
    demo = root / name
    demo.mkdir(parents=True, exist_ok=True)
    if pid is not None:
        pid_file = demo / "pid"
        pid_file.write_text(str(pid))
        stamp = time.time() - age
        os.utime(pid_file, (stamp, stamp))
    return demo


def test_a_dead_demos_dir_is_removed(tmp_root: Path) -> None:
    demo = _demo_dir(tmp_root, "runway-demo-dead", pid=2**22)
    instance.clean_stale_scratch_dirs()
    assert not demo.exists()


def test_a_live_demos_dir_is_kept(tmp_root: Path) -> None:
    demo = _demo_dir(tmp_root, "runway-demo-live", pid=os.getpid())
    instance.clean_stale_scratch_dirs()
    assert demo.exists()


def test_a_kept_dir_is_kept_even_though_its_pid_is_gone(tmp_root: Path) -> None:
    # `--keep` is the operator's decision: the dir outlives the process, so
    # the pid check alone would sweep it away.
    demo = _demo_dir(tmp_root, "runway-demo-kept", pid=2**22)
    (demo / KEEP_MARKER).write_text("kept")
    instance.clean_stale_scratch_dirs()
    assert demo.exists()


def test_an_old_dir_goes_even_when_its_pid_answers(tmp_root: Path) -> None:
    # A pid is reused. The pid of a demo that died days ago can belong to an
    # unrelated process today, and that must not keep the dir for ever.
    demo = _demo_dir(
        tmp_root, "runway-demo-recycled", pid=os.getpid(), age=STALE_AFTER_SECONDS + 60
    )
    instance.clean_stale_scratch_dirs()
    assert not demo.exists()


def test_a_dir_without_a_pid_is_only_swept_once_it_is_old(tmp_root: Path) -> None:
    # The demo died between making the dir and writing its pid: there is no
    # pid to read, so the age decides. A young dir is a demo starting now.
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
