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


# (period_type, SQLite strftime format-or-literal) — mirrors _period_keys
# above exactly (same names, same formats) so a rebuilt rollup and a
# per-event-replayed one produce identical period_key strings.
_PERIOD_KEY_SQL: tuple[tuple[str, str], ...] = (
    ("hour", "strftime('%Y-%m-%dT%H', ts)"),
    ("day", "strftime('%Y-%m-%d', ts)"),
    ("month", "strftime('%Y-%m', ts)"),
    ("year", "strftime('%Y', ts)"),
    ("lifetime", "'all'"),
)

# (model_id expression, sidecar_id expression, extra WHERE clause or None) —
# mirrors update_rollups_for_event's grain-matrix dedupe: the (model_id, '')
# and ('', sidecar_id) grains only add a row distinct from ('', '') when
# that field is actually non-empty, and (model_id, sidecar_id) only when
# both are. Without these guards a bulk rebuild would insert duplicate rows
# for every event whose model_id or sidecar_id happens to be empty.
_GRAINS: tuple[tuple[str, str, str | None], ...] = (
    ("''", "''", None),
    ("COALESCE(model_id, '')", "''", "COALESCE(model_id, '') <> ''"),
    ("''", "COALESCE(sidecar_id, '')", "COALESCE(sidecar_id, '') <> ''"),
    (
        "COALESCE(model_id, '')",
        "COALESCE(sidecar_id, '')",
        "COALESCE(model_id, '') <> '' AND COALESCE(sidecar_id, '') <> ''",
    ),
)

_SUM_SELECT = ", ".join(f"COALESCE({f}, 0) AS {f}" for f in _SUM_FIELDS)
_SUM_AGG = ", ".join(f"SUM({f})" for f in _SUM_FIELDS)


def rebuild_rollups_for_providers(session: Session, providers: list[str] | None) -> int:
    """Recompute rollups for every (provider_id, account_id) pair currently
    seen in usage_events for the given providers (`None` = every provider) —
    the coarser, whole-provider counterpart to `rebuild_rollups_for_pairs`
    for a repair that doesn't already know which specific pairs it touched
    (e.g. `scripts/recost_events.py`'s Phase C, which recomputes cost for a
    whole provider at once). Returns the number of pairs rebuilt.
    """
    from sqlalchemy import text

    stmt = "SELECT DISTINCT provider_id, account_id FROM usage_events WHERE kind = 'message'"
    params: dict[str, object] = {}
    if providers:
        placeholders = ", ".join(f":p{i}" for i in range(len(providers)))
        stmt += f" AND provider_id IN ({placeholders})"
        params = {f"p{i}": p for i, p in enumerate(providers)}
    pairs = {(row.provider_id, row.account_id) for row in session.execute(text(stmt), params)}
    rebuild_rollups_for_pairs(session, pairs)
    return len(pairs)


def rebuild_rollups_for_pairs(session: Session, pairs: set[tuple[str, str]]) -> None:
    """Recompute rollups for ``(provider_id, account_id)`` pairs from events.

    Events are the source of truth; this drops the pairs' rollup rows and
    replaces them with a set-based re-aggregation — the same grains and
    period keys ``update_rollups_for_event`` would produce by replaying every
    event, but as one ``GROUP BY`` per grain instead of ~20 upserts per
    event. On a pair with tens of thousands of events, replaying was minutes
    of round trips; this is a handful of aggregate queries. Caller owns the
    transaction.
    """
    from sqlalchemy import bindparam, text

    # Bound explicitly through the column's own DateTime type rather than
    # left to the DBAPI driver's default adapter for a bare tz-aware
    # datetime — that adapter (used only when a raw text() bind carries no
    # declared type) keeps the "+00:00" offset, while every other write path
    # to this table goes through the ORM and stores a naive UTC string. A
    # mixed column made loading + comparing rows across pairs unreliable.
    now_col_type = UsagePeriodRollup.__table__.c.last_updated.type  # type: ignore[attr-defined]
    now = bindparam("now", datetime.now(UTC), type_=now_col_type)

    for provider_id, account_id in sorted(pairs):
        session.execute(
            text("DELETE FROM usage_period_rollup WHERE provider_id = :p AND account_id = :a"),
            {"p": provider_id, "a": account_id},
        )
        for model_expr, sidecar_expr, extra_where in _GRAINS:
            where = "provider_id = :p AND account_id = :a AND kind = 'message'"
            if extra_where:
                where = f"{where} AND {extra_where}"
            # Interpolates only fixed column names and the module-level
            # _PERIOD_KEY_SQL/_GRAINS/_SUM_FIELDS constants above — never
            # request input. provider_id/account_id are bound parameters.
            union = " UNION ALL ".join(
                f"SELECT provider_id, account_id, '{period_type}' AS period_type, "  # noqa: S608
                f"{period_key_sql} AS period_key, {model_expr} AS model_id, "
                f"{sidecar_expr} AS sidecar_id, {_SUM_SELECT} "
                f"FROM usage_events WHERE {where}"
                for period_type, period_key_sql in _PERIOD_KEY_SQL
            )
            session.execute(
                text(
                    "INSERT INTO usage_period_rollup "  # noqa: S608
                    "(provider_id, account_id, period_type, period_key, model_id, sidecar_id, "
                    "msgs, tokens_input, tokens_output, tokens_cache_read, tokens_cache_create, "
                    "tokens_reasoning, cost_usd, cost_input, cost_output, cost_cache_read, "
                    "cost_cache_create, last_updated) "
                    "SELECT provider_id, account_id, period_type, period_key, model_id, "
                    f"sidecar_id, COUNT(*), {_SUM_AGG}, :now "
                    f"FROM ({union}) "
                    "GROUP BY provider_id, account_id, period_type, period_key, model_id, sidecar_id"
                ).bindparams(now),
                {"p": provider_id, "a": account_id},
            )
