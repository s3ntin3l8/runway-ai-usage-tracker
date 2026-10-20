"""The ``source_app`` dimension — which client app produced a usage event.

``provider_id`` names the upstream provider a request was billed to, and
several client apps share one provider (MiniMax Code, OpenCode and a plain API
script can all spend the same MiniMax plan). ``source_app`` names the client
that made the call, so a canonical-provider retag can't erase it.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core import source_apps
from app.core.db import SQLITE_CONNECT_ARGS, _backfill_source_app, configure_sqlite_engine
from app.models.db import UsageEvent
from app.models.schemas import UsageEventPush
from app.services.event_ingestor import EventIngestor
from app.services.pricing_seed import seed_pricing_table
from app.services.queries.source_apps import query_source_apps
from app.services.queries.windows import query_window_aggregation

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

# The client app each bundled extractor reports. The behavioural assertions
# live in the per-extractor test files; this table is the contract they and
# any future extractor must satisfy.
EXTRACTOR_SOURCE_APPS = {
    "anthropic": source_apps.CLAUDE_CODE,
    "antigravity": source_apps.ANTIGRAVITY,
    "chatgpt": source_apps.CODEX,
    "gemini": source_apps.GEMINI_CLI,
    "hermes": source_apps.HERMES,
    "opencode": source_apps.OPENCODE,
    "xai": source_apps.XAI_CLI,
}


@pytest.mark.parametrize(("extractor", "expected"), sorted(EXTRACTOR_SOURCE_APPS.items()))
def test_every_extractor_stamps_its_source_app(extractor, expected):
    """No extractor may push events without naming the client app.

    ``provider_id`` says who was billed; without ``source_app`` the app that
    made the call is lost the moment the provider is canonicalised. A new
    extractor that forgets the kwarg would silently fall back to the provider
    id at ingest, so the contract is asserted here for the whole package.
    """
    import importlib
    import inspect

    module = importlib.import_module(f"scripts.sidecar_pkg.event_extractors.{extractor}")
    constant = next(
        name for name, value in vars(source_apps).items() if name.isupper() and value == expected
    )
    assert f"source_app={constant}" in inspect.getsource(module)


def _session():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    s = Session(engine)
    seed_pricing_table(s)
    return s


def _push(event_id: str, **kw):
    base = {
        "provider_id": "minimax",
        "account_id": "acc-1",
        "event_id": event_id,
        "ts": NOW.isoformat(),
        "model_id": "MiniMax-M3",
        "tokens_input": 100,
        "tokens_output": 50,
    }
    base.update(kw)
    return UsageEventPush(**base)


def test_ingest_stores_the_reported_source_app():
    s = _session()
    EventIngestor(s).ingest([_push("e1", source_app="minimax_code")], sidecar_id="dev-01")
    row = s.exec(select(UsageEvent)).first()
    assert row is not None
    assert row.source_app == "minimax_code"
    # The provider is unchanged — source_app adds a dimension, it doesn't
    # replace one.
    assert row.provider_id == "minimax"


def test_ingest_falls_back_to_provider_id_without_a_source_app():
    s = _session()
    EventIngestor(s).ingest([_push("e1")], sidecar_id="dev-01")
    row = s.exec(select(UsageEvent)).first()
    assert row is not None
    assert row.source_app == "minimax"


def test_error_events_also_carry_the_source_app():
    s = _session()
    EventIngestor(s).ingest(
        [_push("e1", kind="error", error_reason="boom", source_app="opencode")],
        sidecar_id="dev-01",
    )
    row = s.exec(select(UsageEvent)).first()
    assert row is not None
    assert row.source_app == "opencode"


def _insert_legacy_row(engine, event_id: str, source_app: str | None) -> None:
    """Write a usage_events row directly, as the pre-column table had it.

    ``source_app`` is None for a row written before the column existed; the
    backfill's job is exactly to fill those.
    """
    with Session(engine) as s:
        s.add(
            UsageEvent(
                provider_id="minimax",
                account_id="acc-1",
                event_id=event_id,
                ts=NOW,
                kind="message",
                sidecar_id="dev-01",
                attribution_source="local",
                source_app=source_app,
                tokens_input=1,
                tokens_output=1,
            )
        )
        s.commit()


def _engine():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    return engine


def test_backfill_gives_pre_column_rows_the_provider_id():
    engine = _engine()
    _insert_legacy_row(engine, "old", None)
    with engine.connect() as conn:
        _backfill_source_app(conn)
        row = conn.execute(text("SELECT source_app FROM usage_events")).first()
    assert row is not None
    assert row[0] == "minimax"


def test_backfill_leaves_reported_apps_alone():
    engine = _engine()
    _insert_legacy_row(engine, "new", "minimax_code")
    with engine.connect() as conn:
        _backfill_source_app(conn)
        row = conn.execute(text("SELECT source_app FROM usage_events")).first()
    assert row is not None
    assert row[0] == "minimax_code"


def test_existing_db_is_upgraded_in_place():
    """A database that predates the column is upgraded the way init_db does it:
    the column is added, the index created, and the pre-column rows backfilled
    to the provider id."""
    from app.core.db import _add_columns_if_missing, _add_indexes_if_missing

    engine = _engine()
    _insert_legacy_row(engine, "old", "minimax_code")

    with engine.connect() as conn:
        # Pretend this database was created before the column existed (the
        # index goes first — SQLite refuses to drop an indexed column).
        conn.execute(text("DROP INDEX IF EXISTS ix_usage_events_source_app_ts"))
        conn.execute(text("ALTER TABLE usage_events DROP COLUMN source_app"))
        conn.commit()

        _add_columns_if_missing(conn)
        _add_indexes_if_missing(conn)
        _backfill_source_app(conn)

        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(usage_events)"))}
        assert "source_app" in cols

        index_cols = [
            r[2] for r in conn.execute(text("PRAGMA index_info('ix_usage_events_source_app_ts')"))
        ]
        assert index_cols == ["source_app", "ts"]

        row = conn.execute(text("SELECT source_app FROM usage_events")).first()
    assert row is not None
    assert row[0] == "minimax"


def test_query_source_apps_groups_by_client_app():
    s = _session()
    ingestor = EventIngestor(s)
    ingestor.ingest(
        [
            _push("e1", source_app="minimax_code", tokens_input=100, tokens_output=10),
            _push("e2", source_app="minimax_code", tokens_input=200, tokens_output=20),
            _push("e3", source_app="opencode", tokens_input=50, tokens_output=5),
        ],
        sidecar_id="dev-01",
    )
    apps = query_source_apps(s, since=NOW - timedelta(days=1))
    by_app = {a["source_app"]: a for a in apps}
    assert set(by_app) == {"minimax_code", "opencode"}
    assert by_app["minimax_code"]["tokens_input"] == 300
    assert by_app["minimax_code"]["msgs"] == 2
    assert by_app["opencode"]["tokens_input"] == 50


def test_query_source_apps_filters_by_provider_and_account():
    s = _session()
    EventIngestor(s).ingest(
        [
            _push("e1", source_app="minimax_code"),
            _push("e2", provider_id="anthropic", account_id="other", source_app="claude_code"),
        ],
        sidecar_id="dev-01",
    )
    apps = query_source_apps(
        s, since=NOW - timedelta(days=1), provider_id="minimax", account_id="acc-1"
    )
    assert [a["source_app"] for a in apps] == ["minimax_code"]


def test_query_source_apps_ignores_error_rows():
    s = _session()
    EventIngestor(s).ingest(
        [_push("e1", kind="error", error_reason="boom", source_app="minimax_code")],
        sidecar_id="dev-01",
    )
    assert query_source_apps(s, since=NOW - timedelta(days=1)) == []


def test_window_aggregation_splits_by_source_app():
    s = _session()
    EventIngestor(s).ingest(
        [
            _push("e1", source_app="minimax_code", ts=(NOW - timedelta(hours=1)).isoformat()),
            _push("e2", source_app="opencode", ts=(NOW - timedelta(hours=1)).isoformat()),
        ],
        sidecar_id="dev-01",
    )
    agg = query_window_aggregation(
        s,
        provider_id="minimax",
        account_id="acc-1",
        window_type="session",
        reset_at=NOW,
    )
    assert set(agg["by_source_app"]) == {"minimax_code", "opencode"}
    # The provider-level totals still cover every app.
    assert agg["token_usage"]["input"] == 200
