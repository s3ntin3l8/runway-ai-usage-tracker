"""Best-effort rebuild of the derived tables after a partially failed repair.

A repair that writes events in batches can die half-way: the batches that were
committed are stored, but the rollups and windows were only rebuilt *after*
the last batch, so they lag until the next successful run. This module
rebuilds them for the pairs that were committed.

It never raises. The caller is in a `finally` block after a failed repair, and
the original failure is what the caller must see — a rebuild that fails too
is logged and swallowed, never allowed to mask it.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from app.services.maintenance.windows import rebuild_windows_for_providers
from app.services.period_rollups import rebuild_rollups_for_pairs

logger = logging.getLogger(__name__)


def rebuild_pairs_best_effort(
    session: Session,
    pairs: set[tuple[str, str]],
    *,
    action_desc: str,
) -> bool:
    """Rebuild the rollups and windows of `pairs`; never raise.

    Returns whether the rebuild succeeded. The session may still hold the
    failed batch's transaction, so it is rolled back first.
    """
    if not pairs:
        return True
    try:
        session.rollback()
        rebuild_rollups_for_pairs(session, pairs)
        session.commit()
        rebuild_windows_for_providers(session, sorted({p for p, _a in pairs}))
    except Exception as exc:  # noqa: BLE001 — the original failure must win
        logger.warning(
            "Could not rebuild the rollups and windows of %s after a failed repair: %s",
            action_desc,
            exc,
        )
        return False
    return True
