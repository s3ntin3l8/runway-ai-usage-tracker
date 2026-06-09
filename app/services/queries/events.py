"""Auto-extracted from app.services.event_query during the monolith split.
See app/services/queries/__init__.py for the public surface.
"""

from datetime import datetime

from sqlmodel import Session, select

from app.models.db import UsageEvent

# ---------------------------------------------------------------------------
# 7.1  query_events
# ---------------------------------------------------------------------------


def query_events(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    model_id: str | None = None,
    sidecar_id: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    order: str = "desc",
) -> list[UsageEvent]:
    """Return raw UsageEvent rows filtered and ordered by ts.

    Rows are returned newest-first (order='desc') or oldest-first
    (order='asc').  raw_json exclusion is handled by the endpoint layer.
    """
    stmt = select(UsageEvent).where(
        UsageEvent.provider_id == provider_id,
        UsageEvent.account_id == account_id,
    )

    if since is not None:
        stmt = stmt.where(UsageEvent.ts >= since)
    if until is not None:
        stmt = stmt.where(UsageEvent.ts <= until)
    if model_id is not None:
        stmt = stmt.where(UsageEvent.model_id == model_id)
    if sidecar_id is not None:
        stmt = stmt.where(UsageEvent.sidecar_id == sidecar_id)
    if kind is not None:
        stmt = stmt.where(UsageEvent.kind == kind)

    if order == "asc":
        stmt = stmt.order_by(UsageEvent.ts.asc())  # type: ignore[attr-defined]
    else:
        stmt = stmt.order_by(UsageEvent.ts.desc())  # type: ignore[attr-defined]

    stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())
