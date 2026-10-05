"""Reclaim temp state a frozen sidecar leaves behind when it is killed or re-exec'd.

A PyInstaller onefile build unpacks itself into ``<tmp>/_MEI*`` and its parent
bootloader removes that directory when the Python child exits. That never happens
after a SIGKILL / OOM / power loss, and a self-update ``execve`` leaves the *old*
runtime behind too (its parent bootloader keeps waiting on the re-exec'd image).
Everything here is best-effort and must never stop the sidecar from starting.
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import sys
import tempfile
import time

logger = logging.getLogger(__name__)

# Set by the relaunching image (see ``self_update._fresh_runtime_env``): the runtime dir
# it is abandoning, so the new image can delete it.
RETIRED_ENV = "RUNWAY_RETIRED_MEIPASS"
# Written into our own runtime dir so a later sweep can tell a Runway runtime (and its
# owner) from some other PyInstaller app's ``_MEI*`` directory.
OWNER_MARKER = ".runway-owner"
_MEI_PREFIX = "_MEI"
_UPDATE_DIR_PREFIX = "runway-update-"
# A live update downloads + extracts in seconds; anything this old was orphaned.
_UPDATE_DIR_MAX_AGE_S = 3600
# A rollback swaps in milliseconds; older than this means it was interrupted.
_ROLLBACK_MAX_AGE_S = 600


def pid_is_alive(pid: int) -> bool:
    """Return True if a process with `pid` is currently running."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(1, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it; treat as alive so we don't
        # clobber another user's sidecar.
        return True
    except OSError:
        return False
    return True


def own_runtime_dir() -> pathlib.Path | None:
    """This process's onefile runtime dir, or None when not a onefile build.

    The ``_MEI`` prefix check also excludes macOS ``.app`` onedir bundles, whose
    ``_MEIPASS`` points inside the bundle and must never be touched.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) or not meipass:
        return None
    path = pathlib.Path(meipass)
    return path if path.name.startswith(_MEI_PREFIX) else None


def _size(path: pathlib.Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _remove(path: pathlib.Path) -> int:
    """Delete ``path`` (best-effort) and return the bytes it held."""
    freed = _size(path) if path.is_dir() and not path.is_symlink() else 0
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            return 0
    return freed if not path.exists() else 0


def claim_runtime_dir() -> None:
    """Mark our runtime dir with our PID so a later sweep knows it is ours and in use."""
    runtime = own_runtime_dir()
    if runtime is None:
        return
    try:
        (runtime / OWNER_MARKER).write_text(str(os.getpid()))
    except OSError:
        logger.debug("Could not mark runtime dir %s", runtime, exc_info=True)


def release_retired_runtime() -> int:
    """Delete the runtime dir a self-update re-exec handed over; return bytes freed.

    Always pops ``RUNWAY_RETIRED_MEIPASS`` so it never reaches a child process. Only a
    sibling ``_MEI*`` directory of our own runtime is ever deleted. The retired image's
    parent bootloader later logs a harmless "Failed to remove temporary directory" once.
    """
    raw = os.environ.pop(RETIRED_ENV, None)
    runtime = own_runtime_dir()
    if not raw or runtime is None:
        return 0
    target = pathlib.Path(raw)
    if (
        not target.is_absolute()
        or not target.name.startswith(_MEI_PREFIX)
        or target.is_symlink()
        or not target.is_dir()
        or target.parent.resolve() != runtime.parent.resolve()
        or target.resolve() == runtime.resolve()
    ):
        logger.debug("Ignoring retired runtime %s", raw)
        return 0
    return _remove(target)


def sweep_stale_runtime_dirs() -> int:
    """Delete ``_MEI*`` siblings whose recorded owner PID is dead; return bytes freed.

    Dirs without our marker (other PyInstaller apps, or leftovers from builds that
    predate the marker) are never touched. A reused PID only skips a delete.
    """
    runtime = own_runtime_dir()
    if runtime is None:
        return 0
    freed = 0
    try:
        siblings = list(runtime.parent.iterdir())
    except OSError:
        return 0
    for entry in siblings:
        if not entry.name.startswith(_MEI_PREFIX) or entry == runtime:
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            owner = int((entry / OWNER_MARKER).read_text().strip())
        except (OSError, ValueError):
            continue
        if not pid_is_alive(owner):
            freed += _remove(entry)
    return freed


def sweep_stale_update_dirs(now: float | None = None) -> int:
    """Delete ``runway-update-*`` download dirs orphaned by a kill mid-update."""
    cutoff = (time.time() if now is None else now) - _UPDATE_DIR_MAX_AGE_S
    freed = 0
    try:
        entries = list(pathlib.Path(tempfile.gettempdir()).glob(_UPDATE_DIR_PREFIX + "*"))
    except OSError:
        return 0
    for entry in entries:
        try:
            st = entry.lstat()
            if not entry.is_dir() or entry.is_symlink() or st.st_mtime > cutoff:
                continue
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                continue
        except OSError:
            continue
        freed += _remove(entry)
    return freed


def recover_stranded_rollback(now: float | None = None) -> int:
    """Undo a ``rollback()`` that was killed between its two renames.

    ``rollback()`` moves ``<install>.previous`` to ``<install>.rollback`` before the swap.
    A kill in between strands the backup there, and ``rollback_available()`` then reports
    nothing to roll back to. Put it back (or drop the duplicate); return bytes freed.
    """
    from scripts.sidecar_pkg import self_update

    if not self_update._is_frozen():
        return 0
    install = self_update._install_path()
    stranded = install.with_name(install.name + ".rollback")
    if not stranded.exists():
        return 0
    cutoff = (time.time() if now is None else now) - _ROLLBACK_MAX_AGE_S
    # ctime, not mtime: the rename updates it, mtime is that of the original backup.
    if stranded.lstat().st_ctime > cutoff:
        return 0
    previous = self_update._previous_path(install)
    if not previous.exists():
        os.rename(stranded, previous)
        logger.info("Restored the rollback backup stranded at %s", stranded)
        return 0
    freed = _size(stranded) if stranded.is_dir() else stranded.stat().st_size
    self_update._rm(stranded)
    return freed


def startup_cleanup() -> None:
    """Claim our runtime dir, then reclaim everything dead builds left behind."""
    freed = 0
    claim_runtime_dir()
    for step in (sweep_stale_runtime_dirs, sweep_stale_update_dirs, recover_stranded_rollback):
        try:
            freed += step()
        except Exception:
            logger.debug("Startup cleanup step %s failed", step.__name__, exc_info=True)
    if freed:
        logger.info("Reclaimed %.1f MB of stale sidecar temp files", freed / 1_000_000)
