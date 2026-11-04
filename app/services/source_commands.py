"""Durable device/source command queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func
from sqlmodel import Session, col, select

from app.models.db import SidecarSourceCommand
from app.services.source_controls_rev import bump, reserve_source_writes, state
from scripts.sidecar_pkg.source_catalog import control_catalog as catalog


def queue_command(
    session: Session, device_id: str, source_id: str, action: str
) -> SidecarSourceCommand:
    reserve_source_writes(session)
    row = state(session, device_id, source_id)
    if row is None or action not in {"pause", "resume", "revoke", "request_setup", "dismiss"}:
        raise ValueError("Unknown scraper or action")
    descriptor = catalog().get(source_id)
    if descriptor and descriptor.platforms == ("win32",) and action in {"request_setup", "dismiss"}:
        raise ValueError("Source has no Keychain setup or announcement")
    if action == "resume" and (not row.local_read or row.generation <= row.revoked_through):
        raise ValueError("Resume requires local consent")
    command = SidecarSourceCommand(
        device_id=device_id, source_id=source_id, generation=row.generation, action=action
    )
    if action == "pause":
        row.remote_paused = True
        bump(session)
    elif action == "revoke":
        row.revoked_through = max(row.revoked_through, row.generation)
        _withdraw(session, device_id, source_id)
    elif action == "dismiss":
        descriptor = catalog().get(source_id)
        row.dismissed_revision = descriptor.revision if descriptor else row.revision
    session.add(row)
    session.add(command)
    session.flush()
    return command


def pending_commands(session: Session, device_id: str) -> list[dict[str, Any]]:
    rows = session.exec(
        select(SidecarSourceCommand)
        .where(
            SidecarSourceCommand.device_id == device_id,
            col(SidecarSourceCommand.acknowledged).is_(None),
        )
        .order_by(col(SidecarSourceCommand.id))
        .limit(256)
    ).all()  # noqa: E711
    return [
        {
            "id": row.id,
            "source_id": row.source_id,
            "generation": row.generation,
            "action": row.action,
        }
        for row in rows
    ]


def acknowledge(session: Session, device_id: str, command_id: int, status: str) -> None:
    reserve_source_writes(session)
    command = session.get(SidecarSourceCommand, command_id)
    if command is None and status in {"applied", "denied", "superseded"}:
        # A repeated ack may outlive retention. Missing commands have no effect.
        return
    if (
        command is None
        or command.device_id != device_id
        or status not in {"applied", "denied", "superseded"}
    ):
        raise ValueError("Unknown command")
    if command.acknowledged is not None:
        return
    # A later administrative action always wins over a delayed older resume.
    newer = session.exec(
        select(SidecarSourceCommand).where(
            SidecarSourceCommand.device_id == device_id,
            SidecarSourceCommand.source_id == command.source_id,
            col(SidecarSourceCommand.id) > command_id,
        )
    ).first()
    row = state(session, device_id, command.source_id)
    if (
        command.action == "resume"
        and status == "applied"
        and newer is None
        and row is not None
        and row.local_read
        and row.generation == command.generation
        and row.generation > row.revoked_through
    ):
        row.remote_paused = False
        session.add(row)
        bump(session)
    command.acknowledged = status
    session.add(command)
    session.flush()


def prune_commands(session: Session, device_id: str) -> None:
    """Retain pending commands and each source's latest ordering barrier.

    Older acknowledged commands are useful for diagnostics for 30 days. Keeping
    the highest ID also prevents SQLite from reusing a command ID after cleanup.
    """
    reserve_source_writes(session)
    latest = (
        select(func.max(SidecarSourceCommand.id))
        .where(SidecarSourceCommand.device_id == device_id)
        .group_by(SidecarSourceCommand.source_id)
    )
    session.execute(
        delete(SidecarSourceCommand).where(
            col(SidecarSourceCommand.device_id) == device_id,
            col(SidecarSourceCommand.acknowledged).is_not(None),
            col(SidecarSourceCommand.issued_at) < datetime.now(UTC) - timedelta(days=30),
            col(SidecarSourceCommand.id).not_in(latest),
        )
    )


# Bound after the command functions are defined. Importing source_controls at
# module top cycles: it re-exports this module once its own primitives exist.
from app.services.source_controls import _withdraw
