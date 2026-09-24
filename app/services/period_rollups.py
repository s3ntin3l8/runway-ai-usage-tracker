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


def update_rollups_for_event(session: Session, ev: UsageEvent, *, sign: int = 1) -> None:
    """Atomically upsert the rollup rows touched by this event.

    ``sign=-1`` subtracts the event instead (used when an event is
    re-attributed to another account — its contribution moves between the
    two accounts' rollup rows).

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
                msgs=sign,
                tokens_input=sign * (ev.tokens_input or 0),
                tokens_output=sign * (ev.tokens_output or 0),
                tokens_cache_read=sign * (ev.tokens_cache_read or 0),
                tokens_cache_create=sign * (ev.tokens_cache_create or 0),
                tokens_reasoning=sign * (ev.tokens_reasoning or 0),
                cost_usd=sign * (ev.cost_usd or 0),
                cost_input=sign * (ev.cost_input or 0),
                cost_output=sign * (ev.cost_output or 0),
                cost_cache_read=sign * (ev.cost_cache_read or 0),
                cost_cache_create=sign * (ev.cost_cache_create or 0),
                last_updated=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=list(_INDEX_ELEMENTS),
                set_={
                    "msgs": table.c.msgs + sign,
                    "tokens_input": table.c.tokens_input + sign * (ev.tokens_input or 0),
                    "tokens_output": table.c.tokens_output + sign * (ev.tokens_output or 0),
                    "tokens_cache_read": table.c.tokens_cache_read
                    + sign * (ev.tokens_cache_read or 0),
                    "tokens_cache_create": table.c.tokens_cache_create
                    + sign * (ev.tokens_cache_create or 0),
                    "tokens_reasoning": table.c.tokens_reasoning
                    + sign * (ev.tokens_reasoning or 0),
                    "cost_usd": table.c.cost_usd + sign * (ev.cost_usd or 0),
                    "cost_input": table.c.cost_input + sign * (ev.cost_input or 0),
                    "cost_output": table.c.cost_output + sign * (ev.cost_output or 0),
                    "cost_cache_read": table.c.cost_cache_read + sign * (ev.cost_cache_read or 0),
                    "cost_cache_create": table.c.cost_cache_create
                    + sign * (ev.cost_cache_create or 0),
                    "last_updated": now,
                },
            )
            session.execute(stmt)


def rebuild_rollups_for_pairs(session: Session, pairs: set[tuple[str, str]]) -> None:
    """Recompute rollups for ``(provider_id, account_id)`` pairs from events.

    Events are the source of truth; this drops the pairs' rollup rows and
    replays their message events. Caller owns the transaction.
    """
    from sqlmodel import col, delete, select

    from app.models.db import UsagePeriodRollup

    for provider_id, account_id in sorted(pairs):
        session.exec(
            delete(UsagePeriodRollup).where(
                col(UsagePeriodRollup.provider_id) == provider_id,
                col(UsagePeriodRollup.account_id) == account_id,
            )
        )
        events = session.exec(
            select(UsageEvent)
            .where(
                UsageEvent.provider_id == provider_id,
                UsageEvent.account_id == account_id,
                UsageEvent.kind == "message",
            )
            .order_by(col(UsageEvent.ts))
        ).all()
        for ev in events:
            update_rollups_for_event(session, ev)
