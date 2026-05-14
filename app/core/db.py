import logging
import os

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

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},  # Needed for SQLite + FastAPI
)


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
    """
    from sqlalchemy import text

    for table, column, sql_type in _DEFERRED_COLUMNS:
        try:
            cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column in cols:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
            conn.commit()
            logger.info(f"Migrated: added {table}.{column}")
        except Exception as e:
            logger.warning(f"Could not add column {table}.{column}: {e}")


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
