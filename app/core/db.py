import logging
import os
import re

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
        # provider_configs: separate session cookie storage (distinct from API key)
        "ALTER TABLE provider_configs ADD COLUMN session_cookie_encrypted TEXT",
        # SystemConfig gained dashboard_layout_json (user-reorder persistence)
        "ALTER TABLE system_config ADD COLUMN dashboard_layout_json TEXT",
        # SidecarRegistry: sidecar app version + host OS reported on each ingest
        "ALTER TABLE sidecar_registry ADD COLUMN sidecar_version TEXT",
        "ALTER TABLE sidecar_registry ADD COLUMN os_platform TEXT",
        "ALTER TABLE sidecar_registry ADD COLUMN recent_logs TEXT",
        # provider_configs: user-configurable data collection strategy ordering/toggles
        "ALTER TABLE provider_configs ADD COLUMN collection_strategies_json TEXT",
        # usage_snapshots: per-card disambiguator under same (provider, account, model_id, window_type)
        "ALTER TABLE usage_snapshots ADD COLUMN variant TEXT",
        # latest_usage gained model_id in the identity tuple — collectors emit
        # multiple cards for the same window with different models (Claude
        # Sonnet vs Claude Design), and the original constraint without
        # model_id collided.
        "ALTER TABLE latest_usage ADD COLUMN model_id TEXT NOT NULL DEFAULT ''",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(__import__("sqlalchemy").text(sql))
                conn.commit()
            except Exception:
                pass  # Column already exists or table doesn't exist yet — both are fine

        # latest_usage: ensure the unique constraint includes model_id.
        # SQLite cannot DROP an embedded UNIQUE constraint — only way to change it is
        # to rename the table, create a replacement, copy data, then drop the old.
        try:
            sa = __import__("sqlalchemy").text
            row = conn.execute(
                sa("SELECT sql FROM sqlite_master WHERE type='table' AND name='latest_usage'")
            ).first()
            if row and row[0]:
                old_sql: str = row[0]
                # Recreate if the embedded UNIQUE constraint omits model_id.
                # model_id may appear as a column definition but not in the constraint tuple.
                m = re.search(r"CONSTRAINT\s+\S+\s+UNIQUE\s*\(([^)]+)\)", old_sql, re.IGNORECASE)
                constraint_cols = m.group(1) if m else ""
                needs_recreate = bool(m) and "model_id" not in constraint_cols
                if needs_recreate:
                    logger.info("Recreating latest_usage to add model_id to unique constraint")
                    conn.execute(sa("ALTER TABLE latest_usage RENAME TO latest_usage_old"))
                    conn.execute(
                        sa(
                            """
                            CREATE TABLE latest_usage (
                                id INTEGER PRIMARY KEY,
                                provider_id TEXT NOT NULL,
                                account_id TEXT NOT NULL,
                                sidecar_id TEXT NOT NULL DEFAULT 'local',
                                window_type TEXT NOT NULL DEFAULT 'unknown',
                                variant TEXT NOT NULL DEFAULT 'default',
                                model_id TEXT NOT NULL DEFAULT '',
                                card_json TEXT NOT NULL,
                                updated_at TIMESTAMP,
                                CONSTRAINT uq_latest_usage_identity UNIQUE
                                    (provider_id, account_id, sidecar_id, window_type, variant, model_id)
                            )
                            """
                        )
                    )
                    conn.execute(
                        sa(
                            "INSERT OR IGNORE INTO latest_usage "
                            "  (id, provider_id, account_id, sidecar_id, window_type, "
                            "   variant, model_id, card_json, updated_at) "
                            "SELECT id, provider_id, account_id, sidecar_id, window_type, "
                            "       variant, COALESCE(model_id, '') as model_id, "
                            "       card_json, updated_at "
                            "FROM latest_usage_old"
                        )
                    )
                    conn.execute(sa("DROP TABLE latest_usage_old"))
                    conn.commit()
                    logger.info("latest_usage recreated with model_id in unique constraint")
        except Exception as e:
            logger.error(f"latest_usage constraint migration failed: {e}")

        # Drop legacy cumulative_usage unit_types (percent/currency/credits).
        # Sidecar now emits only tokens_*/cost_usd; old rows are diagnostic noise.
        try:
            result = conn.execute(
                __import__("sqlalchemy").text(
                    "DELETE FROM cumulative_usage "
                    "WHERE unit_type IN ('percent', 'currency', 'credits')"
                )
            )
            if result.rowcount:
                logger.info(
                    f"Cleared {result.rowcount} legacy cumulative_usage rows "
                    f"(percent/currency/credits)"
                )
            conn.commit()
        except Exception as e:
            logger.debug(f"cumulative_usage legacy cleanup skipped: {e}")

        # Pre-release: refuse to boot if the legacy `window_label` column is still present.
        # The schema rework removed it; carrying both columns silently would split aggregation.
        # Wipe `data/runway.db` to continue (no production users yet).
        try:
            row = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT 1 FROM pragma_table_info('usage_snapshots') WHERE name = 'window_label'"
                )
            ).first()
            if row is not None:
                raise RuntimeError(
                    "usage_snapshots.window_label column found — schema was reworked to use "
                    "`variant` instead. Pre-release: wipe `data/runway.db` and restart."
                )
        except RuntimeError:
            raise
        except Exception:
            # pragma_table_info isn't available or the table doesn't exist yet — both are fine
            pass

        # Performance indexes for history queries (safe to create multiple times)
        _create_performance_indexes(conn)


def _create_performance_indexes(conn):
    """Create composite indexes for history query optimization.

    These indexes dramatically speed up time-range queries with provider/account filters.
    Safe to call on every startup - CREATE INDEX IF NOT EXISTS is idempotent.
    """
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_snapshot_provider_account_ts ON usage_snapshots(provider_id, account_id, timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_snapshot_provider_ts ON usage_snapshots(provider_id, timestamp)",
    ]
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
