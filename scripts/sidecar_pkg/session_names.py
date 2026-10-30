"""Session names the sidecar reports next to its usage events.

A session name is the title a tool shows for a session (a launcher's name, a
generated title). It is free text and may be sensitive, so nothing in this
module ever logs one, and the state file holds only hashes.

Contract for an extractor or collector that has names to report:

* append one record per name to the cycle's ``session_names`` list::

      {"provider_id": ..., "session_id": ..., "source_app": ..., "name": ..., "kind": "agent" | "generated"}

  ``provider_id`` is the id the sidecar stamps on that session's events (the
  server does not derive it); ``session_id`` is the id the events carry.
* register the collector in ``scripts/sidecar.py::_SESSION_NAME_COLLECTORS``.

The rest is handled here: the last record per (provider_id, session_id, kind)
wins, only names that changed since the last *accepted* push are sent (at most
``MAX_PER_PUSH`` per request, the server's cap), and the sent hashes are saved
by :func:`commit` once the server accepted the push. A failed push commits
nothing, so the same names go out again next cycle.

A collector that skips unchanged inputs (the Claude pass skips transcripts
whose size and mtime are unchanged) can register what it scanned with
:func:`stage_files`; the signatures are saved together with the names, so a
skipped input never hides a name that was not delivered yet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Any

#: The server drops anything beyond this many names per request.
MAX_PER_PUSH = 2000

#: A name longer than this is cut before it is hashed and sent.
MAX_NAME_LENGTH = 500

#: Entries kept in the state file. Oldest go first; an evicted entry only means
#: its name is sent once more.
_STATE_LIMIT = 20000

_STATE_VERSION = 1
_KINDS = frozenset({"agent", "generated"})

#: key digest -> name digest, for names the server accepted.
_sent: dict[str, str] = {}
#: path digest -> "<mtime_ns>:<size>", for inputs scanned before the last accepted push.
_files: dict[str, str] = {}
#: Staged by collectors this cycle; moved to ``_files`` by :func:`commit`.
_staged_files: dict[str, str] = {}


class _State:
    path: Path | None = None
    dirty: bool = False
    loaded: bool = False


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:24]


def _record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (record["provider_id"], record["session_id"], record["kind"])


def _valid(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    for field in ("provider_id", "session_id", "name"):
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    return record.get("kind") in _KINDS


def make_record(
    provider_id: str, session_id: str, name: str, kind: str, source_app: str | None = None
) -> dict[str, Any]:
    """A record in the shape the ingest payload carries."""
    return {
        "provider_id": provider_id,
        "session_id": session_id,
        "source_app": source_app,
        "name": name[:MAX_NAME_LENGTH],
        "kind": kind,
    }


def configure(path: Path | None) -> None:
    """Load the state from `path` (once per path). Missing or corrupt means empty."""
    if _State.loaded and path == _State.path:
        return
    _sent.clear()
    _files.clear()
    _staged_files.clear()
    _State.path = path
    _State.dirty = False
    _State.loaded = True
    if path is None:
        return
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
        ):
            # A file another user could have planted or edited is not trusted;
            # the next save replaces it with a private one.
            logging.debug("session names: state file not trusted")
            _State.dirty = True
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict) or raw.get("version") != _STATE_VERSION:
        return
    for target, field in ((_sent, "names"), (_files, "files")):
        entries = raw.get(field)
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            if isinstance(key, str) and isinstance(value, str) and key and value:
                target[key] = value
        while len(target) > _STATE_LIMIT:
            target.pop(next(iter(target)))


def save() -> None:
    """Write the state if it changed. Best-effort and atomic, owner-only."""
    path = _State.path
    if path is None or not _State.dirty:
        return
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"version": _STATE_VERSION, "names": _sent, "files": _files}
        temporary.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            logging.debug("session names: state file permissions not narrowed")
        os.replace(temporary, path)
        _State.dirty = False
    except OSError as exc:
        logging.debug("session names: state not saved (%s)", type(exc).__name__)
        try:
            temporary.unlink()
        except OSError:
            logging.debug("session names: temporary state file not removed")


def begin_cycle() -> None:
    """Forget what the previous cycle staged (it was never committed)."""
    _staged_files.clear()


def file_signature(path: Path) -> str | None:
    """`<mtime_ns>:<size>` of a file, or None when it cannot be read."""
    try:
        info = path.stat()
    except OSError:
        return None
    return f"{info.st_mtime_ns}:{info.st_size}"


def file_unchanged(path: Path) -> bool:
    """True when the file's size and mtime match the last accepted push's scan."""
    signature = file_signature(path)
    return signature is not None and _files.get(_digest("file", str(path))) == signature


def stage_files(scanned: dict[Path, str]) -> None:
    """Remember scanned inputs; they are saved by :func:`commit`, not before."""
    for path, signature in scanned.items():
        _staged_files[_digest("file", str(path))] = signature


def select_for_push(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records to send now: the last per key, changed since the last accepted
    push, at most ``MAX_PER_PUSH``."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        if _valid(record):
            latest[_record_key(record)] = record
    changed: list[dict[str, Any]] = []
    for key, record in latest.items():
        name = record["name"][:MAX_NAME_LENGTH]
        if _sent.get(_digest(*key)) != _digest(name):
            changed.append({**record, "name": name})
    if len(changed) > MAX_PER_PUSH:
        # The rest goes next cycle. Inputs skipped by signature would hide it.
        changed = changed[:MAX_PER_PUSH]
        _staged_files.clear()
    return changed


def commit(sent: list[dict[str, Any]]) -> None:
    """Record names the server accepted, and the inputs scanned with them."""
    for record in sent:
        if not _valid(record):
            continue
        key = _digest(*_record_key(record))
        _sent.pop(key, None)
        _sent[key] = _digest(record["name"][:MAX_NAME_LENGTH])
        _State.dirty = True
    for key, signature in _staged_files.items():
        if _files.get(key) != signature:
            _files.pop(key, None)
            _files[key] = signature
            _State.dirty = True
    _staged_files.clear()
    for target in (_sent, _files):
        while len(target) > _STATE_LIMIT:
            target.pop(next(iter(target)))
            _State.dirty = True
    save()


def discard() -> None:
    """The push did not land: commit nothing."""
    _staged_files.clear()


def clear_state() -> None:
    """Forget everything, including the state file binding (used by the tests)."""
    _sent.clear()
    _files.clear()
    _staged_files.clear()
    _State.path = None
    _State.dirty = False
    _State.loaded = False
