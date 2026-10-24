"""The per-worker isolation paths the test suite runs under.

The suite's throwaway HOME and config root have to differ per xdist worker:
four workers sharing one app database file would contend on it. The split
happens in ``tests/conftest.py`` at import time, before the app is imported
and its settings singleton reads the paths -- so it cannot be pinned by a
fixture, and the paths it produces are what the workers actually use.
"""

from __future__ import annotations

from pathlib import Path


def worker_paths(isolation_root: Path, worker: str) -> tuple[Path, Path]:
    """The home and config roots for ``worker`` under ``isolation_root``.

    An empty ``worker`` is the serial run: one home and one config root.
    """
    suffix = f"-{worker}" if worker else ""
    return isolation_root / f"home{suffix}", isolation_root / f"config{suffix}"
