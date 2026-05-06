# tests/integration/test_db_migration.py
"""Tests for DB schema migrations that remove sidecar_id from unique constraints."""

import os
import re
import tempfile

import pytest
import sqlalchemy
from sqlmodel import SQLModel, create_engine

from app.core.db import (
    _migrate_cumulative_usage_remove_sidecar_id,
    _migrate_latest_usage_remove_sidecar_id,
)

# Import models so they register with SQLModel.metadata
from app.models.db import CumulativeUsage, LatestUsage  # noqa: F401

sa = sqlalchemy.text

OLD_LATEST_USAGE_DDL = """
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

OLD_CUMULATIVE_USAGE_DDL = """
CREATE TABLE cumulative_usage (
    id INTEGER PRIMARY KEY,
    provider_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    sidecar_id TEXT NOT NULL DEFAULT 'local',
    period_type TEXT NOT NULL,
    period_key TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    total_value REAL NOT NULL DEFAULT 0.0,
    last_updated TIMESTAMP,
    CONSTRAINT uq_cumulative_usage_identity UNIQUE
        (provider_id, account_id, sidecar_id, period_type, period_key, unit_type)
)
"""


def _make_temp_engine():
    """Create a temporary SQLite engine backed by a temp file."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return engine, db_path


def _constraint_cols_for(conn, table_name: str) -> str:
    """Return the column list inside the UNIQUE constraint for `table_name`, or ''."""
    row = conn.execute(
        sa("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {"name": table_name}
    ).first()
    if not row or not row[0]:
        return ""
    m = re.search(r"CONSTRAINT\s+\S+\s+UNIQUE\s*\(([^)]+)\)", row[0], re.IGNORECASE)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Test 1: latest_usage migration removes sidecar_id from constraint
# ---------------------------------------------------------------------------


class TestMigrateLatestUsageRemoveSidecarId:
    def setup_method(self):
        self.engine, self.db_path = _make_temp_engine()

    def teardown_method(self):
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_migration_removes_sidecar_id_from_constraint(self):
        with self.engine.connect() as conn:
            # Create table with OLD schema (sidecar_id in UNIQUE constraint)
            conn.execute(sa(OLD_LATEST_USAGE_DDL))
            conn.commit()

            # Insert two rows that differ only by sidecar_id
            conn.execute(
                sa(
                    "INSERT INTO latest_usage "
                    "(provider_id, account_id, sidecar_id, window_type, variant, model_id, card_json) "
                    "VALUES ('anthropic', 'default', 'local', 'weekly', 'default', '', '{}')"
                )
            )
            conn.execute(
                sa(
                    "INSERT INTO latest_usage "
                    "(provider_id, account_id, sidecar_id, window_type, variant, model_id, card_json) "
                    "VALUES ('anthropic', 's3ntin3l8@gmail.com', 'dev-01', 'weekly', 'default', '', '{}')"
                )
            )
            conn.commit()

            # Run migration
            result = _migrate_latest_usage_remove_sidecar_id(conn)

        assert result is True

        # Verify post-migration state in a new connection
        with self.engine.connect() as conn:
            # Table still exists
            row = conn.execute(
                sa("SELECT name FROM sqlite_master WHERE type='table' AND name='latest_usage'")
            ).first()
            assert row is not None, "latest_usage table should still exist"

            # sidecar_id is NOT in the UNIQUE constraint
            constraint_cols = _constraint_cols_for(conn, "latest_usage")
            assert constraint_cols, "UNIQUE constraint should still exist"
            assert "sidecar_id" not in constraint_cols, (
                f"sidecar_id should be removed from UNIQUE constraint, got: {constraint_cols!r}"
            )
            # The expected columns are present
            for col in ("provider_id", "account_id", "window_type", "variant", "model_id"):
                assert col in constraint_cols, f"{col} should be in UNIQUE constraint"

            # Table is empty (drop-and-recreate strategy)
            count = conn.execute(sa("SELECT COUNT(*) FROM latest_usage")).scalar()
            assert count == 0, "latest_usage should be empty after drop-and-recreate migration"

            # sidecar_id column still exists as a regular column
            pragma = conn.execute(sa("PRAGMA table_info(latest_usage)")).fetchall()
            col_names = [r[1] for r in pragma]
            assert "sidecar_id" in col_names, "sidecar_id column should still be present"

    def test_second_call_is_noop(self):
        with self.engine.connect() as conn:
            conn.execute(sa(OLD_LATEST_USAGE_DDL))
            conn.commit()

            first = _migrate_latest_usage_remove_sidecar_id(conn)
            assert first is True

            # Second call: already migrated, should be a no-op
            second = _migrate_latest_usage_remove_sidecar_id(conn)
            assert second is False


# ---------------------------------------------------------------------------
# Test 2: cumulative_usage migration removes sidecar_id and sums collisions
# ---------------------------------------------------------------------------


class TestMigrateCumulativeUsageRemoveSidecarId:
    def setup_method(self):
        self.engine, self.db_path = _make_temp_engine()

    def teardown_method(self):
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_migration_sums_collisions_and_removes_sidecar_id(self):
        with self.engine.connect() as conn:
            conn.execute(sa(OLD_CUMULATIVE_USAGE_DDL))
            conn.commit()

            # Two rows: same logical identity, different sidecar_ids
            conn.execute(
                sa(
                    "INSERT INTO cumulative_usage "
                    "(provider_id, account_id, sidecar_id, period_type, period_key, unit_type, total_value) "
                    "VALUES ('anthropic', 's3ntin3l8@gmail.com', 'dev-01', 'lifetime', 'all', 'tokens_input', 500000.0)"
                )
            )
            conn.execute(
                sa(
                    "INSERT INTO cumulative_usage "
                    "(provider_id, account_id, sidecar_id, period_type, period_key, unit_type, total_value) "
                    "VALUES ('anthropic', 's3ntin3l8@gmail.com', 'laptop', 'lifetime', 'all', 'tokens_input', 300000.0)"
                )
            )
            conn.commit()

            result = _migrate_cumulative_usage_remove_sidecar_id(conn)

        assert result is True

        with self.engine.connect() as conn:
            # sidecar_id is NOT in the UNIQUE constraint
            constraint_cols = _constraint_cols_for(conn, "cumulative_usage")
            assert constraint_cols, "UNIQUE constraint should still exist"
            assert "sidecar_id" not in constraint_cols, (
                f"sidecar_id should be removed from UNIQUE constraint, got: {constraint_cols!r}"
            )

            # Only 1 row for the merged identity
            count = conn.execute(sa("SELECT COUNT(*) FROM cumulative_usage")).scalar()
            assert count == 1, f"Expected 1 merged row, got {count}"

            # Summed total_value
            row = conn.execute(
                sa(
                    "SELECT total_value FROM cumulative_usage "
                    "WHERE provider_id='anthropic' AND account_id='s3ntin3l8@gmail.com' "
                    "AND period_type='lifetime' AND period_key='all' AND unit_type='tokens_input'"
                )
            ).first()
            assert row is not None, "Merged row should exist"
            assert row[0] == pytest.approx(800000.0), f"Expected 800000.0, got {row[0]}"

            # sidecar_id column still exists
            pragma = conn.execute(sa("PRAGMA table_info(cumulative_usage)")).fetchall()
            col_names = [r[1] for r in pragma]
            assert "sidecar_id" in col_names, "sidecar_id column should still be present"

    def test_second_call_is_noop(self):
        with self.engine.connect() as conn:
            conn.execute(sa(OLD_CUMULATIVE_USAGE_DDL))
            conn.commit()

            first = _migrate_cumulative_usage_remove_sidecar_id(conn)
            assert first is True

            second = _migrate_cumulative_usage_remove_sidecar_id(conn)
            assert second is False


# ---------------------------------------------------------------------------
# Test 3: fresh DB has new constraint without sidecar_id
# ---------------------------------------------------------------------------


class TestFreshDbConstraints:
    def setup_method(self):
        self.engine, self.db_path = _make_temp_engine()

    def teardown_method(self):
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_fresh_latest_usage_has_no_sidecar_id_in_constraint(self):
        SQLModel.metadata.create_all(self.engine)

        with self.engine.connect() as conn:
            constraint_cols = _constraint_cols_for(conn, "latest_usage")
            assert constraint_cols, "UNIQUE constraint should exist on latest_usage"
            assert "sidecar_id" not in constraint_cols, (
                f"Fresh latest_usage should NOT have sidecar_id in UNIQUE constraint, "
                f"got: {constraint_cols!r}"
            )

    def test_fresh_cumulative_usage_has_no_sidecar_id_in_constraint(self):
        SQLModel.metadata.create_all(self.engine)

        with self.engine.connect() as conn:
            constraint_cols = _constraint_cols_for(conn, "cumulative_usage")
            assert constraint_cols, "UNIQUE constraint should exist on cumulative_usage"
            assert "sidecar_id" not in constraint_cols, (
                f"Fresh cumulative_usage should NOT have sidecar_id in UNIQUE constraint, "
                f"got: {constraint_cols!r}"
            )
