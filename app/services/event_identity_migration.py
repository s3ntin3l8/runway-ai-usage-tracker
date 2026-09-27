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

The whole collapse is set-based (one window-function ranking plus one bulk
DELETE) rather than a per-group Python loop: on a database with tens of
thousands of duplicate groups, a round trip per group added minutes to the
first boot after upgrading. See ``rebuild_rollups_for_pairs`` for the
equivalent fix on the rollup-rebuild side, which dominates this migration's
cost far more than the dedup step ever did.

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

# The ranking join below needs a seek on (provider_id, event_id) that also
# covers account_id — without one, the query planner's only candidate is
# the old 3-column uq_usage_events_identity (provider_id, account_id,
# event_id): event_id isn't its second column, so it can only narrow by
# provider_id and falls back to comparing every row for that provider. On a
# provider with tens of thousands of events and duplicate groups (e.g.
# minimax) that's a quadratic blowup — tens of thousands of full
# per-provider scans — confirmed to turn a sub-second query into one that
# never finished in over ten minutes against a production-scale database.
# This index covers account_id too so the seek stays a single index lookup
# with no extra table read, and is dropped once the migration no longer
# needs it (the real unique index below replaces it for the covered pair).
# Exposed as constants (not inlined) so the regression test pinning the
# query plan exercises this exact DDL/SELECT rather than a copy of it.
_TMP_JOIN_INDEX = "ix_event_identity_migration_tmp"
_TMP_JOIN_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {_TMP_JOIN_INDEX} "
    "ON usage_events (provider_id, event_id, account_id)"
)

# Rank every row belonging to a duplicated (provider_id, event_id) group
# (the semi-join keeps the window function from ranking the whole table).
# Keeper first: a real account beats "default", then the latest attribution
# (highest id) — same tie-break the old per-group loop used.
_RANKING_SELECT_SQL = (
    "SELECT e.id, e.provider_id, e.account_id, "
    "ROW_NUMBER() OVER ("
    "  PARTITION BY e.provider_id, e.event_id "
    "  ORDER BY (e.account_id = 'default') ASC, e.id DESC"
    ") AS rn "
    "FROM usage_events e "
    "JOIN ("
    "  SELECT provider_id, event_id FROM usage_events "
    "  GROUP BY provider_id, event_id HAVING COUNT(*) > 1"
    ") d ON d.provider_id = e.provider_id AND d.event_id = e.event_id"
)


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

    session.execute(text(_TMP_JOIN_INDEX_SQL))
    session.execute(text(f"CREATE TEMP TABLE _event_identity_ranked AS {_RANKING_SELECT_SQL}"))
    session.execute(text(f"DROP INDEX IF EXISTS {_TMP_JOIN_INDEX}"))

    # Every (provider_id, account_id) pair touched by a duplicate group —
    # both the keeper's and every loser's — needs its rollups rebuilt from
    # the surviving events.
    affected = {
        (row.provider_id, row.account_id)
        for row in session.execute(
            text("SELECT DISTINCT provider_id, account_id FROM _event_identity_ranked")
        )
    }

    result = session.execute(
        text(
            "DELETE FROM usage_events WHERE id IN "
            "(SELECT id FROM _event_identity_ranked WHERE rn > 1)"
        )
    )
    removed = result.rowcount or 0  # type: ignore[attr-defined]
    session.execute(text("DROP TABLE _event_identity_ranked"))

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
