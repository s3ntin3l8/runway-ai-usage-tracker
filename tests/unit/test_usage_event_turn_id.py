"""The ``turn_id`` dimension of a usage event.

A provider that emits one event per **request** (MiniMax Code) carries its own
turn identifier, so the Sessions view can count turns instead of requests. A
provider that emits one event per **message** (Claude Code) leaves it unset,
and the turn count falls back to the event count.
"""

from datetime import UTC, datetime

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, _add_columns_if_missing, configure_sqlite_engine
from app.models.db import UsageEvent
from app.models.schemas import UsageEventPush
from app.services.event_ingestor import EventIngestor

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _engine():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    return engine


def test_the_column_is_added_to_a_database_that_predates_it():
    """The migration is forward-safe: an old database gains the column."""
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE usage_events DROP COLUMN turn_id"))
        conn.commit()
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(usage_events)"))}
        assert "turn_id" not in cols

        _add_columns_if_missing(conn)

        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(usage_events)"))}
        assert "turn_id" in cols


def test_the_ingestor_stores_the_turn_id():
    engine = _engine()
    with Session(engine) as session:
        EventIngestor(session).ingest(
            [
                UsageEventPush(
                    provider_id="minimax",
                    account_id="acc1",
                    event_id="e1",
                    ts=NOW.isoformat(),
                    model_id="MiniMax-M3.1-Flash",
                    session_id="mvs_main",
                    turn_id="turn_task_bg_bg_1234",
                    source_app="minimax_code",
                )
            ],
            sidecar_id="dev-01",
        )
        row = session.exec(select(UsageEvent)).one()
        assert row.turn_id == "turn_task_bg_bg_1234"


def test_a_push_without_a_turn_id_leaves_it_unset():
    """A provider that emits one event per message stays on the fallback."""
    engine = _engine()
    with Session(engine) as session:
        EventIngestor(session).ingest(
            [
                UsageEventPush(
                    provider_id="anthropic",
                    account_id="acc1",
                    event_id="e1",
                    ts=NOW.isoformat(),
                    model_id="claude-sonnet-4-6",
                    session_id="parent_sess",
                    subagent_type="Explore",
                    source_app="claude_code",
                )
            ],
            sidecar_id="dev-01",
        )
        row = session.exec(select(UsageEvent)).one()
        assert row.turn_id is None
        assert row.subagent_type == "Explore"
