"""Writer reservation, revision bumps, and per-device source state reads.

These primitives stay free of the command queue and of contribution writes so
both sides can import them without a cycle.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session, select

from app.models.db import SidecarSourceState


def reserve_source_writes(session: Session) -> None:
    """Reserve SQLite's writer as the transaction's first statement, before reads.

    Repeated calls are safe once the transaction holds the writer reservation.
    A no-op UPDATE acquires the same writer reservation as BEGIN IMMEDIATE
    after deferred BEGIN, without nesting or restarting the caller's transaction.
    A prior WAL read snapshot may not upgrade: callers must reserve at entry.
    """
    session.execute(text("UPDATE sidecar_source_revision SET revision = revision WHERE id = 1"))


def state(session: Session, device_id: str, source_id: str) -> SidecarSourceState | None:
    return session.exec(
        select(SidecarSourceState)
        .where(SidecarSourceState.device_id == device_id, SidecarSourceState.source_id == source_id)
        .execution_options(populate_existing=True)
    ).first()


def bump(session: Session) -> None:
    session.execute(
        text("INSERT OR IGNORE INTO sidecar_source_revision (id, revision) VALUES (1, 0)")
    )
    session.execute(text("UPDATE sidecar_source_revision SET revision = revision + 1 WHERE id = 1"))
