"""Build identity for the running server: the commit the code was built from.

``app.__version__`` is a Release Please constant, so it names a *release*, not a
build — every ``:edge`` image cut from main carries the same version string.
The commit SHA is what pins a running container to one exact source tree, so
Settings → About can show it and a reader can jump to the diff.

Resolution order:

1. ``RUNWAY_BUILD_SHA`` — baked into the Docker image at build time (the
   ``BUILD_SHA`` build arg in the Dockerfile) and settable by operators for
   source or pip installs.
2. ``git rev-parse HEAD`` — only when the server runs from a source checkout,
   so ``make dev`` reports the working tree's commit.
3. ``None`` — nothing to report; the UI shows "—".

The value is validated as hex before it is handed out: it ends up in a link to
the commit page, so a stray env var must never become markup.
"""

import os
import re
import shutil
import subprocess  # noqa: S404 — fixed argv, no shell, no user-controlled input
from functools import lru_cache
from pathlib import Path

BUILD_SHA_ENV = "RUNWAY_BUILD_SHA"

# A git object name: 7-40 hex chars. Anything else is not a commit we can link to.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# app/core/build_info.py -> app/core -> app -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def get_build_sha() -> str | None:
    """Return the commit SHA this server was built from, or None if unknown.

    Cached: neither the env var nor the git checkout changes while the
    process runs, and the git fallback spawns a subprocess.
    """
    return _env_sha() or _git_sha()


def _env_sha() -> str | None:
    """The operator/build-supplied SHA, if it looks like a commit."""
    raw = os.getenv(BUILD_SHA_ENV, "").strip().lower()
    return raw if _SHA_RE.match(raw) else None


def _git_sha() -> str | None:
    """HEAD of the checkout this server runs from, if there is one.

    Installed packages and the Docker image ship no ``.git``, so this is the
    dev path only. A worktree's ``.git`` is a *file* pointing at the real
    gitdir, hence the ``exists()`` check rather than ``is_dir()``.
    """
    if not (_REPO_ROOT / ".git").exists():
        return None
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip().lower()
    return sha if result.returncode == 0 and _SHA_RE.match(sha) else None
