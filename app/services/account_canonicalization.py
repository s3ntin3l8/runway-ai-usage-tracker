"""One-shot repair: rewrite stored ``account_id`` values to their canonical form.

Before the ingest paths applied :func:`canonical_account_id`, events and
token-cache keys stored the sidecar's ``account_id`` verbatim while cards
went through :func:`resolve_account_id` (lowercased emails). A mixed-case
email therefore split one account into two identities — an events-only
twin next to the quota card, and a window closer that found no events.

:func:`canonicalize_stored_account_ids` runs at startup (``init_db``) and is
idempotent: once every row is canonical the scan finds nothing to do.

Collision handling (a canonical twin of the same row already exists):

- ``usage_events``: the non-canonical row is a duplicate of the same message
  (same ``event_id``) — drop it, then rebuild rollups for the affected
  ``(provider, account)`` pairs from the surviving events.
- ``usage_period_rollup``: rebuilt from events for affected pairs, never
  merged by hand.
- ``latest_usage`` / ``quota_snapshots``: the canonical row is the live
  one (the accumulator already writes canonical ids) — drop the stale twin.
- ``usage_windows`` / ``provider_configs`` / ``credential_tags`` /
  ``webhook_configs``: left in place and logged — these hold operator
  intent or frozen totals that shouldn't be merged silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import CursorResult, text
from sqlmodel import Session

from app.services.account_identity import canonical_account_id
from app.services.period_rollups import rebuild_rollups_for_pairs

logger = logging.getLogger(__name__)

# Tables whose colliding non-canonical rows are safe to drop (see module doc).
_DROP_ON_COLLISION = ("usage_events", "latest_usage", "quota_snapshots")
# Tables where a collision is reported, never resolved automatically.
_KEEP_ON_COLLISION = ("usage_windows", "provider_configs", "credential_tags", "webhook_configs")


@dataclass
class CanonicalizationReport:
    renamed: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    conflicts: dict[str, int] = field(default_factory=dict)
    rebuilt_pairs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.renamed or self.dropped)


def _table_exists(session: Session, table: str) -> bool:
    row = session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table}
    ).first()
    return row is not None


def _non_canonical_pairs(session: Session, table: str) -> list[tuple[str, str, str]]:
    """``(provider_id, raw, canonical)`` for every stored id that isn't canonical.

    Cheap pre-filter in SQL (only ids with upper-case letters or surrounding
    whitespace can change), exact check in Python.
    """
    rows = session.execute(
        text(
            f"SELECT DISTINCT provider_id, account_id FROM {table} "  # noqa: S608 — fixed table names
            "WHERE account_id IS NOT NULL "
            "AND (account_id != lower(account_id) OR account_id != trim(account_id))"
        )
    ).all()
    out = []
    for provider_id, raw in rows:
        canon = canonical_account_id(raw)
        if canon != raw:
            out.append((provider_id, raw, canon))
    return out


def canonicalize_stored_account_ids(session: Session) -> CanonicalizationReport:
    """Rewrite every non-canonical stored ``account_id``. Commits on change."""
    report = CanonicalizationReport()
    rollup_pairs: set[tuple[str, str]] = set()

    for table in (*_DROP_ON_COLLISION, *_KEEP_ON_COLLISION, "usage_period_rollup"):
        if not _table_exists(session, table):
            continue
        for provider_id, raw, canon in _non_canonical_pairs(session, table):
            params = {"p": provider_id, "raw": raw, "canon": canon}
            result: CursorResult[Any] = session.execute(  # type: ignore[assignment]
                text(
                    f"UPDATE OR IGNORE {table} SET account_id = :canon "  # noqa: S608
                    "WHERE provider_id = :p AND account_id = :raw"
                ),
                params,
            )
            renamed = result.rowcount
            if renamed:
                report.renamed[table] = report.renamed.get(table, 0) + renamed
            if table in ("usage_events", "usage_period_rollup"):
                rollup_pairs.add((provider_id, canon))
            if table == "usage_period_rollup":
                # Whatever didn't move collided with a canonical row — it is
                # rebuilt from events below, so the leftovers just go.
                session.execute(
                    text(
                        "DELETE FROM usage_period_rollup WHERE provider_id = :p AND account_id = :raw"
                    ),
                    params,
                )
                continue
            leftover = session.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} "  # noqa: S608
                    "WHERE provider_id = :p AND account_id = :raw"
                ),
                params,
            ).scalar_one()
            if not leftover:
                continue
            if table in _DROP_ON_COLLISION:
                session.execute(
                    text(
                        f"DELETE FROM {table} "  # noqa: S608
                        "WHERE provider_id = :p AND account_id = :raw"
                    ),
                    params,
                )
                report.dropped[table] = report.dropped.get(table, 0) + leftover
            else:
                report.conflicts[table] = report.conflicts.get(table, 0) + leftover

    if rollup_pairs:
        rebuild_rollups_for_pairs(session, rollup_pairs)
        report.rebuilt_pairs = sorted(rollup_pairs)

    if report.changed or rollup_pairs:
        session.commit()
        logger.info(
            "Canonicalized account_ids: renamed=%s dropped_duplicates=%s rebuilt_rollups=%d",
            report.renamed,
            report.dropped,
            len(report.rebuilt_pairs),
        )
    if report.conflicts:
        logger.warning(
            "account_id canonicalization left %s row(s) that collide with an existing "
            "canonical row; review and merge them manually.",
            report.conflicts,
        )
    return report
