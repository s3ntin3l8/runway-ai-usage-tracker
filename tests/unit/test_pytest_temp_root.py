"""The pytest temp root must be usable, or every test errors out before it runs.

pytest keeps its per-run directories in ``%TEMP%\\pytest-of-<user>``. A
directory left behind by another user's (or an elevated) process can carry an
ACL that leaves this user unable to use it, and pytest then fails every single
test with ``PermissionError`` before one of them runs.
"""

import os
import stat
from pathlib import Path

import pytest

from tests.conftest import (
    _ensure_writable_pytest_root,
    _pytest_root_dir,
    _pytest_root_usable,
)


@pytest.fixture
def temproot_env(monkeypatch):
    """Own PYTEST_DEBUG_TEMPROOT so the helper's write is undone at teardown.

    ``monkeypatch.delenv`` only guards a value that already exists — what the
    helper sets during the test would otherwise stay set for the rest of the
    session.
    """
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    yield
    os.environ.pop("PYTEST_DEBUG_TEMPROOT", None)


@pytest.fixture
def deny_writes_to(monkeypatch):
    """Make writes into the given directory fail, as a foreign ACL would."""
    denied: list[Path] = []
    real_mkdir = Path.mkdir

    def mkdir(self, *args, **kwargs):
        if self.parent in denied:
            raise PermissionError(13, "Access is denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    return denied


def test_an_explicit_temproot_is_left_alone(tmp_path, temproot_env, deny_writes_to):
    # The operator has already chosen a root (the escape hatch for a machine
    # whose %TEMP% is unusable) — pytest reads that variable itself.
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    deny_writes_to.append(root)
    chosen = tmp_path / "chosen-root"
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(chosen)

    _ensure_writable_pytest_root(temproot=tmp_path)

    assert os.environ["PYTEST_DEBUG_TEMPROOT"] == str(chosen)
    # pytest's own mkdir has no parents=True, so the root has to exist.
    assert chosen.is_dir()


def test_a_missing_root_is_created(tmp_path, temproot_env):
    root = _pytest_root_dir(tmp_path)
    assert not root.exists()

    _ensure_writable_pytest_root(temproot=tmp_path)

    assert root.is_dir()
    # Nothing was broken — the default root was fine.
    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ


def test_a_usable_root_is_left_alone(tmp_path, temproot_env):
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    (root / "pytest-0").mkdir()

    _ensure_writable_pytest_root(temproot=tmp_path)

    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ


def test_a_readable_but_unwritable_root_falls_back(tmp_path, temproot_env, deny_writes_to):
    # A root left by an elevated process is typically listable but not
    # writable: a read check would wave it through, a write would not.
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    deny_writes_to.append(root)

    _ensure_writable_pytest_root(temproot=tmp_path)

    fallback = Path(os.environ["PYTEST_DEBUG_TEMPROOT"])
    assert fallback != root
    assert fallback.is_dir()
    # pytest still files its own pytest-of-<user> directory inside the fallback.
    assert _pytest_root_dir(fallback).parent == fallback


def test_a_symlinked_root_falls_back(tmp_path, temproot_env, monkeypatch):
    # pytest refuses a symlinked root outright.
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    real_stat = Path.stat

    def stat_as_symlink(self, *args, **kwargs):
        if self == root:
            info = real_stat(self, *args, **kwargs)
            return os.stat_result(
                (
                    stat.S_IFLNK | 0o777,
                    info.st_ino,
                    info.st_dev,
                    1,
                    info.st_uid,
                    info.st_gid,
                    0,
                    0,
                    0,
                    0,
                )
            )
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_as_symlink)

    _ensure_writable_pytest_root(temproot=tmp_path)

    assert Path(os.environ["PYTEST_DEBUG_TEMPROOT"]) != root


@pytest.mark.skipif(os.name == "nt", reason="Windows has no uid to compare")
def test_a_root_owned_by_another_user_falls_back(tmp_path, temproot_env, monkeypatch):
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    real_stat = Path.stat

    def stat_as_foreign(self, *args, **kwargs):
        if self == root:
            info = real_stat(self, *args, **kwargs)
            return os.stat_result(
                (
                    info.st_mode,
                    info.st_ino,
                    info.st_dev,
                    1,
                    info.st_uid + 1,
                    info.st_gid,
                    0,
                    0,
                    0,
                    0,
                )
            )
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_as_foreign)

    _ensure_writable_pytest_root(temproot=tmp_path)

    assert Path(os.environ["PYTEST_DEBUG_TEMPROOT"]) != root


def test_the_fallback_is_not_shared_between_runs(tmp_path, temproot_env, deny_writes_to):
    # pytest allocates its per-run directories under the root it is given, so a
    # shared fallback would have concurrent runs fighting over the same ones.
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    deny_writes_to.append(root)

    _ensure_writable_pytest_root(temproot=tmp_path)
    first = os.environ["PYTEST_DEBUG_TEMPROOT"]
    os.environ.pop("PYTEST_DEBUG_TEMPROOT", None)

    _ensure_writable_pytest_root(temproot=tmp_path)
    second = os.environ["PYTEST_DEBUG_TEMPROOT"]

    assert first != second


def test_an_unusable_fallback_leaves_pytest_to_report_the_real_error(
    tmp_path, temproot_env, deny_writes_to, monkeypatch
):
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    deny_writes_to.append(root)
    # The fallback lives next to the root, so denying writes to the parent
    # takes the fallback down too.
    deny_writes_to.append(tmp_path)

    _ensure_writable_pytest_root(temproot=tmp_path)

    # No usable root anywhere — pytest's own error is the useful one.
    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ


def test_the_probe_covers_the_missing_root_case(tmp_path):
    # A missing root is fine: pytest creates it itself.
    root = _pytest_root_dir(tmp_path)
    assert _pytest_root_usable(root) is True
    assert root.is_dir()
