import logging
import os

from sqlalchemy import event
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


def configure_sqlite_engine(engine_) -> None:
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
    def _on_connect(dbapi_conn, _conn_record):
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
    def _emit_begin(conn):
        conn.exec_driver_sql("BEGIN")


engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=SQLITE_CONNECT_ARGS,
)
configure_sqlite_engine(engine)


def init_db():
    """Create database tables if they don't exist."""
    from app.models.db import (  # noqa: F401  ensures models are registered
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

    from app.services.pricing_seed import seed_pricing_table

    with Session(engine) as session:
        inserted = seed_pricing_table(session)
        if inserted:
            logger.info(f"Seeded provider_pricing with {inserted} new rows")

    # Performance indexes (idempotent)
    with engine.connect() as conn:
        _create_performance_indexes(conn)


_DEFERRED_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, sql_type_with_default)
    ("sidecar_registry", "collection_enabled", "BOOLEAN NOT NULL DEFAULT 1"),
    ("system_config", "user_timezone", "VARCHAR"),
    ("usage_events", "subagent_type", "VARCHAR"),
]


def _add_columns_if_missing(conn) -> None:
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


def _create_performance_indexes(conn):
    """Create composite indexes for history query optimization.

    These indexes dramatically speed up time-range queries with provider/account filters.
    Safe to call on every startup - CREATE INDEX IF NOT EXISTS is idempotent.
    The primary usage_events indexes are already declared in __table_args__; this
    function covers any additional indexes not captured by the ORM declarations.
    """
    indexes: list[str] = []
    for sql in indexes:
        try:
            conn.execute(__import__("sqlalchemy").text(sql))
            conn.commit()
        except Exception as e:
            logger.debug(f"Could not create performance index (may already exist): {e}")


def get_session():
    """FastAPI dependency for DB session."""
    with Session(engine) as session:
        yield session
