"""The ``request_id`` dimension of a usage event.

Claude Code logs ``requestId`` at the top level of the assistant transcript
record rather than inside ``message``. Carrying it as its own field preserves
existing ``(provider_id, event_id)`` deduplication keys while giving side-call
telemetry a join key.
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
        conn.execute(text("ALTER TABLE usage_events DROP COLUMN request_id"))
        conn.commit()
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(usage_events)"))}
        assert "request_id" not in cols

        _add_columns_if_missing(conn)

        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(usage_events)"))}
        assert "request_id" in cols


def test_the_ingestor_stores_the_request_id():
    engine = _engine()
    with Session(engine) as session:
        EventIngestor(session).ingest(
            [
                UsageEventPush(
                    provider_id="anthropic",
                    account_id="acc1",
                    event_id="msg_1",
                    request_id="req_abc123",
                    ts=NOW.isoformat(),
                    model_id="claude-sonnet-4-6",
                    session_id="sess_1",
                    source_app="claude_code",
                )
            ],
            sidecar_id="dev-01",
        )
        row = session.exec(select(UsageEvent)).one()
        assert row.request_id == "req_abc123"


def test_a_push_without_a_request_id_leaves_it_unset():
    engine = _engine()
    with Session(engine) as session:
        EventIngestor(session).ingest(
            [
                UsageEventPush(
                    provider_id="anthropic",
                    account_id="acc1",
                    event_id="msg_2",
                    ts=NOW.isoformat(),
                    model_id="claude-sonnet-4-6",
                    session_id="sess_1",
                    source_app="claude_code",
                )
            ],
            sidecar_id="dev-01",
        )
        row = session.exec(select(UsageEvent)).one()
        assert row.request_id is None
