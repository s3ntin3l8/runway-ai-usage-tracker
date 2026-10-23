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
        assert (frozen / rc.OWNER_MARKER).read_text(encoding="utf-8") == str(os.getpid())

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

    def test_does_not_follow_a_symlinked_dir(self, frozen, tmp_path, make_symlink):
        real = _make_dir(tmp_path, "elsewhere", owner=_dead_pid())
        make_symlink(frozen.parent / "_MEIlink", real, target_is_directory=True)
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

    def test_refuses_a_symlink(self, frozen, monkeypatch, tmp_path, make_symlink):
        real = _make_dir(tmp_path, "elsewhere")
        link = frozen.parent / "_MEIlink"
        make_symlink(link, real, target_is_directory=True)
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


class TestStartupCleanup:
    def test_never_raises_and_reports_what_it_freed(self, frozen, monkeypatch, caplog):
        _make_dir(frozen.parent, "_MEIdead", owner=_dead_pid())
        monkeypatch.setattr(rc, "sweep_stale_update_dirs", lambda: 0)
        with caplog.at_level("INFO"):
            rc.startup_cleanup()
        assert "Reclaimed" in caplog.text
        assert (frozen / rc.OWNER_MARKER).exists()

    def test_a_failing_step_does_not_stop_the_others(self, frozen, monkeypatch):
        stale = _make_dir(frozen.parent, "_MEIdead", owner=_dead_pid())

        def boom() -> int:
            raise OSError("nope")

        monkeypatch.setattr(rc, "sweep_stale_update_dirs", boom)
        rc.startup_cleanup()
        assert not stale.exists()


class TestWindowsPidProbe:
    """The Windows liveness probe must never call a terminated process alive.

    ``OpenProcess`` succeeds for a process that exited while a handle to it
    stayed open (the PyInstaller onefile bootloader, the update helper, a console
    host), so only the process object's signalled state proves termination.
    Reading it the other way left the single-instance lock holding a dead PID
    forever, and the sidecar refused to start after every self-update.
    """

    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258

    @staticmethod
    def _fake_kernel(monkeypatch, *, handle, wait_result=None, error=0):
        """Replace kernel32 with a fake; ``handle`` None means OpenProcess fails."""
        import ctypes
        from types import SimpleNamespace

        closed: list[object] = []

        def open_process(*args):
            return handle

        def wait(*args):
            assert wait_result is not None, "WaitForSingleObject ran without a handle"
            return wait_result

        def close(closed_handle):
            closed.append(closed_handle)
            return 1

        kernel = SimpleNamespace(
            OpenProcess=open_process, WaitForSingleObject=wait, CloseHandle=close
        )
        monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
        return closed

    def test_a_terminated_process_is_not_alive(self, monkeypatch):
        self._fake_kernel(monkeypatch, handle=1, wait_result=self.WAIT_OBJECT_0)
        assert rc.windows_pid_is_alive(4242) is False

    def test_a_running_process_is_alive(self, monkeypatch):
        self._fake_kernel(monkeypatch, handle=1, wait_result=self.WAIT_TIMEOUT)
        assert rc.windows_pid_is_alive(4242) is True

    def test_a_pid_that_never_existed_is_not_alive(self, monkeypatch):
        # ERROR_INVALID_PARAMETER (87): the PID is not in use at all.
        self._fake_kernel(monkeypatch, handle=None, error=87)
        assert rc.windows_pid_is_alive(4242) is False

    def test_an_unknown_status_is_treated_as_alive(self, monkeypatch):
        # ACCESS_DENIED (5) and friends: the process may belong to another user,
        # so report it as alive rather than clobbering a live sidecar.
        for error in (5, 6, 0):
            self._fake_kernel(monkeypatch, handle=None, error=error)
            assert rc.windows_pid_is_alive(4242) is True

    def test_the_handle_is_closed(self, monkeypatch):
        closed = self._fake_kernel(monkeypatch, handle=7, wait_result=self.WAIT_OBJECT_0)
        rc.windows_pid_is_alive(4242)
        assert closed == [7]

    def test_pid_is_alive_uses_the_windows_probe_on_windows(self, monkeypatch):
        seen: list[int] = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(rc, "windows_pid_is_alive", lambda pid: seen.append(pid) or True)
        assert rc.pid_is_alive(4242) is True
        assert seen == [4242]
