"""The git repository a working directory belongs to, as a name.

The `project` label on a usage event is derived on the server from the client's
working directory. The folder's name is a good label — until the same
repository is checked out under two names, on two hosts, and each clone lands in
a bucket of its own.

This module resolves the repository on the **client's** host, because the server
never reads the client's filesystem (see `AGENTS.md`). Only the repository's
*name* is ever sent onward: a remote URL can embed a username and a token, so
it is reduced to its last path segment here and never leaves this module.

The probe is best-effort by design. A missing `git`, a directory that is not a
repository, a timeout — every failure degrades to `None`, and the server then
labels the row with the folder's name as before. Nothing here can fail a
collection cycle.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

#: How long a git call may take. The probe runs inside a collection cycle, so
#: it must not hold the cycle up.
_TIMEOUT_SECONDS = 3

#: A repository name longer than this is not a name; anything this long is
#: either a mistake or an attempt to smuggle data into the label.
_MAX_NAME_LENGTH = 128

#: How many working directories are remembered per process. A collection cycle
#: touches a handful of directories, so this is generous.
_CACHE_LIMIT = 512

#: The last path segment of a remote URL, split on every separator a URL may
#: use: `/` for the path, `:` for the scp-like form (`git@host:owner/repo.git`).
_URL_SEPARATORS = "/\\:"

_cache: dict[str, str | None] = {}


def _creationflags() -> int:
    """Hide the console window a child command opens on Windows."""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _git(args: list[str], cwd: str) -> str | None:
    """Run one git command in `cwd`; its trimmed output, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            creationflags=_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or "").strip()
    return output or None


def _sanitize(name: str | None) -> str | None:
    """Make a repository name safe to use as a label."""
    if not name:
        return None
    # Control characters have no business in a label and would break the UI.
    cleaned = "".join(char for char in name if char.isprintable()).strip()
    if not cleaned or cleaned in {".", ".."}:
        return None
    if len(cleaned) > _MAX_NAME_LENGTH:
        cleaned = cleaned[:_MAX_NAME_LENGTH]
    return cleaned or None


def repo_name_from_url(url: str | None) -> str | None:
    """The repository name a remote URL names — its last path segment.

    `https://github.com/owner/repo.git` → `repo`
    `git@github.com:owner/repo.git` → `repo`
    `ssh://git@host/owner/repo` → `repo`

    The host, the owner and any credentials in the URL are dropped here and
    never returned.
    """
    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    # A trailing "/" and the conventional ".git" suffix are not part of the name.
    candidate = candidate.rstrip("/")
    if candidate.lower().endswith(".git"):
        candidate = candidate[: -len(".git")]
    segments = candidate.replace("\\", "/").split("/")
    # The scp-like form keeps the owner behind a ":" — take what follows it.
    last = segments[-1] if segments else ""
    if ":" in last:
        last = last.rsplit(":", 1)[-1]
    return _sanitize(last)


def _resolve(cwd: str) -> str | None:
    """The repository name for `cwd`, without the cache."""
    if shutil.which("git") is None:
        return None
    if not os.path.isdir(cwd):
        return None
    # `--show-toplevel` resolves the repository root from anywhere inside it,
    # including a worktree or a subfolder.
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    if top is None:
        return None
    remote = _git(["config", "--get", "remote.origin.url"], cwd)
    name = repo_name_from_url(remote)
    if name:
        return name
    # A repository without an origin remote: the root's own folder name is the
    # best identity available.
    return repo_name_from_url(top)


def resolve_repo_name(cwd: str | None) -> str | None:
    """The repository name for a working directory, or None.

    The answer is cached per directory: a collection cycle touches the same
    directories over and over, and one `git` call per directory per cycle is
    enough.
    """
    if not cwd:
        return None
    key = cwd.strip()
    if not key:
        return None
    if key in _cache:
        return _cache[key]
    try:
        name = _resolve(key)
    except Exception:  # pragma: no cover — defensive only
        name = None
    if len(_cache) >= _CACHE_LIMIT:
        _cache.clear()
    _cache[key] = name
    return name


def annotate_project_refs(events: list[dict[str, Any]]) -> int:
    """Give every event the repository name of its working directory.

    Events without a working directory, and those whose directory belongs to no
    repository, are left alone — the server labels them with the folder's name.
    """
    resolved = 0
    for event in events:
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        name = resolve_repo_name(cwd)
        if name:
            event["project_ref"] = name
            resolved += 1
    return resolved


def clear_cache() -> None:
    """Forget the resolved repositories (used by the tests)."""
    _cache.clear()
