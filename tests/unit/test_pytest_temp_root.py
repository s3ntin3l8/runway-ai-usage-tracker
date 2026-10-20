"""The pytest temp root must be usable, or every test errors out before it runs.

pytest keeps its per-run directories in ``%TEMP%\\pytest-of-<user>``. A
directory left behind by another user's (or an elevated) process can carry an
ACL that lets this user stat it but not list it, and pytest then fails every
single test with ``PermissionError`` before one of them runs.
"""

import os
import tempfile
from pathlib import Path

from tests.conftest import _ensure_writable_pytest_root, _pytest_root_dir


def test_a_missing_root_is_created(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    root = _pytest_root_dir(tmp_path)
    assert not root.exists()

    _ensure_writable_pytest_root(temproot=tmp_path)

    assert root.is_dir()
    # Nothing was broken — the default root was fine.
    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ


def test_a_readable_root_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    (root / "pytest-0").mkdir()

    _ensure_writable_pytest_root(temproot=tmp_path)

    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ


def test_an_unreadable_root_falls_back_to_a_fresh_one(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    real_listdir = os.listdir

    def deny_listing(path, *args, **kwargs):
        if Path(path) == root:
            raise PermissionError(13, "Access is denied", str(path))
        return real_listdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "listdir", deny_listing)

    _ensure_writable_pytest_root(temproot=tmp_path)

    fallback = Path(os.environ["PYTEST_DEBUG_TEMPROOT"])
    assert fallback != root
    assert fallback.is_dir()
    # pytest still files its own pytest-of-<user> directory inside the fallback.
    assert _pytest_root_dir(fallback).parent == fallback


def test_the_fallback_is_not_shared_between_runs(tmp_path, monkeypatch):
    # pytest allocates its per-run directories under the root it is given, so a
    # shared fallback would have concurrent runs fighting over the same ones.
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    real_listdir = os.listdir

    def deny_listing(path, *args, **kwargs):
        if Path(path) == root:
            raise PermissionError(13, "Access is denied", str(path))
        return real_listdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "listdir", deny_listing)

    _ensure_writable_pytest_root(temproot=tmp_path)
    first = os.environ["PYTEST_DEBUG_TEMPROOT"]
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)

    _ensure_writable_pytest_root(temproot=tmp_path)
    second = os.environ["PYTEST_DEBUG_TEMPROOT"]

    assert first != second


def test_an_unusable_fallback_leaves_pytest_to_report_the_real_error(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    root = _pytest_root_dir(tmp_path)
    root.mkdir()
    real_listdir = os.listdir

    def deny_listing(path, *args, **kwargs):
        if Path(path) == root:
            raise PermissionError(13, "Access is denied", str(path))
        return real_listdir(path, *args, **kwargs)

    def deny_fallback(*args, **kwargs):
        raise PermissionError(13, "Access is denied", "mkdtemp")

    monkeypatch.setattr(os, "listdir", deny_listing)
    monkeypatch.setattr(tempfile, "mkdtemp", deny_fallback)

    _ensure_writable_pytest_root(temproot=tmp_path)

    # No usable root anywhere — pytest's own error is the useful one.
    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ
