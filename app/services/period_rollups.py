"""Atomic upsert into usage_period_rollup per event."""

from datetime import UTC, datetime

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from app.models.db import UsageEvent, UsagePeriodRollup


def _period_keys(ts: datetime) -> list[tuple[str, str]]:
    """Return (period_type, period_key) tuples for a given event timestamp."""
    return [
        ("hour", ts.strftime("%Y-%m-%dT%H")),
        ("day", ts.strftime("%Y-%m-%d")),
        ("month", ts.strftime("%Y-%m")),
        ("year", ts.strftime("%Y")),
        ("lifetime", "all"),
    ]


_INDEX_ELEMENTS = (
    "provider_id",
    "account_id",
    "period_type",
    "period_key",
    "model_id",
    "sidecar_id",
)


_ROLLUP_TABLE = UsagePeriodRollup.__table__  # type: ignore[attr-defined]


def update_rollups_for_event(session: Session, ev: UsageEvent) -> None:
    """Atomically upsert the rollup rows touched by this event.

    Uses INSERT … ON CONFLICT DO UPDATE so the SELECT + mutate + UPDATE
    sequence is collapsed into one statement. SQLite serialises writes
    under EXCLUSIVE lock, so concurrent ingests can no longer lose
    increments through the read-modify-write window.

    Caller owns the transaction — this function does not commit.

    Grain matrix: ('',''), (model_id,''), ('',sidecar_id), (model_id,sidecar_id).
    When model_id is empty/None the matrix deduplicates to 2 unique grains.
    """
    grains: list[tuple[str, str]] = [
        ("", ""),
        (ev.model_id or "", ""),
        ("", ev.sidecar_id or ""),
        (ev.model_id or "", ev.sidecar_id or ""),
    ]
    # Dedupe while preserving order.
    unique_grains = list(dict.fromkeys(grains))
    now = datetime.now(UTC)
    table = _ROLLUP_TABLE

    for period_type, period_key in _period_keys(ev.ts):
        for model_id, sidecar_id in unique_grains:
            stmt = sqlite_insert(UsagePeriodRollup).values(
                provider_id=ev.provider_id,
                account_id=ev.account_id,
                period_type=period_type,
                period_key=period_key,
                model_id=model_id,
                sidecar_id=sidecar_id,
                msgs=1,
                tokens_input=ev.tokens_input,
                tokens_output=ev.tokens_output,
                tokens_cache_read=ev.tokens_cache_read,
                tokens_cache_create=ev.tokens_cache_create,
                tokens_reasoning=ev.tokens_reasoning,
                cost_usd=ev.cost_usd,
                last_updated=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=list(_INDEX_ELEMENTS),
                set_={
                    "msgs": table.c.msgs + 1,
                    "tokens_input": table.c.tokens_input + ev.tokens_input,
                    "tokens_output": table.c.tokens_output + ev.tokens_output,
                    "tokens_cache_read": table.c.tokens_cache_read + ev.tokens_cache_read,
                    "tokens_cache_create": table.c.tokens_cache_create + ev.tokens_cache_create,
                    "tokens_reasoning": table.c.tokens_reasoning + ev.tokens_reasoning,
                    "cost_usd": table.c.cost_usd + ev.cost_usd,
                    "last_updated": now,
                },
            )
            session.execute(stmt)
