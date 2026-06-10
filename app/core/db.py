import logging
import os
from collections.abc import Iterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings

logger = logging.getLogger(__name__)

# Ensure data directory exists
db_dir = os.path.dirname(settings.DATABASE_PATH)
if not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create database directory {db_dir}: {e}")

SQLITE_CONNECT_ARGS = {
    # check_same_thread=False: required so FastAPI's worker threads can
    # share the connection pool.
    "check_same_thread": False,
    # SQLite is single-writer; busy_timeout makes concurrent writers wait for
    # the lock instead of failing immediately with "database is locked".
    "timeout": 30.0,
}


def configure_sqlite_engine(engine_: Engine) -> None:
    """Wire up the SQLAlchemy-recommended pattern for proper SQLite
    transaction semantics.

    Three things happen at connect time, in order:

    1. `isolation_level=None` puts the DBAPI connection in autocommit so
       SQLAlchemy controls BEGIN/COMMIT/ROLLBACK. Without this, Python's
       sqlite3 silently auto-commits on certain statements and SQLAlchemy's
       ROLLBACK has nothing to roll back — partial-batch failures leak.
    2. WAL journal mode and synchronous=NORMAL are applied per-connection
       before any transaction is open. WAL lets readers proceed alongside
       a writer; without it, every read serialises through the same lock
       as every write, and BEGIN times out under fleet contention.
    3. The "begin" event emits plain `BEGIN` (DEFERRED). Combined with WAL
       this is correct: writers serialise through SQLite's single writer
       lock with busy_timeout retry, readers use snapshots and never
       conflict. BEGIN IMMEDIATE would serialise reads as well — a 30s
       timeout is not enough margin for a healthy dashboard polling loop.
    """

    @event.listens_for(engine_, "connect")
    def _on_connect(dbapi_conn: Any, _conn_record: Any) -> None:
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        try:
            # `journal_mode=WAL` returns the journal mode actually in effect
            # ("memory" for :memory: DBs, "wal" for files). Either way it's
            # safe to set on every connect — it's a no-op once enabled.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:
            logger.warning(f"Could not set SQLite concurrency pragmas: {e}")
        finally:
            cursor.close()

    @event.listens_for(engine_, "begin")
    def _emit_begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")


engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=SQLITE_CONNECT_ARGS,
)
configure_sqlite_engine(engine)


def init_db() -> None:
    """Create database tables if they don't exist."""
    from app.models.db import (  # noqa: F401  ensures models are registered
        AuditLog,
        LatestUsage,
        ProviderConfig,
        ProviderPricing,
        QuotaSnapshot,
        SidecarRegistry,
        SystemConfig,
        UsageEvent,
        UsagePeriodRollup,
        UsageWindow,
        WebhookConfig,
    )

    # Concurrency pragmas (WAL / synchronous=NORMAL) are applied per-connection
    # in the "connect" event listener — they must run before any BEGIN, which
    # rules out doing them through SQLAlchemy's normal execute path.

    SQLModel.metadata.create_all(engine)
    logger.info(f"Database initialized at {settings.DATABASE_PATH}")

    # Add columns introduced after initial schema (SQLite create_all doesn't ALTER)
    with engine.connect() as conn:
        _add_columns_if_missing(conn)
        _add_indexes_if_missing(conn)
        _rebuild_quota_snapshot_indexes(conn)
        _backfill_quota_snapshot_variant(conn)

    from app.services.pricing_seed import seed_pricing_table

    with Session(engine) as session:
        inserted = seed_pricing_table(session)
        if inserted:
            logger.info(f"Seeded provider_pricing with {inserted} new rows")


_DEFERRED_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, sql_type_with_default)
    ("sidecar_registry", "collection_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
    ("system_config", "user_timezone", "VARCHAR"),
    ("system_config", "sidecar_update_channel", "VARCHAR"),
    ("usage_events", "subagent_type", "VARCHAR"),
    ("quota_snapshots", "variant", "TEXT NOT NULL DEFAULT ''"),
    # oai-sc: OpenAI service-credential cookie required by chatgpt.com/api/auth/session
    ("provider_configs", "oai_sc_cookie_encrypted", "VARCHAR"),
]


_DEFERRED_INDEXES: list[tuple[str, str, str]] = [
    # index_name, table, comma-separated columns
    # Note: ix_quota_snapshots_series_ts is handled by _rebuild_quota_snapshot_indexes
    # so it can be rebuilt with variant included on existing databases.
]


def _add_indexes_if_missing(conn: Any) -> None:
    """Idempotently CREATE INDEX IF NOT EXISTS for indexes that postdate
    initial schema creation. SQLModel.create_all() only adds indexes for
    fresh tables, so existing databases miss newer indexes declared in
    __table_args__.
    """
    from sqlalchemy import text

    for name, table, cols in _DEFERRED_INDEXES:
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))
        conn.commit()


def _rebuild_quota_snapshot_indexes(conn: Any) -> None:
    """Rebuild quota_snapshot indexes to include the variant column.

    The unique constraint and series_ts index both need variant in their
    key. On existing databases the column was just added by _add_columns_if_missing;
    the old indexes must be dropped and recreated with the new column list.
    On fresh databases create_all() builds the correct indexes from __table_args__
    so PRAGMA index_info won't find variant missing and this is a no-op.
    """
    from sqlalchemy import text

    # Check that variant column exists before touching indexes.
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(quota_snapshots)"))}
    if "variant" not in cols:
        return

    _QUOTA_SNAPSHOT_INDEXES = [
        (
            "uq_quota_snapshots_identity",
            "CREATE UNIQUE INDEX uq_quota_snapshots_identity ON quota_snapshots "
            "(provider_id, account_id, window_type, variant, model_id, ts)",
        ),
        (
            "ix_quota_snapshots_series_ts",
            "CREATE INDEX ix_quota_snapshots_series_ts ON quota_snapshots "
            "(provider_id, account_id, window_type, variant, model_id, ts)",
        ),
    ]
    for index_name, create_sql in _QUOTA_SNAPSHOT_INDEXES:
        # Check whether the existing index already covers variant.
        index_cols = [row[2] for row in conn.execute(text(f"PRAGMA index_info('{index_name}')"))]
        if "variant" in index_cols:
            continue
        conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        conn.execute(text(create_sql))
        conn.commit()
        logger.info("Migrated: rebuilt %s to include variant", index_name)


def _backfill_quota_snapshot_variant(conn: Any) -> None:
    """Rewrite quota_snapshots rows written with variant='default' to variant=''.

    The accumulator previously stored the absent-variant sentinel as "default"
    while the forecast read path filters for "". This one-time UPDATE aligns
    historical rows with the column default and the read side expectation.
    OR IGNORE handles the (extremely unlikely) duplicate on the unique key.
    """
    from sqlalchemy import text

    result = conn.execute(
        text("UPDATE OR IGNORE quota_snapshots SET variant = '' WHERE variant = 'default'")
    )
    conn.commit()
    if result.rowcount:
        logger.info(
            "Migrated: backfilled %d quota_snapshots rows (variant '' <- 'default')",
            result.rowcount,
        )


def _add_columns_if_missing(conn: Any) -> None:
    """Idempotently ALTER TABLE ... ADD COLUMN for fields that postdate
    initial schema creation. SQLModel.create_all() only creates new tables
    on existing SQLite databases — it never adds new columns.

    Only the "duplicate column" race is swallowed silently; any other
    failure is re-raised so genuine schema drift is loud.
    """
    import sqlalchemy.exc
    from sqlalchemy import text

    for table, column, sql_type in _DEFERRED_COLUMNS:
        cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        if column in cols:
            continue
        try:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
            conn.commit()
            logger.info(f"Migrated: added {table}.{column}")
        except sqlalchemy.exc.OperationalError as e:
            if "duplicate column" in str(e).lower():
                continue
            raise


def get_session() -> Iterator[Session]:
    """FastAPI dependency for DB session."""
    with Session(engine) as session:
        yield session
