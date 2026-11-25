"""Remote Cowork session hints: ids a Mac's sidecar found, kept so the cloud collector can fetch them.

Claude Desktop's remote Cowork sessions run in Anthropic's cloud and leave no local transcript.
``GET /v1/code/sessions`` and every other list leave them out, but each one can be fetched by id;
the sidecar reads the ids from Claude Desktop's ``remote-session-spaces.json`` and sends them here.
The id itself is stored encrypted and never logged; the digest is the same one-way stand-in the
cloud checkpoints and events use.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, col, select

from app.core.db import engine
from app.core.encryption import DecryptionError, encryption_service
from app.models.db import ClaudeRemoteSessionHint
from app.models.schemas_ingest import RemoteSessionHintIn
from app.services.claude_cloud_events import session_digest

ENVIRONMENT_KIND = "ccs"
REMOTE_COWORK_TAG = "product:cowork-remote"
# A hint already stored is re-stamped at most this often, so a sidecar that sends its list on every
# heartbeat does not turn each heartbeat into a write.
_TOUCH_INTERVAL = 3600.0
SETTLED_AFTER = 7 * 86400.0
SETTLED_RECHECK = 86400.0


def is_remote_cowork(row: dict[str, Any]) -> bool:
    """Whether a session is a remote Cowork session: both its kind and its product tag must say so.

    A hint only points at an id; it never decides what is read. Anything else (``bridge``, a local
    session mirrored to claude.ai and already scraped locally) is left alone.
    """
    tags = row.get("tags")
    return (
        row.get("environment_kind") == ENVIRONMENT_KIND
        and isinstance(tags, list)
        and REMOTE_COWORK_TAG in tags
    )


def upsert_hints(
    session: Session,
    hints: Iterable[RemoteSessionHintIn],
    sidecar_id: str | None,
    now: float | None = None,
) -> int:
    """Store new hints and re-stamp stale ones; returns how many rows were written.

    The first report decides a session's account and organization: a session belongs to one
    account, so a later report that disagrees only re-stamps the hint and never moves it.
    """
    stamp = time.time() if now is None else now
    by_digest = {session_digest(hint.session_id): hint for hint in hints}
    if not by_digest:
        return 0
    # One query for the whole list: a sidecar sends it with every heartbeat.
    existing = {
        row.session_digest: row
        for row in session.exec(
            select(ClaudeRemoteSessionHint).where(
                col(ClaudeRemoteSessionHint.session_digest).in_(list(by_digest))
            )
        ).all()
    }
    written = 0
    for digest, hint in by_digest.items():
        row = existing.get(digest)
        if row is None:
            row = ClaudeRemoteSessionHint(
                session_digest=digest,
                session_id_encrypted=encryption_service.encrypt_json(
                    {"session_id": hint.session_id}
                ),
                account_uuid=hint.account_uuid,
                org_uuid=hint.org_uuid,
                sidecar_id=sidecar_id,
                first_seen=stamp,
                last_seen=stamp,
            )
        elif stamp - row.last_seen < _TOUCH_INTERVAL:
            continue
        else:
            row.last_seen = stamp
        session.add(row)
        written += 1
    if written:
        session.commit()
    return written


@dataclass(frozen=True)
class Hint:
    session_id: str
    digest: str


def _due(row: ClaudeRemoteSessionHint, now: float) -> bool:
    if row.last_checked <= 0:
        return True
    if row.last_event_at <= 0:
        # Fetched but never resolved (deleted, unreadable, not remote Cowork): back off like a
        # settled session instead of spending a request on it every cycle.
        return now - row.last_checked >= SETTLED_RECHECK
    if now - row.last_event_at < SETTLED_AFTER:
        return True
    # A session quiet for a week is only looked at once a day, in case it is resumed.
    return now - row.last_checked >= SETTLED_RECHECK


def load_hints(
    account_uuid: str, org_uuid: str, limit: int, now: float | None = None
) -> list[Hint]:
    """Hinted sessions of one account and organization that are due a fetch, longest unchecked first.

    The order rotates through every hint (each fetch moves it to the back), so no session is starved
    however many a Mac reports; settled sessions are rechecked slowly.
    """
    stamp = time.time() if now is None else now
    with Session(engine) as session:
        rows = session.exec(
            select(ClaudeRemoteSessionHint)
            .where(col(ClaudeRemoteSessionHint.account_uuid) == account_uuid)
            .where(col(ClaudeRemoteSessionHint.org_uuid) == org_uuid)
            .order_by(col(ClaudeRemoteSessionHint.last_checked), col(ClaudeRemoteSessionHint.id))
        ).all()
    hints: list[Hint] = []
    for row in rows:
        if len(hints) >= limit:
            break
        if not _due(row, stamp):
            continue
        try:
            value = encryption_service.decrypt_json(row.session_id_encrypted)
        except DecryptionError:
            continue
        sid = value.get("session_id") if isinstance(value, dict) else None
        if isinstance(sid, str) and session_digest(sid) == row.session_digest:
            hints.append(Hint(sid, row.session_digest))
    return hints


def mark_checked(checked: dict[str, float], now: float | None = None) -> None:
    """Record a fetch per digest with the session's ``last_event_at`` (0 when unknown)."""
    if not checked:
        return
    stamp = time.time() if now is None else now
    with Session(engine) as session:
        rows = session.exec(
            select(ClaudeRemoteSessionHint).where(
                col(ClaudeRemoteSessionHint.session_digest).in_(list(checked))
            )
        ).all()
        for row in rows:
            row.last_checked = stamp
            if checked[row.session_digest] > 0:
                row.last_event_at = checked[row.session_digest]
            session.add(row)
        session.commit()
