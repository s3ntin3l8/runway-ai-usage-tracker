"""One-shot migration to account-independent event identity.

``usage_events`` used to be unique on ``(provider_id, account_id, event_id)``,
so the same message pushed under a second account (a retag, a tag hint that
arrived later, the auto-hint turning off when a second host appeared) was
inserted again and counted twice. The model now carries a unique index on
``(provider_id, event_id)``; existing databases need their cross-account
duplicates collapsed before that index can be created.

For each duplicated ``(provider_id, event_id)`` one row is kept — a real
account over ``"default"``, then the most recently inserted row (the latest
attribution) — the rest are deleted, and rollups are rebuilt from events for
every affected ``(provider, account)`` pair. Runs from ``init_db``; a no-op
once the index exists.

Closed ``usage_windows`` rows are frozen totals and are not recomputed; a
window closed while the double count was live keeps its inflated totals.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlmodel import Session

from app.services.period_rollups import rebuild_rollups_for_pairs

logger = logging.getLogger(__name__)

INDEX_NAME = "uq_usage_events_provider_event"


def _index_exists(session: Session) -> bool:
    row = session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"), {"n": INDEX_NAME}
    ).first()
    return row is not None


def migrate_to_provider_event_identity(session: Session) -> int:
    """Collapse cross-account duplicate events and create the unique index.

    Returns the number of duplicate rows removed. Commits.
    """
    if _index_exists(session):
        return 0

    dup_groups = session.execute(
        text(
            "SELECT provider_id, event_id FROM usage_events "
            "GROUP BY provider_id, event_id HAVING COUNT(*) > 1"
        )
    ).all()

    removed = 0
    affected: set[tuple[str, str]] = set()
    for provider_id, event_id in dup_groups:
        rows = session.execute(
            text(
                "SELECT id, account_id FROM usage_events "
                "WHERE provider_id = :p AND event_id = :e "
                # Keeper first: a real account beats "default", then the
                # latest attribution (highest id).
                "ORDER BY (account_id = 'default') ASC, id DESC"
            ),
            {"p": provider_id, "e": event_id},
        ).all()
        _keep_id, keep_account = rows[0]
        affected.add((provider_id, keep_account))
        for row_id, account_id in rows[1:]:
            session.execute(text("DELETE FROM usage_events WHERE id = :id"), {"id": row_id})
            affected.add((provider_id, account_id))
            removed += 1

    if affected:
        rebuild_rollups_for_pairs(session, affected)
    session.execute(
        text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} ON usage_events (provider_id, event_id)"
        )
    )
    session.commit()
    if removed:
        logger.info(
            "Migrated usage_events to (provider_id, event_id) identity: removed %d "
            "cross-account duplicate(s), rebuilt rollups for %d account(s)",
            removed,
            len(affected),
        )
    return removed
