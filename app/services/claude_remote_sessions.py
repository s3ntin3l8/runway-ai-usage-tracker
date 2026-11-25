"""Remote Cowork session hints: ids a Mac's sidecar found, kept so the cloud collector can fetch them.

Claude Desktop's remote Cowork sessions run in Anthropic's cloud and leave no local transcript.
``GET /v1/code/sessions`` and every other list leave them out, but each one can be fetched by id;
the sidecar reads the ids from Claude Desktop's ``remote-session-spaces.json`` and sends them here,
and the cloud collector finds the rest (also those started on the phone or the web) in the account's
claude.ai conversation list (``discover_from_conversations``).
The id itself is stored encrypted and never logged; the digest is the same one-way stand-in the
cloud checkpoints and events use.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlmodel import Session, col, select

from app.core.db import engine
from app.core.encryption import DecryptionError, encryption_service
from app.models.db import ClaudeRemoteSessionHint
from app.models.schemas_ingest import RemoteSessionHintIn
from app.services.account_identity import canonical_account_id
from app.services.claude_cloud_events import session_digest
from app.services.claude_verification import claude_get
from app.services.collectors.anthropic_console import ConsoleAPIError

# Mac-bound remote Cowork sessions are ``ccs``; ones started on the phone or the web run on the
# cloud runner (``anthropic_cloud``). The list leaves out both; the product tag tells them apart
# from ordinary cloud-runner sessions.
ENVIRONMENT_KINDS = frozenset({"ccs", "anthropic_cloud"})
REMOTE_COWORK_TAG = "product:cowork-remote"
# claude.ai links each Cowork session to a chat conversation; its list is the only one that names
# the sessions started on another device (#1397).
DISCOVERY_SOURCE = "claude.ai"
DISCOVERY_INTERVAL = 3600.0
DISCOVERY_PAGE = 100
DISCOVERY_MAX_PAGES = 20
_WORKSPACE_ID = re.compile(r"(?:session|cse)_(01[A-Za-z0-9]{22})")
# When each account last listed its conversations (process-local; a restart lists again once).
_LAST_DISCOVERY: dict[str, float] = {}
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
        row.get("environment_kind") in ENVIRONMENT_KINDS
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


def discovery_due(account: str, now: float | None = None) -> bool:
    stamp = time.time() if now is None else now
    return stamp - _LAST_DISCOVERY.get(account, 0.0) >= DISCOVERY_INTERVAL


async def discover_from_conversations(
    client: httpx.AsyncClient, cookie: str, account: str, org_uuid: str, *, pause: float = 0.0
) -> tuple[list[str], str]:
    """Remote Cowork session ids from the account's claude.ai conversations, and an outcome label.

    The cookie must belong to ``account`` (``/api/account`` is checked) and only the verified
    organization is listed, so a cookie of another person never feeds this account's hints. Only
    ``workspace_session_id`` is read from a row; titles and messages are never looked at. The labels
    are fixed strings, safe to log.
    """
    _LAST_DISCOVERY[account] = time.time()
    try:
        holder = await claude_get(client, "anthropic", "/api/account", cookie)
        email = holder.get("email_address") if isinstance(holder, dict) else None
        if not isinstance(email, str) or canonical_account_id(email) != account:
            return [], "cookie_other_account"
        ids: list[str] = []
        for page in range(DISCOVERY_MAX_PAGES):
            if page and pause:
                await asyncio.sleep(pause)
            body = await claude_get(
                client,
                "anthropic",
                f"/api/organizations/{org_uuid}/chat_conversations_v2"
                f"?limit={DISCOVERY_PAGE}&offset={page * DISCOVERY_PAGE}",
                cookie,
            )
            rows = (
                body
                if isinstance(body, list)
                else body.get("data")
                if isinstance(body, dict)
                else None
            )
            if not isinstance(rows, list):
                return ids, "unsupported_contract"
            for row in rows:
                raw = row.get("workspace_session_id") if isinstance(row, dict) else None
                match = _WORKSPACE_ID.fullmatch(raw) if isinstance(raw, str) else None
                if match and (sid := f"cse_{match.group(1)}") not in ids:
                    ids.append(sid)
            if len(rows) < DISCOVERY_PAGE:
                return ids, "ok"
        return ids, "max_pages"
    except ConsoleAPIError as exc:
        return [], exc.error_type or "unavailable"


def store_discovered(ids: list[str], account_uuid: str, org_uuid: str) -> int:
    """Hints for the discovered ids under the verified login's account and organization.

    Returns how many were new: re-listing a known session only re-stamps it.
    """
    if not ids:
        return 0
    with Session(engine) as session:
        known = set(
            session.exec(
                select(ClaudeRemoteSessionHint.session_digest).where(
                    col(ClaudeRemoteSessionHint.session_digest).in_(
                        [session_digest(sid) for sid in ids]
                    )
                )
            ).all()
        )
        upsert_hints(
            session,
            [
                RemoteSessionHintIn(session_id=sid, account_uuid=account_uuid, org_uuid=org_uuid)
                for sid in ids
            ],
            DISCOVERY_SOURCE,
        )
    return len({session_digest(sid) for sid in ids} - known)
