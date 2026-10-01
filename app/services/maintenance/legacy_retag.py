"""Retag events stuck under a legacy OpenCode-sibling provider id onto the
provider Runway now folds them into — the Data Health `legacy_provider_ids`
fixer. See `legacy_providers.py` for what "legacy" means here and why the
map lives in `app/` rather than being imported from the sidecar.

A legacy id and its canonical target are different `provider_id`s, so the
`(provider_id, event_id)` unique index doesn't stop the same underlying
message existing under both today (e.g. `opencode-xai` and `xai` both
carrying the OpenCode sidecar's canonical-fold miss and a later hit for the
same message) — retagging the legacy row onto the canonical provider_id is
what turns that into a real collision, which this module resolves before
writing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.db import LatestUsage, QuotaSnapshot, UsageEvent, UsagePeriodRollup
from app.services.maintenance._chunked_sql import chunked_delete, chunked_update
from app.services.maintenance.legacy_providers import LEGACY_PROVIDER_MAP
from app.services.maintenance.rollups import rebuild_rollups_for_providers
from app.services.maintenance.windows import rebuild_windows_for_providers

_COLLISION_BATCH = 500


def pick_winner(a: UsageEvent, b: UsageEvent) -> tuple[UsageEvent, UsageEvent]:
    """Return (winner, loser) for two events sharing an event_id across a
    legacy/canonical provider pair. Same tie-break order as
    `collapse_default_account_events.py`'s historical `_pick_winner`: a
    message beats an error, then more tokens, then lower id."""
    a_is_message = a.kind == "message"
    b_is_message = b.kind == "message"
    if a_is_message != b_is_message:
        return (a, b) if a_is_message else (b, a)
    a_tokens = a.tokens_input + a.tokens_output
    b_tokens = b.tokens_input + b.tokens_output
    if a_tokens != b_tokens:
        return (a, b) if a_tokens > b_tokens else (b, a)
    return (a, b) if (a.id or 0) < (b.id or 0) else (b, a)


@dataclass
class RetagPlan:
    canonical_provider_id: str = ""
    total: int = 0
    collisions: int = 0  # would be resolved via pick_winner and one side dropped
    retagged: int = 0  # moved to canonical with no collision
    rollups_to_purge: int = (
        0  # usage_period_rollup rows under the legacy id the apply step will delete
    )
    samples: list[str] = field(default_factory=list)


@dataclass
class RetagResult:
    canonical_provider_id: str = ""
    retagged: int = 0
    collisions_resolved: int = 0
    latest_usage_dropped: int = 0
    quota_snapshots_dropped: int = 0
    rollups_legacy_purged: int = 0  # rollup rows under the legacy id the apply step deleted
    rollups_rebuilt_pairs: int = 0
    windows_rebuilt: int = 0


def _canonical_for(legacy_provider_id: str) -> str:
    canonical = LEGACY_PROVIDER_MAP.get(legacy_provider_id)
    if canonical is None:
        raise ValueError(f"{legacy_provider_id!r} is not a known legacy provider id")
    return canonical


def _collision_event_ids(
    session: Session, legacy_provider_id: str, canonical_provider_id: str
) -> set[str]:
    rows = session.execute(
        select(UsageEvent.event_id)
        .where(col(UsageEvent.provider_id) == legacy_provider_id)
        .intersect(
            select(UsageEvent.event_id).where(col(UsageEvent.provider_id) == canonical_provider_id)
        )
    ).all()
    return {r[0] for r in rows}


def plan_legacy_retag(
    session: Session, legacy_provider_id: str, *, sample_size: int = 20
) -> RetagPlan:
    """Read-only preview."""
    canonical = _canonical_for(legacy_provider_id)
    total = session.exec(
        select(UsageEvent).where(UsageEvent.provider_id == legacy_provider_id)
    ).all()
    collisions = _collision_event_ids(session, legacy_provider_id, canonical)
    rollups_to_purge = session.execute(
        select(func.count()).where(col(UsagePeriodRollup.provider_id) == legacy_provider_id)
    ).one()[0]
    return RetagPlan(
        canonical_provider_id=canonical,
        total=len(total),
        collisions=len(collisions),
        retagged=len(total) - len(collisions),
        rollups_to_purge=rollups_to_purge,
        samples=[ev.event_id for ev in total[:sample_size]],
    )


def apply_legacy_retag(session: Session, legacy_provider_id: str) -> RetagResult:
    """Retag every event under `legacy_provider_id` onto its canonical
    provider, resolving any (post-retag) collision via `pick_winner`,
    preserving a source-reported cost that would otherwise only have been
    recognized via the legacy id's provider-prefix check, purging the legacy
    id's own `usage_period_rollup` rows (so they don't linger as
    `rollup_drift` orphans), and rebuilding rollups/windows for both
    providers.

    Every write is chunked (`_chunked_sql`) and committed per batch — a
    legacy id like `opencode-openrouter` can carry tens of thousands of
    events, and this fixer must never hold SQLite's writer lock for one
    giant transaction. Resumable: interrupting mid-run and calling again
    picks up wherever the last committed batch left off.
    """
    canonical = _canonical_for(legacy_provider_id)
    result = RetagResult(canonical_provider_id=canonical)

    collision_ids = sorted(_collision_event_ids(session, legacy_provider_id, canonical))
    for start in range(0, len(collision_ids), _COLLISION_BATCH):
        batch = collision_ids[start : start + _COLLISION_BATCH]
        legacy_rows = {
            ev.event_id: ev
            for ev in session.exec(
                select(UsageEvent).where(
                    col(UsageEvent.provider_id) == legacy_provider_id,
                    col(UsageEvent.event_id).in_(batch),
                )
            )
        }
        canonical_rows = {
            ev.event_id: ev
            for ev in session.exec(
                select(UsageEvent).where(
                    col(UsageEvent.provider_id) == canonical,
                    col(UsageEvent.event_id).in_(batch),
                )
            )
        }
        for event_id in batch:
            legacy_row = legacy_rows.get(event_id)
            canonical_row = canonical_rows.get(event_id)
            if legacy_row is None or canonical_row is None:
                continue  # raced away between the plan query and here — skip, next scan picks it up
            _winner, loser = pick_winner(legacy_row, canonical_row)
            session.delete(loser)
            result.collisions_resolved += 1
        session.commit()
        session.expunge_all()

    # Every remaining legacy-provider row (survivors of a collision, plus
    # every row that never collided) retags onto the canonical provider_id.
    # A legacy id is always OpenCode-sourced, so its cost_usd is the
    # source-reported subscription amount, exactly like the provider-prefix
    # check `recost_events`/`resolve_event_cost` use to recognize a reported
    # cost — but that check is keyed on the *current* provider_id, so it
    # stops recognizing this event the moment it's retagged. Backfill
    # cost_reported_usd here so that recognition survives the rename.
    result.retagged = chunked_update(
        session,
        UsageEvent,
        [col(UsageEvent.provider_id) == legacy_provider_id],
        {
            "provider_id": canonical,
            "cost_reported_usd": func.coalesce(UsageEvent.cost_reported_usd, UsageEvent.cost_usd),
        },
    )

    # The legacy id's own dashboard card/history no longer applies — the
    # canonical provider's card is authoritative going forward.
    result.latest_usage_dropped = chunked_delete(
        session, LatestUsage, [col(LatestUsage.provider_id) == legacy_provider_id]
    )
    result.quota_snapshots_dropped = chunked_delete(
        session, QuotaSnapshot, [col(QuotaSnapshot.provider_id) == legacy_provider_id]
    )

    # rebuild_rollups_for_providers derives its pair list from usage_events,
    # which no longer carries the legacy id after the update above — purge
    # the legacy id's own rollup rows explicitly or they linger as
    # rollup_drift orphans. Done before the rebuild so a hypothetical
    # surviving legacy event still gets correct rows recreated by it.
    result.rollups_legacy_purged = chunked_delete(
        session, UsagePeriodRollup, [col(UsagePeriodRollup.provider_id) == legacy_provider_id]
    )

    touched = [legacy_provider_id, canonical]
    result.rollups_rebuilt_pairs = rebuild_rollups_for_providers(session, touched)
    session.commit()
    result.windows_rebuilt = rebuild_windows_for_providers(session, touched)

    return result
