"""Account-independent event identity: ``(provider_id, event_id)``.

The same message pushed under a new account (operator retag, hint arrived or
was withdrawn) must move, not double count. Covers the ingest-time
re-attribution rule and the one-shot migration of existing databases.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup
from app.models.schemas import UsageEventPush
from app.services.event_identity_migration import (
    _RANKING_SELECT_SQL,
    _TMP_JOIN_INDEX,
    _TMP_JOIN_INDEX_SQL,
    INDEX_NAME,
    migrate_to_provider_event_identity,
)
from app.services.event_ingestor import EventIngestor
from app.services.period_rollups import update_rollups_for_event


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _push(account_id: str, event_id: str = "msg_1", kind: str = "message") -> UsageEventPush:
    return UsageEventPush(
        provider_id="anthropic",
        account_id=account_id,
        event_id=event_id,
        ts="2026-09-01T10:00:00Z",
        kind=kind,
        model_id="sonnet-4.5",
        tokens_input=100,
        tokens_output=10,
        error_reason="auth" if kind == "error" else None,
    )


def _lifetime(session: Session, account_id: str) -> tuple[int, int]:
    """(msgs, tokens_input) on the account's lifetime/all-models/all-sidecars grain."""
    row = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.account_id == account_id,
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    return (row.msgs, row.tokens_input) if row else (0, 0)


# ---------------------------------------------------------------------------
# Ingest-time re-attribution
# ---------------------------------------------------------------------------


def test_same_sidecar_retag_moves_event_and_rollups(session: Session):
    ing = EventIngestor(session)
    ing.ingest([_push("bob@example.com")], sidecar_id="laptop")
    assert _lifetime(session, "bob@example.com") == (1, 100)

    # The operator tags the credential; the sidecar re-pushes under the tag.
    res = EventIngestor(session).ingest([_push("alice@example.com")], sidecar_id="laptop")

    assert (res.events_inserted, res.events_reattributed, res.events_duplicate) == (0, 1, 0)
    rows = session.exec(select(UsageEvent)).all()
    assert [(r.account_id, r.event_id) for r in rows] == [("alice@example.com", "msg_1")]
    # Counted exactly once, on the new account.
    assert _lifetime(session, "alice@example.com") == (1, 100)
    assert _lifetime(session, "bob@example.com") == (0, 0)


def test_other_sidecar_cannot_steal_an_event(session: Session):
    """A second host seeing the same log (shared home dir) keeps the first
    attribution — two hosts must not flip an event back and forth."""
    EventIngestor(session).ingest([_push("alice@example.com")], sidecar_id="laptop")
    res = EventIngestor(session).ingest([_push("bob@example.com")], sidecar_id="desktop")

    assert (res.events_inserted, res.events_reattributed, res.events_duplicate) == (0, 0, 1)
    row = session.exec(select(UsageEvent)).one()
    assert row.account_id == "alice@example.com"
    assert _lifetime(session, "bob@example.com") == (0, 0)


def test_identical_repush_is_a_plain_duplicate(session: Session):
    EventIngestor(session).ingest([_push("alice@example.com")], sidecar_id="laptop")
    res = EventIngestor(session).ingest([_push("alice@example.com")], sidecar_id="laptop")
    assert (res.events_inserted, res.events_reattributed, res.events_duplicate) == (0, 0, 1)
    assert _lifetime(session, "alice@example.com") == (1, 100)


def test_error_events_reattribute_without_rollups(session: Session):
    EventIngestor(session).ingest([_push("bob@example.com", kind="error")], sidecar_id="laptop")
    res = EventIngestor(session).ingest(
        [_push("alice@example.com", kind="error")], sidecar_id="laptop"
    )
    assert res.events_reattributed == 1
    assert session.exec(select(UsageEvent)).one().account_id == "alice@example.com"
    assert session.exec(select(UsagePeriodRollup)).all() == []


def test_reattributed_row_takes_the_repushed_payload(session: Session):
    """The moved row is refreshed from the re-push, and rollups follow the
    refreshed values — no stale model / project / cost left behind."""
    EventIngestor(session).ingest([_push("bob@example.com")], sidecar_id="laptop")

    enriched = _push("alice@example.com")
    enriched.tokens_input = 250
    enriched.cwd = "/home/me/work/runway"
    EventIngestor(session).ingest([enriched], sidecar_id="laptop")

    row = session.exec(select(UsageEvent)).one()
    assert (row.account_id, row.tokens_input, row.project) == (
        "alice@example.com",
        250,
        "runway",
    )
    assert _lifetime(session, "alice@example.com") == (1, 250)
    assert _lifetime(session, "bob@example.com") == (0, 0)


def test_error_repush_over_a_message_removes_its_rollups(session: Session):
    EventIngestor(session).ingest([_push("bob@example.com")], sidecar_id="laptop")
    EventIngestor(session).ingest([_push("alice@example.com", kind="error")], sidecar_id="laptop")
    row = session.exec(select(UsageEvent)).one()
    assert (row.account_id, row.kind) == ("alice@example.com", "error")
    assert _lifetime(session, "bob@example.com") == (0, 0)
    assert _lifetime(session, "alice@example.com") == (0, 0)


def test_negative_rollup_delta_never_inserts_a_row(session: Session):
    """Subtracting an event whose rollup row doesn't exist (e.g. events
    imported without rollup replays) is a no-op, not a negative row."""
    ev = UsageEvent(
        provider_id="anthropic",
        account_id="ghost@example.com",
        sidecar_id="laptop",
        event_id="msg_x",
        ts=datetime(2026, 9, 1, 10, tzinfo=UTC),
        kind="message",
        model_id="sonnet-4.5",
        tokens_input=100,
    )
    update_rollups_for_event(session, ev, sign=-1)
    assert session.exec(select(UsagePeriodRollup)).all() == []


# ---------------------------------------------------------------------------
# One-shot migration of existing databases
# ---------------------------------------------------------------------------


def _legacy_event(account_id: str, event_id: str, tokens: int = 100) -> UsageEvent:
    return UsageEvent(
        provider_id="anthropic",
        account_id=account_id,
        sidecar_id="laptop",
        event_id=event_id,
        ts=datetime(2026, 9, 1, 10, tzinfo=UTC),
        kind="message",
        model_id="sonnet-4.5",
        tokens_input=tokens,
    )


def _index_exists(session: Session) -> bool:
    return (
        session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"), {"n": INDEX_NAME}
        ).first()
        is not None
    )


def test_migration_ranking_join_never_falls_back_to_a_provider_scan(session: Session):
    """Regression for a real production-scale stall: with no index covering
    (provider_id, event_id, account_id), the query planner's only option is
    the old 3-column uq_usage_events_identity (provider_id, account_id,
    event_id) — event_id isn't its second column, so a seek can only narrow
    by provider_id and falls back to comparing every row sharing it. On a
    provider with tens of thousands of events and duplicate groups that's a
    quadratic blowup that measured as never finishing in over ten minutes
    against a copy of a real database, versus under a second with the seek.

    Pins the query plan of the migration module's own SQL constants (not a
    copy re-typed here) so the fix can't silently stop being used.
    """
    session.execute(text(f"DROP INDEX {INDEX_NAME}"))  # pre-migration DB
    session.execute(text(_TMP_JOIN_INDEX_SQL))
    plan = " ".join(
        str(row) for row in session.execute(text(f"EXPLAIN QUERY PLAN {_RANKING_SELECT_SQL}"))
    )
    assert f"SEARCH e USING COVERING INDEX {_TMP_JOIN_INDEX}" in plan
    assert "provider_id=? AND event_id=?" in plan


def test_migration_creates_the_join_index_before_the_ranking_query(
    session: Session, monkeypatch: pytest.MonkeyPatch
):
    """The temp index only helps if it exists before the ranking query
    runs — pin that ordering so a future refactor can't reorder past it."""
    session.execute(text(f"DROP INDEX {INDEX_NAME}"))  # pre-migration DB
    for ev in (
        _legacy_event("default", "msg_1"),
        _legacy_event("alice@example.com", "msg_1"),
    ):
        session.add(ev)
        session.flush()
        update_rollups_for_event(session, ev)
    session.commit()

    statements: list[str] = []
    real_execute = Session.execute

    def _spy(self: Session, statement: object, *args: object, **kwargs: object) -> object:
        statements.append(str(statement))
        return real_execute(self, statement, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Session, "execute", _spy)

    migrate_to_provider_event_identity(session)

    index_pos = next(i for i, s in enumerate(statements) if _TMP_JOIN_INDEX_SQL in s)
    ranking_pos = next(
        i for i, s in enumerate(statements) if "CREATE TEMP TABLE _event_identity_ranked" in s
    )
    assert index_pos < ranking_pos


def test_migration_collapses_cross_account_duplicates(session: Session):
    session.execute(text(f"DROP INDEX {INDEX_NAME}"))  # pre-migration DB
    # msg_1 double-counted under default + alice (a retag); msg_2 only default.
    for ev in (
        _legacy_event("default", "msg_1"),
        _legacy_event("alice@example.com", "msg_1"),
        _legacy_event("default", "msg_2", tokens=5),
    ):
        session.add(ev)
        session.flush()
        update_rollups_for_event(session, ev)
    session.commit()
    assert _lifetime(session, "default") == (2, 105)

    removed = migrate_to_provider_event_identity(session)

    assert removed == 1
    rows = sorted((r.event_id, r.account_id) for r in session.exec(select(UsageEvent)))
    # The real account wins over "default"; the lone row is untouched.
    assert rows == [("msg_1", "alice@example.com"), ("msg_2", "default")]
    assert _lifetime(session, "alice@example.com") == (1, 100)
    assert _lifetime(session, "default") == (1, 5)
    assert _index_exists(session)

    # Idempotent: index present → no-op.
    assert migrate_to_provider_event_identity(session) == 0


def test_migration_is_noop_on_fresh_db(session: Session):
    assert _index_exists(session)  # create_all builds it from the model
    assert migrate_to_provider_event_identity(session) == 0


def test_migration_leaves_no_scaffolding_table_behind(session: Session):
    """The set-based collapse's temp ranking table (a Python loop per
    duplicate group added minutes to a database with tens of thousands of
    them) doesn't survive the migration."""
    session.execute(text(f"DROP INDEX {INDEX_NAME}"))  # pre-migration DB
    for ev in (
        _legacy_event("default", "msg_1"),
        _legacy_event("alice@example.com", "msg_1"),
    ):
        session.add(ev)
        session.flush()
        update_rollups_for_event(session, ev)
    session.commit()

    migrate_to_provider_event_identity(session)

    table_names = {
        row[0]
        for row in session.execute(text("SELECT name FROM sqlite_temp_master WHERE type='table'"))
    }
    assert "_event_identity_ranked" not in table_names
    assert _index_exists(session)


def test_migration_handles_a_three_way_duplicate_group(session: Session):
    """More than two copies of the same event (a message re-tagged twice)
    all collapse to the single correct keeper."""
    session.execute(text(f"DROP INDEX {INDEX_NAME}"))  # pre-migration DB
    for ev in (
        _legacy_event("default", "msg_1"),
        _legacy_event("bob@example.com", "msg_1"),
        _legacy_event("alice@example.com", "msg_1"),
    ):
        session.add(ev)
        session.flush()
        update_rollups_for_event(session, ev)
    session.commit()

    removed = migrate_to_provider_event_identity(session)

    assert removed == 2
    rows = list(session.exec(select(UsageEvent)))
    assert len(rows) == 1
    # The most recently inserted real account wins over an earlier real
    # account, same as it beats "default".
    assert rows[0].account_id == "alice@example.com"


# ---------------------------------------------------------------------------
# Sidecar: retagged events also advance the iterating provider's watermark
# ---------------------------------------------------------------------------


def test_retagged_events_register_watermark_alias(monkeypatch):
    import scripts.sidecar as sc

    evt = UsageEventPush(
        provider_id="minimax",  # retagged from opencode
        account_id="me@example.com",
        event_id="msg_abc",
        ts="2026-09-01T10:00:00Z",
        kind="message",
        model_id="minimax-m2",
    )
    monkeypatch.setattr(
        sc, "_make_account_extractor_opencode", lambda *_a, **_k: lambda *a, **k: [evt]
    )
    sc._EVENT_WATERMARK_ALIASES.clear()
    out: list[dict] = []
    sc._extract_events_for_provider(
        "opencode",
        ["default"],
        watermark=MagicMock(last_pushed=MagicMock(return_value=None)),
        bootstrap_days=90,
        out_events=out,
    )
    assert sc._EVENT_WATERMARK_ALIASES == {("minimax", "msg_abc"): ("opencode", "default")}
