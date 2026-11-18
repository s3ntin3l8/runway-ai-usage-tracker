"""The ``query_source`` dimension of a usage event.

Claude Code's OpenTelemetry ``api_request`` events name each call's type in
``query_source`` (``agent_summary``, ``compact``, ``prompt_suggestion``,
``hook_prompt``, ``away_summary``, ``generate_session_title``,
``repl_main_thread``, ``agent:builtin:Explore``, ...). Stored separately from
``subagent_type``, which the Sessions views already group on.
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
        conn.execute(text("ALTER TABLE usage_events DROP COLUMN query_source"))
        conn.commit()
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(usage_events)"))}
        assert "query_source" not in cols

        _add_columns_if_missing(conn)

        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(usage_events)"))}
        assert "query_source" in cols


def test_the_ingestor_stores_the_query_source():
    engine = _engine()
    with Session(engine) as session:
        EventIngestor(session).ingest(
            [
                UsageEventPush(
                    provider_id="anthropic",
                    account_id="acc1",
                    event_id="e1",
                    ts=NOW.isoformat(),
                    model_id="claude-haiku-4-5",
                    session_id="sess_1",
                    query_source="agent_summary",
                    source_app="claude_code",
                )
            ],
            sidecar_id="dev-01",
        )
        row = session.exec(select(UsageEvent)).one()
        assert row.query_source == "agent_summary"


def test_a_push_without_a_query_source_leaves_it_unset():
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
                    session_id="sess_1",
                    source_app="claude_code",
                )
            ],
            sidecar_id="dev-01",
        )
        row = session.exec(select(UsageEvent)).one()
        assert row.query_source is None


def test_overlong_and_non_printable_query_source_is_bounded_or_dropped():
    engine = _engine()
    with Session(engine) as session:
        EventIngestor(session).ingest(
            [
                UsageEventPush(
                    provider_id="anthropic",
                    account_id="acc1",
                    event_id="e-bounded",
                    ts=NOW.isoformat(),
                    query_source="agent\n_summary\x00" + "x" * 100,
                    source_app="claude_code",
                ),
                UsageEventPush(
                    provider_id="anthropic",
                    account_id="acc1",
                    event_id="e-empty",
                    ts=NOW.isoformat(),
                    query_source="   \n\x00\t  ",
                    source_app="claude_code",
                ),
            ],
            sidecar_id="dev-01",
        )
        rows = {r.event_id: r for r in session.exec(select(UsageEvent)).all()}
        assert rows["e-bounded"].query_source == ("agent_summary" + "x" * 100)[:64]
        assert len(rows["e-bounded"].query_source or "") == 64
        assert rows["e-empty"].query_source is None


def test_reattribution_refreshes_query_source():
    engine = _engine()
    with Session(engine) as session:
        ingestor = EventIngestor(session)
        ingestor.ingest(
            [
                UsageEventPush(
                    provider_id="anthropic",
                    account_id="acc1",
                    event_id="e-reattr",
                    ts=NOW.isoformat(),
                    query_source="compact",
                    source_app="claude_code",
                )
            ],
            sidecar_id="dev-01",
        )
        ingestor.ingest(
            [
                UsageEventPush(
                    provider_id="anthropic",
                    account_id="acc2",
                    event_id="e-reattr",
                    ts=NOW.isoformat(),
                    query_source="hook_prompt",
                    source_app="claude_code",
                )
            ],
            sidecar_id="dev-01",
        )
        row = session.exec(select(UsageEvent)).one()
        assert row.account_id == "acc2"
        assert row.query_source == "hook_prompt"
