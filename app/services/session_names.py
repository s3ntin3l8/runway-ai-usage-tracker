"""Session names: store the latest display name per (provider, session, kind).

Names are free text and may contain sensitive wording. Nothing in this module
logs a name or puts one in an exception message; counts only.
"""

import logging
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import tuple_
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.models.db import SessionName
from app.models.schemas import SessionNameIn

logger = logging.getLogger(__name__)

#: Rows touched per statement; SQLite caps bound variables per statement.
_CHUNK = 300
_LOCK_RETRIES = 3
_LOCK_BACKOFF_SECONDS = 0.2

#: ``agent`` (user/launcher) wins over ``generated`` (tool) when both exist.
_KIND_PRECEDENCE = ("agent", "generated")


def _latest(names: Iterable[SessionNameIn]) -> dict[tuple[str, str, str], SessionNameIn]:
    """Last entry wins per key; empty names are skipped."""
    latest: dict[tuple[str, str, str], SessionNameIn] = {}
    for entry in names:
        if not entry.name or not entry.session_id or not entry.provider_id:
            continue
        latest[(entry.provider_id, entry.session_id, entry.kind)] = entry
    return latest


def _upsert_once(session: Session, latest: dict[tuple[str, str, str], SessionNameIn]) -> int:
    keys = list(latest)
    changed = 0
    now = datetime.now(UTC)
    for start in range(0, len(keys), _CHUNK):
        chunk = keys[start : start + _CHUNK]
        existing = {
            (row.provider_id, row.session_id, row.kind): row
            for row in session.exec(
                select(SessionName).where(
                    tuple_(SessionName.provider_id, SessionName.session_id, SessionName.kind).in_(
                        chunk
                    )
                )
            ).all()
        }
        for key in chunk:
            entry = latest[key]
            row = existing.get(key)
            if row is None:
                session.add(
                    SessionName(
                        provider_id=entry.provider_id,
                        session_id=entry.session_id,
                        source_app=entry.source_app,
                        name=entry.name,
                        kind=entry.kind,
                        updated_at=now,
                    )
                )
                changed += 1
            elif row.name != entry.name or (
                entry.source_app and row.source_app != entry.source_app
            ):
                row.name = entry.name
                if entry.source_app:
                    row.source_app = entry.source_app
                row.updated_at = now
                session.add(row)
                changed += 1
    return changed


def upsert_session_names(session: Session, names: Sequence[SessionNameIn] | None) -> int:
    """Store the latest name per (provider, session, kind); return rows written.

    An unchanged name writes nothing (``updated_at`` stays put). A locked
    database is retried a few times before the error propagates.
    """
    if not names:
        return 0
    latest = _latest(names)
    if not latest:
        return 0
    for attempt in range(_LOCK_RETRIES + 1):
        try:
            changed = _upsert_once(session, latest)
            session.commit()
            return changed
        except OperationalError:
            session.rollback()
            if attempt == _LOCK_RETRIES:
                raise
            time.sleep(_LOCK_BACKOFF_SECONDS * (attempt + 1))
    return 0  # pragma: no cover


def resolve_names(session: Session, provider_id: str, session_ids: Sequence[str]) -> dict[str, str]:
    """session_id -> display name for the given ids (agent over generated).

    One indexed query over the ids passed in, so callers pass a page's ids,
    never the whole aggregate.
    """
    ids = [sid for sid in dict.fromkeys(session_ids) if sid]
    found: dict[str, dict[str, str]] = {}
    for start in range(0, len(ids), _CHUNK):
        rows = session.exec(
            select(SessionName).where(
                SessionName.provider_id == provider_id,
                SessionName.session_id.in_(ids[start : start + _CHUNK]),  # type: ignore[attr-defined]
            )
        ).all()
        for row in rows:
            found.setdefault(row.session_id, {})[row.kind] = row.name
    resolved: dict[str, str] = {}
    for sid, by_kind in found.items():
        for kind in _KIND_PRECEDENCE:
            if by_kind.get(kind):
                resolved[sid] = by_kind[kind]
                break
    return resolved
