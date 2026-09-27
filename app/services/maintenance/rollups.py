"""Re-exports the set-based rollup rebuild used by every maintenance repair.

The functions themselves live in `app/services/period_rollups.py`, next to
`update_rollups_for_event` (the incremental per-event upsert they replace for
a bulk rebuild) — they were already extracted and rewritten as bulk SQL
there (fixing a real startup-hang bug; see that module's history) before
this `maintenance` package existed. This module just gives them the import
path the rest of `app/services/maintenance/` and `app/services/data_health/`
expect, without moving well-tested code for a naming preference alone.
"""

from __future__ import annotations

from app.services.period_rollups import (
    rebuild_rollups_for_pairs,
    rebuild_rollups_for_providers,
)

__all__ = ["rebuild_rollups_for_pairs", "rebuild_rollups_for_providers"]
