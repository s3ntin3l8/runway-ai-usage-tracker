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

    Python's sqlite3 driver in its default ("legacy") mode silently auto-commits
    on certain statements (CREATE TABLE, etc.) and may not issue BEGIN for
    plain INSERT, so SQLAlchemy's ROLLBACK has nothing to roll back. The
    documented fix is: set the DBAPI connection to autocommit (None), then
    have SQLAlchemy emit an explicit BEGIN at the start of each transaction.
    The end result is that BEGIN / COMMIT / ROLLBACK / SAVEPOINT all work
    as advertised — without this, partial-batch failures leak through.
    """

    @event.listens_for(engine_, "connect")
    def _set_sqlite_autocommit(dbapi_conn, _conn_record):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine_, "begin")
    def _emit_begin(conn):
        # IMMEDIATE acquires the RESERVED lock at BEGIN time. The DEFERRED
        # default upgrades on first write, which lets two concurrent writers
        # both reach SHARED and then deadlock during upgrade — SQLite returns
        # BUSY immediately without honoring busy_timeout. IMMEDIATE makes the
        # second writer wait the full busy_timeout instead.
        conn.exec_driver_sql("BEGIN IMMEDIATE")


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

    # WAL allows concurrent readers alongside a writer; synchronous=NORMAL
    # is safe under WAL and substantially faster than the FULL default.
    with engine.connect() as conn:
        _enable_concurrency_pragmas(conn)

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


def _enable_concurrency_pragmas(conn) -> None:
    """Set WAL journal mode and NORMAL synchronous on the connection.

    WAL lets readers and one writer proceed concurrently; the old rollback
    journal serialised everything. synchronous=NORMAL is safe under WAL —
    a crash can lose the last in-flight transaction but never corrupts the
    DB — and is materially faster than FULL.

    In-memory SQLite (used by tests) refuses journal_mode changes, so we
    silently skip them there.
    """
    from sqlalchemy import text

    if (
        settings.DATABASE_URL.startswith("sqlite:///:memory:")
        or settings.DATABASE_URL == "sqlite://"
    ):
        return
    try:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not set concurrency pragmas: {e}")


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
