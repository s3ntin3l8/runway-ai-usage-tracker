"""Helper for recording admin-mutation events into `audit_log`.

Closes audit finding S7 + R9. Every successful state-changing call on
the admin surface (sidecar pause/resume/delete/patch and similar)
appends one row here, so an operator looking at "who paused this
sidecar last Tuesday?" has a primary source instead of guessing from
container stdout.

Designed to be call-site-quiet: one line per mutation, fire-and-forget
semantics. Failures to record an audit row are logged but never block
the mutation itself — the source-of-truth state has already been
written by the time we get here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from sqlmodel import Session

from app.core.utils import scrub_log
from app.models.db import AuditLog

logger = logging.getLogger(__name__)


def resolve_actor(request: Request) -> str:
    """Best-effort identifier for the caller. Set by `require_admin_key`."""
    return getattr(request.state, "admin_actor", None) or "unknown"


def resolve_source_ip(request: Request) -> str | None:
    if request.client:
        return request.client.host
    return None


def record(
    session: Session,
    request: Request,
    *,
    action: str,
    target_id: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append one audit row in its own savepoint so a logging failure
    can't poison the outer transaction.
    """
    # Structured attribution comes from the AuthResult stashed by
    # require_admin_key (issue #103); fall back to the legacy actor string
    # for callers that never passed through the gate.
    auth = getattr(request.state, "auth", None)
    actor_type = getattr(auth, "actor_type", None)
    actor_meta = getattr(auth, "actor_meta", None)
    row = AuditLog(
        actor=resolve_actor(request),
        actor_type=actor_type,
        actor_meta_json=(json.dumps(actor_meta, separators=(",", ":")) if actor_meta else None),
        source_ip=resolve_source_ip(request),
        action=action,
        target_id=target_id,
        payload_json=json.dumps(payload, separators=(",", ":")) if payload else None,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
        session.commit()
    except Exception as exc:  # noqa: BLE001 — never let logging break the caller
        logger.warning(
            "audit_log write failed for action=%s target=%s: %s",
            scrub_log(action),
            scrub_log(target_id),
            exc,
        )
