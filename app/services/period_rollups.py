"""Atomic upsert into usage_period_rollup per event."""

from datetime import UTC, datetime

from sqlalchemy import update as sa_update
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

# Additive per-event columns summed into each rollup grain (msgs is +/-1).
_SUM_FIELDS = (
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_create",
    "tokens_reasoning",
    "cost_usd",
    "cost_input",
    "cost_output",
    "cost_cache_read",
    "cost_cache_create",
)


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
    deltas = {field: sign * (getattr(ev, field) or 0) for field in _SUM_FIELDS}

    for period_type, period_key in _period_keys(ev.ts):
        for model_id, sidecar_id in unique_grains:
            key = {
                "provider_id": ev.provider_id,
                "account_id": ev.account_id,
                "period_type": period_type,
                "period_key": period_key,
                "model_id": model_id,
                "sidecar_id": sidecar_id,
            }
            increments = {field: table.c[field] + delta for field, delta in deltas.items()}
            increments["msgs"] = table.c.msgs + sign
            increments["last_updated"] = now
            if sign < 0:
                # Subtraction only ever adjusts an existing row: with no row
                # there is nothing to subtract from, and an upsert would
                # insert negative totals (e.g. on a DB whose events were
                # imported without rollup replays).
                session.execute(
                    sa_update(UsagePeriodRollup)
                    .where(*(table.c[k] == v for k, v in key.items()))
                    .values(increments)
                )
                continue
            stmt = sqlite_insert(UsagePeriodRollup).values(
                **key, msgs=sign, **deltas, last_updated=now
            )
            stmt = stmt.on_conflict_do_update(index_elements=list(_INDEX_ELEMENTS), set_=increments)
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
