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
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement
from sqlmodel import Session, SQLModel, col, delete, select, update

_DEFAULT_BATCH = 5000


def _next_batch_ids(
    session: Session,
    model: type[SQLModel],
    where: Sequence[ColumnElement[bool]],
    cursor_id: int,
    batch_size: int,
) -> list[int]:
    id_col = col(model.id)  # type: ignore[attr-defined]
    stmt = select(model.id).where(*where, id_col > cursor_id).order_by(id_col).limit(batch_size)  # type: ignore[attr-defined]
    return [row[0] for row in session.execute(stmt)]


def chunked_update(
    session: Session,
    model: type[SQLModel],
    where: Sequence[ColumnElement[bool]],
    values: dict[str, Any],
    *,
    batch_size: int = _DEFAULT_BATCH,
) -> int:
    """UPDATE `model` rows matching `where`, `batch_size` at a time."""
    id_col = col(model.id)  # type: ignore[attr-defined]
    cursor = 0
    total = 0
    while True:
        ids = _next_batch_ids(session, model, where, cursor, batch_size)
        if not ids:
            break
        session.exec(update(model).where(id_col.in_(ids)).values(**values))  # type: ignore[call-overload]
        session.commit()
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
) -> int:
    """DELETE `model` rows matching `where`, `batch_size` at a time."""
    id_col = col(model.id)  # type: ignore[attr-defined]
    cursor = 0
    total = 0
    while True:
        ids = _next_batch_ids(session, model, where, cursor, batch_size)
        if not ids:
            break
        session.exec(delete(model).where(id_col.in_(ids)))  # type: ignore[call-overload]
        session.commit()
        total += len(ids)
        cursor = ids[-1]
        if len(ids) < batch_size:
            break
    return total
