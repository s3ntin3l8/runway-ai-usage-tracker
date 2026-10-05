"""Unit tests for scripts/sidecar_pkg/runtime_cleanup.py (stale PyInstaller temp state)."""

import os
import pathlib
import sys
import time

import pytest

from scripts.sidecar_pkg import runtime_cleanup as rc


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Pose as a onefile build whose runtime dir is ``tmp_path/_MEIown``."""
    runtime = tmp_path / "_MEIown"
    runtime.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(runtime), raising=False)
    monkeypatch.delenv(rc.RETIRED_ENV, raising=False)
    return runtime


def _make_dir(parent: pathlib.Path, name: str, owner: int | None = None) -> pathlib.Path:
    d = parent / name
    d.mkdir()
    (d / "libpython.so").write_bytes(b"x" * 1000)
    if owner is not None:
        (d / rc.OWNER_MARKER).write_text(str(owner))
    return d


def _dead_pid() -> int:
    pid = 2_000_000
    while rc.pid_is_alive(pid):
        pid += 1
    return pid


class TestOwnRuntimeDir:
    def test_none_when_not_frozen(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        assert rc.own_runtime_dir() is None

    def test_none_for_an_onedir_bundle(self, monkeypatch, tmp_path):
        """macOS .app: _MEIPASS is inside the bundle, not an ``_MEI*`` dir."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(
            sys, "_MEIPASS", str(tmp_path / "Runway.app/Contents/Frameworks"), raising=False
        )
        assert rc.own_runtime_dir() is None

    def test_returns_the_mei_dir(self, frozen):
        assert rc.own_runtime_dir() == frozen


class TestClaim:
    def test_writes_our_pid(self, frozen):
        rc.claim_runtime_dir()
        assert (frozen / rc.OWNER_MARKER).read_text() == str(os.getpid())

    def test_noop_when_not_frozen(self, monkeypatch, tmp_path):
        monkeypatch.delattr(sys, "frozen", raising=False)
        rc.claim_runtime_dir()
        assert not list(tmp_path.iterdir())


class TestSweepStaleRuntimeDirs:
    def test_deletes_a_dead_owners_dir(self, frozen):
        stale = _make_dir(frozen.parent, "_MEIdead", owner=_dead_pid())
        freed = rc.sweep_stale_runtime_dirs()
        assert not stale.exists()
        assert freed >= 1000

    def test_keeps_a_live_owners_dir(self, frozen):
        live = _make_dir(frozen.parent, "_MEIlive", owner=os.getpid())
        assert rc.sweep_stale_runtime_dirs() == 0
        assert live.exists()

    def test_never_touches_an_unmarked_dir(self, frozen):
        """Another PyInstaller app's runtime, or a pre-marker Runway leftover."""
        other = _make_dir(frozen.parent, "_MEIother")
        rc.sweep_stale_runtime_dirs()
        assert other.exists()

    def test_never_touches_our_own_dir(self, frozen):
        (frozen / rc.OWNER_MARKER).write_text(str(_dead_pid()))
        rc.sweep_stale_runtime_dirs()
        assert frozen.exists()

    def test_ignores_non_mei_names(self, frozen):
        keep = _make_dir(frozen.parent, "runway-keep", owner=_dead_pid())
        rc.sweep_stale_runtime_dirs()
        assert keep.exists()

    def test_ignores_a_garbled_marker(self, frozen):
        d = _make_dir(frozen.parent, "_MEIbad")
        (d / rc.OWNER_MARKER).write_text("not-a-pid")
        rc.sweep_stale_runtime_dirs()
        assert d.exists()

    def test_does_not_follow_a_symlinked_dir(self, frozen, tmp_path):
        real = _make_dir(tmp_path, "elsewhere", owner=_dead_pid())
        (frozen.parent / "_MEIlink").symlink_to(real, target_is_directory=True)
        rc.sweep_stale_runtime_dirs()
        assert real.exists()

    def test_noop_when_not_frozen(self, monkeypatch, tmp_path):
        monkeypatch.delattr(sys, "frozen", raising=False)
        d = _make_dir(tmp_path, "_MEIdead", owner=_dead_pid())
        assert rc.sweep_stale_runtime_dirs() == 0
        assert d.exists()


class TestReleaseRetiredRuntime:
    def test_deletes_the_handed_over_sibling_and_pops_the_var(self, frozen, monkeypatch):
        old = _make_dir(frozen.parent, "_MEIold")
        monkeypatch.setenv(rc.RETIRED_ENV, str(old))
        assert rc.release_retired_runtime() >= 1000
        assert not old.exists()
        assert rc.RETIRED_ENV not in os.environ

    def test_pops_the_var_even_when_it_refuses(self, frozen, monkeypatch):
        monkeypatch.setenv(rc.RETIRED_ENV, "relative/_MEIold")
        assert rc.release_retired_runtime() == 0
        assert rc.RETIRED_ENV not in os.environ

    def test_refuses_our_own_runtime(self, frozen, monkeypatch):
        monkeypatch.setenv(rc.RETIRED_ENV, str(frozen))
        assert rc.release_retired_runtime() == 0
        assert frozen.exists()

    def test_refuses_a_non_sibling(self, frozen, monkeypatch, tmp_path):
        elsewhere = tmp_path / "sub"
        elsewhere.mkdir()
        far = _make_dir(elsewhere, "_MEIfar")
        monkeypatch.setenv(rc.RETIRED_ENV, str(far))
        assert rc.release_retired_runtime() == 0
        assert far.exists()

    def test_refuses_a_non_mei_name(self, frozen, monkeypatch):
        d = _make_dir(frozen.parent, "important")
        monkeypatch.setenv(rc.RETIRED_ENV, str(d))
        assert rc.release_retired_runtime() == 0
        assert d.exists()

    def test_refuses_a_dotdot_escape(self, frozen, monkeypatch, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        far = _make_dir(sub, "_MEIfar")
        monkeypatch.setenv(rc.RETIRED_ENV, f"{frozen.parent}/sub/../sub/{far.name}")
        assert rc.release_retired_runtime() == 0
        assert far.exists()

    def test_refuses_a_symlink(self, frozen, monkeypatch, tmp_path):
        real = _make_dir(tmp_path, "elsewhere")
        link = frozen.parent / "_MEIlink"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setenv(rc.RETIRED_ENV, str(link))
        assert rc.release_retired_runtime() == 0
        assert real.exists()

    def test_pops_the_var_when_not_frozen(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setenv(rc.RETIRED_ENV, "/tmp/_MEIx")
        assert rc.release_retired_runtime() == 0
        assert rc.RETIRED_ENV not in os.environ


class TestSweepStaleUpdateDirs:
    @pytest.fixture(autouse=True)
    def _tmp(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rc.tempfile, "gettempdir", lambda: str(tmp_path))

    def test_deletes_an_old_dir_and_keeps_a_fresh_one(self, tmp_path):
        old = _make_dir(tmp_path, "runway-update-old")
        fresh = _make_dir(tmp_path, "runway-update-fresh")
        long_ago = time.time() - 2 * 3600
        os.utime(old, (long_ago, long_ago))
        assert rc.sweep_stale_update_dirs() >= 1000
        assert not old.exists()
        assert fresh.exists()

    def test_ignores_other_names(self, tmp_path):
        other = _make_dir(tmp_path, "something-else")
        long_ago = time.time() - 2 * 3600
        os.utime(other, (long_ago, long_ago))
        rc.sweep_stale_update_dirs()
        assert other.exists()


class TestRecoverStrandedRollback:
    @pytest.fixture
    def install(self, monkeypatch, tmp_path):
        from scripts.sidecar_pkg import self_update

        exe = tmp_path / "runway-sidecar-cli"
        exe.write_bytes(b"current")
        monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
        monkeypatch.setattr(self_update, "_install_path", lambda: exe)
        return exe

    @staticmethod
    def _age(path: pathlib.Path) -> float:
        # ctime cannot be set; judge "old enough" by moving the clock instead.
        return path.lstat().st_ctime + 3600

    def test_restores_the_backup_when_previous_is_missing(self, install):
        stranded = install.with_name(install.name + ".rollback")
        stranded.write_bytes(b"backup")
        rc.recover_stranded_rollback(now=self._age(stranded))
        assert not stranded.exists()
        assert install.with_name(install.name + ".previous").read_bytes() == b"backup"

    def test_drops_the_duplicate_when_previous_exists(self, install):
        stranded = install.with_name(install.name + ".rollback")
        stranded.write_bytes(b"dup")
        previous = install.with_name(install.name + ".previous")
        previous.write_bytes(b"keep")
        assert rc.recover_stranded_rollback(now=self._age(stranded)) == 3
        assert not stranded.exists()
        assert previous.read_bytes() == b"keep"

    def test_leaves_a_rollback_that_may_still_be_running(self, install):
        stranded = install.with_name(install.name + ".rollback")
        stranded.write_bytes(b"backup")
        rc.recover_stranded_rollback()
        assert stranded.exists()

    def test_noop_when_nothing_is_stranded(self, install):
        assert rc.recover_stranded_rollback() == 0

    def test_noop_when_not_frozen(self, install, monkeypatch):
        from scripts.sidecar_pkg import self_update

        monkeypatch.setattr(self_update, "_is_frozen", lambda: False)
        stranded = install.with_name(install.name + ".rollback")
        stranded.write_bytes(b"backup")
        rc.recover_stranded_rollback(now=self._age(stranded))
        assert stranded.exists()


class TestStartupCleanup:
    def test_never_raises_and_reports_what_it_freed(self, frozen, monkeypatch, caplog):
        _make_dir(frozen.parent, "_MEIdead", owner=_dead_pid())
        monkeypatch.setattr(rc, "sweep_stale_update_dirs", lambda: 0)
        monkeypatch.setattr(rc, "recover_stranded_rollback", lambda: 0)
        with caplog.at_level("INFO"):
            rc.startup_cleanup()
        assert "Reclaimed" in caplog.text
        assert (frozen / rc.OWNER_MARKER).exists()

    def test_a_failing_step_does_not_stop_the_others(self, frozen, monkeypatch):
        stale = _make_dir(frozen.parent, "_MEIdead", owner=_dead_pid())

        def boom() -> int:
            raise OSError("nope")

        monkeypatch.setattr(rc, "sweep_stale_update_dirs", boom)
        # `startup_cleanup` iterates a tuple of names it resolved at call time.
        monkeypatch.setattr(rc, "recover_stranded_rollback", boom)
        rc.startup_cleanup()
        assert not stale.exists()
