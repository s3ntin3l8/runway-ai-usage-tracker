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
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement
from sqlmodel import Session, SQLModel, col, delete, select, update

from app.services.maintenance._retry import (
    _INITIAL_BACKOFF,
    _MAX_RETRIES,
    _retry_on_lock,
)

logger = logging.getLogger(__name__)

_DEFAULT_BATCH = 5000


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
    if max_retries <= 0:
        raise ValueError(f"max_retries must be positive, got {max_retries}")
    if initial_backoff < 0:
        raise ValueError(f"initial_backoff must be non-negative, got {initial_backoff}")
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
    if max_retries <= 0:
        raise ValueError(f"max_retries must be positive, got {max_retries}")
    if initial_backoff < 0:
        raise ValueError(f"initial_backoff must be non-negative, got {initial_backoff}")
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
