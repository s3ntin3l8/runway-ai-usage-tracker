"""The per-event rollup upsert: one cached statement per event, same stored values.

A sidecar batch of 1000 new events used to hold the server's event loop for
tens of seconds, because every event built ~20 ORM upsert statements. The
upsert is now one fixed statement executed once per event with all its rows.
Value parity with the set-based rebuild is covered by
``test_rebuild_rollups_for_pairs.py``; this file pins the statement shape and
what the raw statement stores.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.period_rollups import update_rollups_for_event


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _event(**overrides) -> UsageEvent:
    values: dict = {
        "provider_id": "anthropic",
        "account_id": "acct",
        "sidecar_id": "dev-01",
        "event_id": "otel|req_1",
        "ts": datetime(2026, 11, 27, 9, 12, tzinfo=UTC),
        "kind": "message",
        "model_id": "sonnet-5.5",
        "tokens_input": 100,
        "tokens_output": 50,
        "tokens_cache_read": 1000,
        "tokens_cache_create": 200,
        "tokens_reasoning": 0,
        "cost_usd": 0.5,
        "cost_input": 0.1,
        "cost_output": 0.2,
        "cost_cache_read": 0.1,
        "cost_cache_create": 0.1,
    }
    values.update(overrides)
    return UsageEvent(**values)


def test_one_executemany_per_event_covering_every_grain() -> None:
    session = _session()
    calls: list[tuple[bool, int]] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if "usage_period_rollup" in statement:
            calls.append((executemany, len(parameters) if executemany else 1))

    event.listen(session.get_bind(), "before_cursor_execute", record)
    update_rollups_for_event(session, _event())
    # 5 periods x 4 grains, in a single executemany round.
    assert calls == [(True, 20)]


def test_repeated_events_accumulate_and_last_updated_is_stored_like_the_orm() -> None:
    session = _session()
    update_rollups_for_event(session, _event())
    update_rollups_for_event(session, _event(event_id="otel|req_2", tokens_input=7))
    session.commit()

    row = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "sonnet-5.5",
            UsagePeriodRollup.sidecar_id == "dev-01",
        )
    ).one()
    assert row.msgs == 2
    assert row.tokens_input == 107
    assert row.tokens_cache_read == 2000
    assert abs(row.cost_usd - 1.0) < 1e-9
    assert row.last_updated is not None and row.last_updated.tzinfo is not None

    # The raw column holds the naive UTC text every ORM write produces, not an
    # offset-carrying string, so rows written by either path compare alike.
    raw = session.exec(text("SELECT last_updated FROM usage_period_rollup LIMIT 1")).one()[0]  # type: ignore[call-overload]
    assert isinstance(raw, str) and "+" not in raw


def test_subtraction_adjusts_existing_rows_and_never_inserts() -> None:
    session = _session()
    ev = _event()
    update_rollups_for_event(session, ev)
    update_rollups_for_event(session, ev, sign=-1)
    # A subtraction for an account with no rows inserts nothing.
    update_rollups_for_event(session, _event(account_id="other"), sign=-1)
    session.commit()

    rows = session.exec(select(UsagePeriodRollup)).all()
    assert len(rows) == 20
    assert {r.account_id for r in rows} == {"acct"}
    assert all(r.msgs == 0 and r.tokens_input == 0 for r in rows)
