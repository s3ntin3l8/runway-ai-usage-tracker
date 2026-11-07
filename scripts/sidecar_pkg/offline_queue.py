"""The sidecar's offline queue: bounded, plaintext-free retry storage for payloads.

Split out of ``scripts/sidecar.py``. Names tests patch on ``scripts.sidecar``
(``get_queue_dir``, ``ensure_dirs``, ``http_post_signed_with_retry``, ``os`` and
the helpers below) are read back from that module at call time, so a
``monkeypatch.setattr(sidecar, ...)`` still intercepts the moved code. This module
must not import ``scripts.sidecar`` at import time (circular import).
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import secrets
import stat
import sys
import threading
import time
import types
from collections.abc import Callable
from typing import Any, TextIO

_FACADE_NAMES = ("scripts.sidecar", "sidecar")


def _is_facade(module: Any) -> bool:
    return isinstance(module, types.ModuleType) and hasattr(module, "get_queue_dir")


def _facade() -> Any:
    """The ``sidecar`` module whose code (or test) called into the queue.

    The sidecar can be loaded twice in one process, as ``scripts.sidecar`` and as
    a bare ``sidecar`` (tests put ``scripts/`` on ``sys.path``), and a test patches
    one copy. Both copies share this module, so the calling copy is found from the
    call stack; ``sys.modules`` is only the fallback. A caller whose globals hold
    both copies resolves to the first one found, so a test module must patch
    through a single import style (do not mix ``import sidecar`` and
    ``from scripts import sidecar``).
    """
    frame = sys._getframe(1)
    while frame is not None:
        scope = frame.f_globals
        if scope.get("__name__") != __name__:
            if scope.get("__name__") in (*_FACADE_NAMES, "__main__") and "get_queue_dir" in scope:
                return sys.modules[scope["__name__"]]
            for value in scope.values():
                if (
                    _is_facade(value)
                    and getattr(value, "__name__", "") in _FACADE_NAMES
                    and hasattr(value, "queue_flush")
                ):
                    return value
        frame = frame.f_back
    for name in (*_FACADE_NAMES, "__main__"):
        module = sys.modules.get(name)
        if _is_facade(module):
            return module
    raise RuntimeError("offline_queue must be used through scripts.sidecar, which is not loaded")


def _sidecar_attr(name: str) -> Any:
    """Read a moved name back from ``sidecar`` so a test's patch of its re-export applies."""
    return getattr(_facade(), name)


class _FacadeAttr:
    """Stand-in for a module-level name that resolves on ``sidecar`` per access."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(getattr(_facade(), self._name), attr)


# ``os`` is read through the facade so ``monkeypatch.setattr(sidecar, "os", ...)`` applies.
os = _FacadeAttr("os")


def get_queue_dir(*args: Any, **kwargs: Any) -> Any:
    return _facade().get_queue_dir(*args, **kwargs)


def ensure_dirs(*args: Any, **kwargs: Any) -> Any:
    return _facade().ensure_dirs(*args, **kwargs)


def http_post_signed_with_retry(*args: Any, **kwargs: Any) -> Any:
    return _facade().http_post_signed_with_retry(*args, **kwargs)


def _open_directory(*args: Any, **kwargs: Any) -> Any:
    return _facade()._open_directory(*args, **kwargs)


def redact_log_text(*args: Any, **kwargs: Any) -> Any:
    return _facade().redact_log_text(*args, **kwargs)


def _secure_queue_dir() -> int:
    """Return an open queue directory descriptor, secured on Unix."""
    ensure_dirs()
    fd = _open_directory(get_queue_dir())
    if os.name != "nt":
        os.fchmod(fd, 0o700)
    return fd


def _open_queue_file(dir_fd: int, name: str, flags: int, mode: int = 0o600) -> int:
    """Open a regular queue file without following symlinks and secure it."""
    before = None
    if os.name != "nt":
        try:
            before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not flags & os.O_CREAT:
                raise
        if before is not None:
            if not stat.S_ISREG(before.st_mode):
                raise OSError(f"Queue entry is not a regular file: {name}")
            # chmod through the validated directory entry allows recovery of
            # a regular file whose mode was changed to 000, without following links.
            os.chmod(name, 0o600, dir_fd=dir_fd, follow_symlinks=False)
    nofollow = getattr(os, "O_NOFOLLOW", 0) if os.name != "nt" else 0
    fd = os.open(name, flags | nofollow, mode, dir_fd=dir_fd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"Queue entry is not a regular file: {name}")
        if before is not None and (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError(f"Queue entry changed during access: {name}")
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        return fd
    except Exception:
        os.close(fd)
        raise


def _queue_names(dir_fd: int) -> list[str]:
    return sorted(name for name in os.listdir(dir_fd) if name.endswith(".jsonl"))


def _unlink_queue_file(dir_fd: int, name: str) -> None:
    """Unlink only after reopening and validating the regular queue file."""
    fd = _sidecar_attr("_open_queue_file")(dir_fd, name, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (info.st_dev, info.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise OSError(f"Queue entry changed during deletion: {name}")
        os.unlink(name, dir_fd=dir_fd)
    finally:
        os.close(fd)


def _queue_limit_mb(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        limit = float(value)
    except OverflowError:
        return None
    return limit if math.isfinite(limit) and limit > 0 else None


# Cards that exist only to carry a credential to the server (never displayed).
# Mirrors `is_token_only` in app/api/endpoints/fleet_ingest.py.
_TOKEN_ONLY_UNITS = frozenset({"oauth", "api_key", "cookie"})


def strip_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    """Return *payload* without credential-carrying cards or raw log lines.

    The offline queue is plaintext on disk, so it must hold usage only. Credentials
    are re-sent on the next live cycle; replaying a stale one adds nothing.
    """
    stripped = dict(payload)
    # The predicate is the server's own token-only rule, which is also what feeds the
    # token cache, so a credential the server would act on is always removed here.
    if "metrics" in payload:
        stripped["metrics"] = [
            card
            for card in payload["metrics"] or []
            if not (
                isinstance(card, dict)
                and card.get("remaining") == "Token"
                and card.get("unit") in _sidecar_attr("_TOKEN_ONLY_UNITS")
            )
        ]
    if payload.get("last_log_lines"):
        stripped["last_log_lines"] = [redact_log_text(str(x)) for x in payload["last_log_lines"]]
    return stripped


def _sanitize_queue_line(line: str) -> str:
    """A queue line with its credentials removed (entries from older sidecars held them)."""
    try:
        entry = json.loads(line)
        entry["payload"] = _sidecar_attr("strip_credentials")(entry.get("payload") or {})
        return json.dumps(entry, separators=(",", ":"))
    except Exception:
        # Can't edit what can't be parsed; keep it, but make the retry visible.
        logging.warning("Queue line is not valid JSON; kept as is (it may hold credentials)")
        return line


def queue_push(payload: dict[str, Any], max_size_mb: float | None = None) -> bool:
    """Add payload to the bounded offline queue; return False when full.

    ``max_size_mb`` is the sidecar config's ``queue_max_size_mb`` key
    (default 10); an unset, non-positive or otherwise invalid value falls
    back to 10 so a bad config value can't silently disable the cap.

    Credentials are never queued (see ``strip_credentials``).
    """
    payload = _sidecar_attr("strip_credentials")(payload)

    if os.name == "nt":
        ensure_dirs()

    max_size_mb = _sidecar_attr("_queue_limit_mb")(max_size_mb) or 10.0

    # Create queue file for today
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    queue_file = get_queue_dir() / f"{today}.jsonl"

    entry = {"ts": int(time.time()), "payload": payload}
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    max_size_bytes = max_size_mb * 1024 * 1024

    if os.name == "nt":
        total_size = sum(f.stat().st_size for f in get_queue_dir().glob("*.jsonl"))
        if total_size + len(line.encode("utf-8")) > max_size_bytes:
            logging.error(
                "Offline queue reached its %s MB limit; retaining existing entries and "
                "skipping this payload; the next collection cycle will retry source data.",
                max_size_mb,
            )
            return False
        with open(queue_file, "a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    else:
        dir_fd = _sidecar_attr("_secure_queue_dir")()
        try:
            total_size = 0
            for name in _sidecar_attr("_queue_names")(dir_fd):
                fd = _sidecar_attr("_open_queue_file")(dir_fd, name, os.O_RDONLY)
                try:
                    total_size += os.fstat(fd).st_size
                finally:
                    os.close(fd)
            if total_size + len(line.encode("utf-8")) > max_size_bytes:
                logging.error(
                    "Offline queue reached its %s MB limit; retaining existing entries and "
                    "skipping this payload; the next collection cycle will retry source data.",
                    max_size_mb,
                )
                return False
            fd = _sidecar_attr("_open_queue_file")(
                dir_fd,
                queue_file.name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            )
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    logging.info(f"Queued payload for retry: {queue_file.name}")
    _sidecar_attr("queue_rotate")(max_size_mb)
    return True


def queue_rotate(max_size_mb: float | None = None, config: dict[str, Any] | None = None) -> None:
    """Report queue growth without deleting unacknowledged payloads.

    ``max_size_mb`` wins when given (matches ``queue_push``'s already-resolved
    limit, so both stay in agreement about what "full" means within one
    call). Otherwise falls back to ``config["queue_max_size_mb"]``, then 10.
    """
    queue_dir = get_queue_dir()
    if os.name == "nt" and not queue_dir.exists():
        return

    max_size_mb = (
        _sidecar_attr("_queue_limit_mb")(max_size_mb)
        or _sidecar_attr("_queue_limit_mb")((config or {}).get("queue_max_size_mb"))
        or 10.0
    )

    max_size_bytes = max_size_mb * 1024 * 1024

    if os.name == "nt":
        queue_files = sorted(queue_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        total_size = sum(f.stat().st_size for f in queue_files)
        if total_size > max_size_bytes:
            logging.error(
                "Offline queue is %d bytes; retaining all unacknowledged payloads", total_size
            )
        return

    try:
        dir_fd = _sidecar_attr("_secure_queue_dir")()
    except Exception as e:
        logging.error(f"Failed to access queue directory: {e}")
        return
    try:
        entries = []
        for name in _sidecar_attr("_queue_names")(dir_fd):
            try:
                fd = _sidecar_attr("_open_queue_file")(dir_fd, name, os.O_RDONLY)
                try:
                    info = os.fstat(fd)
                    entries.append((info.st_mtime, name, info.st_size))
                finally:
                    os.close(fd)
            except Exception as e:
                logging.error(f"Failed to secure queue file {name}: {e}")
        total_size = sum(size for _, _, size in entries)
        if total_size > max_size_bytes:
            logging.error(
                "Offline queue is %d bytes; retaining all unacknowledged payloads", total_size
            )
    finally:
        os.close(dir_fd)


def _replay_queue_lines(
    source: TextIO,
    retained: TextIO,
    target_url: str,
    api_key: str,
    stop_event: threading.Event | None,
    config: dict[str, Any] | None,
    on_sent: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[int, bool]:
    """Replay one entry at a time, streaming failures and the unprocessed suffix."""
    failed_count = 0
    interrupted = False
    for line in source:
        if not interrupted and stop_event and stop_event.is_set():
            logging.info("queue_flush: stop requested, aborting flush")
            interrupted = True
        line = line.strip()
        if not line:
            continue
        # A stop treats the current line as part of the unprocessed suffix.
        keep = interrupted
        if not interrupted:
            try:
                entry = json.loads(line)
                queued = entry.get("payload", {})
                payload = _sidecar_attr("strip_credentials")(queued)
                if payload != queued and not payload.get("metrics") and not payload.get("events"):
                    # Legacy credential-only entries have nothing useful to replay.
                    continue
                success, result, _ = http_post_signed_with_retry(
                    target_url, payload, api_key, stop_event=stop_event, config=config
                )
                events_failed = bool(
                    payload.get("events")
                    and isinstance(result, dict)
                    and result.get("events_error")
                )
                if success and not events_failed:
                    if on_sent is not None:
                        on_sent(payload)
                else:
                    keep = True
                    if success and events_failed:
                        logging.warning(
                            "Server reported an event-ingest failure; "
                            "keeping queued payload for retry"
                        )
            except json.JSONDecodeError:
                logging.error(f"Invalid JSON in queue file: {line[:100]}")
            except Exception as e:
                logging.error(f"Failed to send queued payload: {e}")
                keep = True
        if keep:
            # Keep writes outside the replay exception handler: a write failure
            # must abort publication, leaving the original queue untouched.
            retained.write(_sidecar_attr("_sanitize_queue_line")(line) + "\n")
            failed_count += 1
    return failed_count, interrupted


def _check_queue_snapshot(dir_fd: int, name: str, snapshot: os.stat_result) -> None:
    """Do not overwrite or unlink an entry replaced/appended during replay."""
    current = (
        os.stat(get_queue_dir() / name, follow_symlinks=False)
        if dir_fd < 0
        else os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    )
    if not stat.S_ISREG(current.st_mode) or (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != (snapshot.st_dev, snapshot.st_ino, snapshot.st_size, snapshot.st_mtime_ns):
        raise OSError(f"Queue entry changed during replay: {name}")


def queue_flush(
    api_url: str,
    api_key: str,
    stop_event: threading.Event | None = None,
    config: dict[str, Any] | None = None,
    on_upload: Callable[[int, int], None] | None = None,
) -> int:
    """Replay the queue with bounded memory and atomic replacement of retained entries."""
    queue_dir = get_queue_dir()
    dir_fd = -1
    if os.name == "nt" and not queue_dir.exists():
        return 0
    if os.name == "nt":
        queue_names = sorted(p.name for p in queue_dir.glob("*.jsonl"))
    else:
        try:
            dir_fd = _sidecar_attr("_secure_queue_dir")()
            queue_names = _sidecar_attr("_queue_names")(dir_fd)
        except Exception as e:
            logging.error(f"Failed to access queue directory: {e}")
            return 0

    count = 0

    def record_sent(payload: dict[str, Any]) -> None:
        # Count acknowledgements even if a later retention write/read fails.
        nonlocal count
        count += 1
        if on_upload is not None:
            on_upload(len(payload.get("metrics") or []), len(payload.get("events") or []))

    target_url = f"{api_url.rstrip('/')}/api/v1/fleet/ingest"
    try:
        for name in queue_names:
            if stop_event and stop_event.is_set():
                logging.info("queue_flush: stop requested, aborting flush")
                break
            temporary = f".{name}.{secrets.token_hex(8)}.tmp"
            temporary_created = False
            try:
                source_fd = (
                    os.open(queue_dir / name, os.O_RDONLY)
                    if dir_fd < 0
                    else _sidecar_attr("_open_queue_file")(dir_fd, name, os.O_RDONLY)
                )
                wrapped = False
                try:
                    source = os.fdopen(source_fd, encoding="utf-8")
                    wrapped = True
                finally:
                    # Failed wrapping leaves the raw descriptor owned here.
                    if not wrapped:
                        os.close(source_fd)
                with source:
                    snapshot = os.fstat(source.fileno())
                    # Random names and O_EXCL defend against leftovers from a
                    # prior interrupted run or another concurrent flusher.
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    fd = (
                        os.open(queue_dir / temporary, flags, 0o600)
                        if dir_fd < 0
                        else _sidecar_attr("_open_queue_file")(dir_fd, temporary, flags)
                    )
                    temporary_created = True
                    retained_wrapped = False
                    try:
                        retained = os.fdopen(fd, "w", encoding="utf-8")
                        retained_wrapped = True
                    finally:
                        # Close the unwrapped descriptor, even on interruption.
                        if not retained_wrapped:
                            os.close(fd)
                    with retained:
                        failed_count, interrupted = _sidecar_attr("_replay_queue_lines")(
                            source, retained, target_url, api_key, stop_event, config, record_sent
                        )
                        retained.flush()
                        os.fsync(retained.fileno())
                        temporary_snapshot = os.fstat(retained.fileno())
                # Exiting both stream contexts closes their FDs on POSIX and Windows.
                _sidecar_attr("_check_queue_snapshot")(dir_fd, name, snapshot)
                if failed_count:
                    _sidecar_attr("_check_queue_snapshot")(dir_fd, temporary, temporary_snapshot)
                    if dir_fd < 0:
                        os.replace(queue_dir / temporary, queue_dir / name)
                    else:
                        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                    temporary_created = False
                    logging.warning(f"Queue file has {failed_count} failed entries: {name}")
                else:
                    # The empty temp is only removed in finally, never published,
                    # so its contents need no snapshot check.
                    if dir_fd < 0:
                        (queue_dir / name).unlink()
                    else:
                        _sidecar_attr("_unlink_queue_file")(dir_fd, name)
                    logging.info(f"Queue file processed and removed: {name}")
                if interrupted:
                    break
            except Exception as e:
                logging.error(f"Failed to process queue file {name}: {e}")
            finally:
                if temporary_created:
                    try:
                        if dir_fd < 0:
                            (queue_dir / temporary).unlink()
                        else:
                            os.unlink(temporary, dir_fd=dir_fd)
                    except FileNotFoundError:
                        # Replacement already consumed the temporary file.
                        pass
                    except OSError as e:
                        logging.warning(f"Failed to remove queue temporary file: {e}")
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)
    return count
