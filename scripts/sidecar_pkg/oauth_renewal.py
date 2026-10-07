"""Pieces shared by the sidecar's OAuth login renewers (xAI, Claude Code).

Each renewer refreshes a CLI's login and writes the result back into the CLI's own
credentials file, so the CLI stays logged in even though the provider rotates refresh tokens.
Only the parts that are genuinely provider-independent live here.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class RefreshRejectedError(Exception):
    """The token endpoint refused the refresh token (``invalid_grant`` etc.)."""


def atomic_replace_json(path: Path, data: Any, mode: int, *, prefix: str = ".cred-") -> bool:
    """Replace *path* with *data* atomically, keeping *mode*; False if it could not be written.

    A temp file is created in the same directory (so ``os.replace`` is atomic) and the *real* file
    is replaced, not a dotfile-manager symlink pointing at it. A failed write leaves no temp file.
    """
    target = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except OSError:
        # Best-effort cleanup of our temp file; the write already failed, nothing else to do.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    return True
