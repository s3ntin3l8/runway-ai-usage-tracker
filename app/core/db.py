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
    # Import all models here so they are registered with SQLModel.metadata
    from app.models.db import (  # noqa: F401
        ProviderConfig,
        SidecarRegistry,
        SystemConfig,
        UsageSnapshot,
        WebhookConfig,
    )

    try:
        SQLModel.metadata.create_all(engine)
        logger.info(f"Database initialized at {settings.DATABASE_PATH}")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

    # Additive column migrations (SQLite doesn't support DROP COLUMN, only ADD COLUMN)
    _run_migrations()


def _run_migrations():
    """Apply additive schema migrations that CREATE TABLE won't cover."""
    migrations = [
        # system_config gained default_poll_interval_seconds after initial release
        "ALTER TABLE system_config ADD COLUMN default_poll_interval_seconds INTEGER",
        # system_config gained local_collector / credential toggles
        "ALTER TABLE system_config ADD COLUMN local_collector_enabled INTEGER",
        "ALTER TABLE system_config ADD COLUMN local_credential_scraping_enabled INTEGER",
        # provider_configs: browser_preference column removed from model but kept in DB for compatibility
        # provider_configs: separate session cookie storage (distinct from API key)
        "ALTER TABLE provider_configs ADD COLUMN session_cookie_encrypted TEXT",
        # SystemConfig gained dashboard_layout_json (user-reorder persistence)
        "ALTER TABLE system_config ADD COLUMN dashboard_layout_json TEXT",
        # SidecarRegistry: sidecar app version + host OS reported on each ingest
        "ALTER TABLE sidecar_registry ADD COLUMN sidecar_version TEXT",
        "ALTER TABLE sidecar_registry ADD COLUMN os_platform TEXT",
        "ALTER TABLE sidecar_registry ADD COLUMN recent_logs TEXT",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(__import__("sqlalchemy").text(sql))
                conn.commit()
            except Exception:
                pass  # Column already exists or table doesn't exist yet — both are fine


def get_session():
    """FastAPI dependency for DB session."""
    with Session(engine) as session:
        yield session
