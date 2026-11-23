"""Pre-write SQLite snapshot helper for offline maintenance scripts.

Takes a consistent snapshot via SQLite's online backup API before a script
mutates the database, enforces a 1.2x free-disk-space guard, and prunes
older snapshots for the same script after a successful run.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import re
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.config import get_platform_config_dir, settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

DEFAULT_KEEP_SNAPSHOTS = 2
DISK_SPACE_MULTIPLIER = 1.2
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {parsed}")
    return parsed


def add_snapshot_args(parser: argparse.ArgumentParser) -> None:
    """Register standard snapshot CLI options on a script's ArgumentParser."""
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip taking a pre-write database snapshot.",
    )
    parser.add_argument(
        "--keep-snapshots",
        type=_non_negative_int,
        default=DEFAULT_KEEP_SNAPSHOTS,
        metavar="N",
        help=(
            f"Keep the newest N snapshots for this script after a successful run "
            f"(default: {DEFAULT_KEEP_SNAPSHOTS})."
        ),
    )


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    value = float(num_bytes)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024.0
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}"
    return f"{num_bytes} B"


def _resolve_db_path(
    *,
    engine: Engine | None = None,
    db_path: Path | str | None = None,
) -> Path | None:
    if db_path is not None:
        raw = str(db_path)
    elif engine is not None:
        raw = engine.url.database or ""
    else:
        raw = settings.DATABASE_PATH

    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def _resolve_backups_dir(config_dir: Path | str | None = None) -> Path:
    base = Path(config_dir) if config_dir is not None else Path(get_platform_config_dir("runway"))
    return base / "backups"


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _database_size_bytes(db_path: Path) -> int:
    total = db_path.stat().st_size
    wal_path = Path(f"{db_path}-wal")
    if wal_path.exists():
        total += wal_path.stat().st_size
    return total


def _ensure_disk_space(backups_dir: Path, db_size: int) -> None:
    required = math.ceil(db_size * DISK_SPACE_MULTIPLIER)
    if required <= 0:
        return
    probe_dir = _existing_ancestor(backups_dir)
    free = shutil.disk_usage(probe_dir).free
    if free < required:
        print(
            f"Error: insufficient disk space for database snapshot in {backups_dir}: "
            f"need {_format_size(required)} ({required:,} bytes, 1.2x of "
            f"{_format_size(db_size)}), only {_format_size(free)} ({free:,} bytes) available.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _snapshot_pattern(script_name: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(script_name)}-(\d{{8}}T\d{{6}}(?:\d{{6}})?Z)\.db$")


def _sort_key(ts: str, name: str) -> tuple[str, str]:
    # Normalize 14-digit (%Y%m%dT%H%M%SZ) and 20-digit (%Y%m%dT%H%M%S%fZ) timestamps
    # so older second-precision filenames always sort before microsecond-precision ones
    # from the same second.
    return (ts.removesuffix("Z").ljust(21, "0"), name)


def take_snapshot(
    script_name: str,
    *,
    enabled: bool = True,
    engine: Engine | None = None,
    db_path: Path | str | None = None,
    config_dir: Path | str | None = None,
) -> Path | None:
    """Take a consistent SQLite online backup before a script writes.

    Returns the snapshot path, or ``None`` when ``enabled`` is False or the
    target database is in-memory or does not exist yet. Exits non-zero on
    insufficient disk space or backup failure.
    """
    if not enabled:
        return None

    resolved_db = _resolve_db_path(engine=engine, db_path=db_path)
    if resolved_db is None or not resolved_db.exists():
        return None

    backups_dir = _resolve_backups_dir(config_dir=config_dir)
    db_size = _database_size_bytes(resolved_db)
    _ensure_disk_space(backups_dir, db_size)

    backups_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(backups_dir, 0o700)

    timestamp = datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)
    dest_path = backups_dir / f"{script_name}-{timestamp}.db"

    src_conn: sqlite3.Connection | None = None
    dst_conn: sqlite3.Connection | None = None
    try:
        src_conn = sqlite3.connect(str(resolved_db), timeout=30.0)
        dst_conn = sqlite3.connect(str(dest_path))
        src_conn.backup(dst_conn)
    except Exception as exc:
        with contextlib.suppress(OSError):
            if dest_path.exists():
                dest_path.unlink()
        print(
            f"Error: failed to snapshot database {resolved_db} to {dest_path}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    finally:
        if dst_conn is not None:
            dst_conn.close()
        if src_conn is not None:
            src_conn.close()

    snap_size = dest_path.stat().st_size
    print(f"Snapshot written to {dest_path} ({_format_size(snap_size)})", flush=True)
    return dest_path


def prune_snapshots(
    backups_dir: Path,
    script_name: str,
    *,
    keep: int = DEFAULT_KEEP_SNAPSHOTS,
    protect: Path | None = None,
) -> list[Path]:
    """Delete older snapshots for ``script_name``, keeping the newest ``keep``.

    Never deletes files that do not match ``<script_name>-<UTC timestamp>.db``,
    and never deletes ``protect`` (the snapshot taken in the current run).
    Returns the list of deleted paths.
    """
    if not backups_dir.is_dir():
        return []

    pattern = _snapshot_pattern(script_name)
    protect_resolved = protect.resolve() if protect is not None else None

    candidates: list[tuple[tuple[str, str], Path]] = []
    for entry in backups_dir.iterdir():
        if not entry.is_file():
            continue
        match = pattern.match(entry.name)
        if not match:
            continue
        if protect_resolved is not None and entry.resolve() == protect_resolved:
            continue
        candidates.append((_sort_key(match.group(1), entry.name), entry))

    candidates.sort(key=lambda item: item[0], reverse=True)
    keep_older = max(0, keep - 1) if protect_resolved is not None else max(0, keep)

    deleted: list[Path] = []
    for _, path in candidates[keep_older:]:
        path.unlink()
        deleted.append(path)
    return deleted
