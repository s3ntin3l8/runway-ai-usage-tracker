"""Chunked bulk UPDATE/DELETE so a repair never holds SQLite's writer lock
for more than one batch — the Data Health apply gate requires each write
transaction to land in roughly 2s, and a handful of these repairs (retagging
tens of thousands of legacy-provider events, reassigning a stray account's
full event history) would otherwise do it in one shot.

Batches advance over a monotonic `id` cursor rather than re-running `where`
against the post-update table: `where` often names a column the update
itself changes (a retag's `provider_id`), which is exactly what lets a
finished row fall out of scope — but a caller whose update doesn't happen to
touch a `where`-referenced column (or a delete, which removes the row
outright and makes this moot) must not turn into an infinite loop re-matching
the same rows forever. The cursor makes progress guaranteed regardless: each
batch only looks at strictly-greater ids than the last one committed, so a
crash mid-run leaves only committed batches applied, and calling again with
the same arguments resumes for whatever the (still column-narrowing) `where`
has left, or is a safe no-op once nothing remains.

The cursor is always `id`-based — `where` may reference any column, but
progress is tracked purely by ascending `id`, not by any column named in
`where`. A row inserted mid-run with an `id` less than or equal to the
current cursor will never be picked up by that run (it's "behind" the
cursor); this only matters for a `where` some future caller adds that
targets rows expected to arrive concurrently with a lower id than already-
processed ones, which none of the current callers do.

Transaction contract:
Each batch releases its read lock immediately (`session.commit()` inside
`_next_batch_ids`) before initiating the write transaction. In SQLite WAL
mode with deferred transactions, executing `SELECT` followed by `UPDATE` in
the same transaction causes a lock escalation (`SHARED` -> `RESERVED`) that
deadlocks under concurrent writes and fails with `database is locked` in 0.00s
(ignoring `busy_timeout`). Separating read and write transactions ensures the
write starts directly in write mode (`UNLOCKED` -> `RESERVED`), allowing
SQLite's `busy_timeout` to wait safely.

Because `_next_batch_ids` commits the session, any pending work staged by the
caller before invoking `chunked_update`/`chunked_delete` is committed as well,
and an error retry's `session.rollback()` will discard uncommitted changes.
Callers must therefore commit any preceding modifications before entering.
Finally, `values` must be idempotent, since a lock conflict during commit may
retry the batch execution.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, col, delete, select, update

logger = logging.getLogger(__name__)

_DEFAULT_BATCH = 5000
_MAX_RETRIES = 5
_INITIAL_BACKOFF = 0.05


def _is_sqlite_locked_or_busy(exc: OperationalError) -> bool:
    """True if `exc` represents SQLite SQLITE_BUSY or SQLITE_LOCKED contention.

    Inspects `exc.orig` for the driver-level error name/code first to prevent false
    positives from bound parameters containing the words 'locked' or 'busy'.
    """
    orig = getattr(exc, "orig", None)
    if orig is not None:
        errorname = getattr(orig, "sqlite_errorname", None)
        if errorname in ("SQLITE_BUSY", "SQLITE_LOCKED"):
            return True
        errorcode = getattr(orig, "sqlite_errorcode", None)
        if errorcode in (5, 6):  # SQLITE_BUSY=5, SQLITE_LOCKED=6
            return True
        msg = str(orig).lower()
        if "database is locked" in msg or "database is busy" in msg:
            return True
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def _retry_on_lock[T](
    fn: Callable[[], T],
    *,
    session: Session,
    action_desc: str,
    max_retries: int,
    initial_backoff: float,
) -> T:
    for attempt in range(max_retries):
        try:
            return fn()
        except OperationalError as exc:
            if _is_sqlite_locked_or_busy(exc) and attempt < max_retries - 1:
                session.rollback()
                delay = initial_backoff * (2**attempt) + random.uniform(0.01, 0.05)
                logger.warning(
                    "Database locked during %s (attempt %d/%d); retrying in %.2fs",
                    action_desc,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                time.sleep(delay)
                continue
            raise
    raise AssertionError("unreachable: retry loop either returns or re-raises")


def _next_batch_ids(
    session: Session,
    model: type[SQLModel],
    where: Sequence[ColumnElement[bool]],
    cursor_id: int,
    batch_size: int,
    *,
    max_retries: int = _MAX_RETRIES,
    initial_backoff: float = _INITIAL_BACKOFF,
) -> list[int]:
    id_col = col(model.id)  # type: ignore[attr-defined]
    stmt = (
        select(model.id).where(*where, id_col > cursor_id).order_by(id_col).limit(batch_size)  # type: ignore[attr-defined]
    )

    def _read() -> list[int]:
        rows = [row[0] for row in session.execute(stmt)]
        session.commit()
        return rows

    return _retry_on_lock(
        _read,
        session=session,
        action_desc=f"batch ID lookup of {model.__name__}",
        max_retries=max_retries,
        initial_backoff=initial_backoff,
    )


def _execute_with_retry(
    session: Session,
    statement: Any,
    *,
    action_desc: str,
    max_retries: int = _MAX_RETRIES,
    initial_backoff: float = _INITIAL_BACKOFF,
) -> None:
    def _write() -> None:
        session.exec(statement)
        session.commit()

    _retry_on_lock(
        _write,
        session=session,
        action_desc=action_desc,
        max_retries=max_retries,
        initial_backoff=initial_backoff,
    )


def chunked_update(
    session: Session,
    model: type[SQLModel],
    where: Sequence[ColumnElement[bool]],
    values: dict[str, Any],
    *,
    batch_size: int = _DEFAULT_BATCH,
    max_retries: int = _MAX_RETRIES,
    initial_backoff: float = _INITIAL_BACKOFF,
) -> int:
    """UPDATE `model` rows matching `where`, `batch_size` at a time."""
    id_col = col(model.id)  # type: ignore[attr-defined]
    cursor = 0
    total = 0
    while True:
        ids = _next_batch_ids(
            session,
            model,
            where,
            cursor,
            batch_size,
            max_retries=max_retries,
            initial_backoff=initial_backoff,
        )
        if not ids:
            break
        _execute_with_retry(
            session,
            update(model).where(id_col.in_(ids)).values(**values),  # type: ignore[call-overload]
            action_desc=f"chunked update of {model.__name__}",
            max_retries=max_retries,
            initial_backoff=initial_backoff,
        )
        total += len(ids)
        cursor = ids[-1]
        if len(ids) < batch_size:
            break
    return total


def chunked_delete(
    session: Session,
    model: type[SQLModel],
    where: Sequence[ColumnElement[bool]],
    *,
    batch_size: int = _DEFAULT_BATCH,
    max_retries: int = _MAX_RETRIES,
    initial_backoff: float = _INITIAL_BACKOFF,
) -> int:
    """DELETE `model` rows matching `where`, `batch_size` at a time."""
    id_col = col(model.id)  # type: ignore[attr-defined]
    cursor = 0
    total = 0
    while True:
        ids = _next_batch_ids(
            session,
            model,
            where,
            cursor,
            batch_size,
            max_retries=max_retries,
            initial_backoff=initial_backoff,
        )
        if not ids:
            break
        _execute_with_retry(
            session,
            delete(model).where(id_col.in_(ids)),  # type: ignore[call-overload]
            action_desc=f"chunked delete of {model.__name__}",
            max_retries=max_retries,
            initial_backoff=initial_backoff,
        )
        total += len(ids)
        cursor = ids[-1]
        if len(ids) < batch_size:
            break
    return total
