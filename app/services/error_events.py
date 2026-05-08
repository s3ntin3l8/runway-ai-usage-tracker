"""Record provider-side errors (rate-limit, auth failure, etc.) as kind=error UsageEvent rows."""

import logging
from datetime import UTC, datetime

from sqlmodel import Session

from app.models.db import UsageEvent

logger = logging.getLogger(__name__)


def record_provider_error(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    reason: str,  # rate_limit | auth_failed | quota_exceeded | timeout | network
    detail: str = "",
) -> None:
    """Insert a kind=error event. event_id is synthesized from (provider, account, ts, reason).

    Uses a per-second granularity key so that a single error per second/reason
    is idempotent (duplicate calls in the same second with the same reason are
    silently dropped via the unique constraint).
    """
    ts = datetime.now(UTC)
    event_id = f"err|{provider_id}|{account_id}|{int(ts.timestamp())}|{reason}"
    ev = UsageEvent(
        provider_id=provider_id,
        account_id=account_id or "default",
        sidecar_id="server",
        event_id=event_id,
        ts=ts,
        kind="error",
        stop_reason=reason,
        raw_json=detail[:500] if detail else None,
    )
    try:
        session.add(ev)
        session.commit()
    except Exception:
        session.rollback()
        logger.debug(
            "record_provider_error: duplicate or DB error for %s/%s reason=%s (suppressed)",
            provider_id,
            account_id,
            reason,
        )
