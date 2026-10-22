"""Non-blocking per-user exclusion for Runway's Claude file and Keychain renewers."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

_THREAD_LOCK = threading.Lock()


def _open_user_lock() -> int:
    import fcntl

    root = Path.home() / "Library/Caches/Runway/renewal"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError("unsafe renewal lock directory")
    fd = os.open(root / "claude.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("unsafe renewal lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        raise


@contextlib.contextmanager
def claude_renewal_lock() -> Iterator[bool]:
    """Yield False on contention or unsafe storage; body exceptions propagate unchanged."""
    if not _THREAD_LOCK.acquire(blocking=False):
        yield False
        return
    fd = None
    try:
        try:
            if sys.platform == "darwin":
                fd = _open_user_lock()
            acquired = True
        except OSError:
            acquired = False  # Unsafe storage or contention: skip rather than run unlocked.
        yield acquired
    finally:
        if fd is not None:
            os.close(fd)
        _THREAD_LOCK.release()
