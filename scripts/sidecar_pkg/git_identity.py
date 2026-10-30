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

import json
import logging
import os
import secrets
import shutil
import subprocess
from pathlib import Path
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

#: How many resolved directories survive a restart. Oldest entries go first.
_PERSIST_LIMIT = 4096

_STATE_VERSION = 1

_cache: dict[str, str | None] = {}

#: Directory -> repository name for every directory that resolved once, kept in
#: the sidecar's state so a worktree that is deleted later keeps its name. Only
#: successes live here: a None may be a temporary failure (git busy, a directory
#: not created yet), and recording it would hide a later success.
_persisted: dict[str, str] = {}


class _State:
    """Where the persisted cache lives and whether it changed since it was written."""

    path: Path | None = None
    dirty: bool = False


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
    if name:
        _remember(key, name)
    elif key in _persisted:
        # The directory is gone (or git failed): the name it resolved to once
        # is still the right one. A live answer, when there is one, wins above,
        # so a renamed remote is picked up after the next restart.
        name = _persisted[key]
    if len(_cache) >= _CACHE_LIMIT:
        _cache.clear()
    _cache[key] = name
    return name


def _remember(key: str, name: str) -> None:
    """Record a successful resolution for the state file."""
    if _persisted.get(key) == name:
        return
    _persisted.pop(key, None)  # re-insert at the end: the freshest entry survives longest
    _persisted[key] = name
    while len(_persisted) > _PERSIST_LIMIT:
        _persisted.pop(next(iter(_persisted)))
    _State.dirty = True


def configure(path: Path | None) -> None:
    """Load the persisted directory -> repository cache from `path`.

    Called once per process before the first annotation; calling it again with
    the same path is a no-op. A missing or unreadable file is an empty cache.
    """
    if path is not None and path == _State.path:
        return
    _persisted.clear()
    _State.path = path
    _State.dirty = False
    if path is None:
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return
    for key, name in entries.items():
        if isinstance(key, str) and key and isinstance(name, str):
            clean = _sanitize(name)
            if clean:
                _persisted[key] = clean
    while len(_persisted) > _PERSIST_LIMIT:
        _persisted.pop(next(iter(_persisted)))


def save() -> None:
    """Write the persisted cache if it changed. Best-effort, atomic."""
    path = _State.path
    if path is None or not _State.dirty:
        return
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _STATE_VERSION, "entries": _persisted}
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)  # paths of the user's projects
        except OSError:
            logging.debug("git identity: state file permissions not narrowed")
        os.replace(temporary, path)
        _State.dirty = False
    except OSError as exc:
        logging.debug("git identity: state not saved (%s)", type(exc).__name__)
        try:
            temporary.unlink()
        except OSError:
            logging.debug("git identity: temporary state file not removed")


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
    save()
    return resolved


def clear_cache() -> None:
    """Forget the resolved repositories and the state file binding (used by the tests)."""
    _cache.clear()
    _persisted.clear()
    _State.path = None
    _State.dirty = False
