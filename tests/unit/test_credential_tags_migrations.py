"""Unit tests for credential tag schema migrations and index maintenance.

Migrations and index integrity:
- _migrate_credential_tag_scoping (#319): rebuild pre-#319 tables.
- _drop_redundant_credential_tag_indexes (#322 review).
- Forward-safe migrations for pending preview columns.
- Forward-safe migrations for target provider column.
- Keep alive column addition in sidecar registry.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import (
    _DEFERRED_COLUMNS,
    _add_columns_if_missing,
    _drop_redundant_credential_tag_indexes,
    _migrate_credential_tag_scoping,
)


def test_migrate_credential_tag_scoping_upgrades_legacy_table():
    """Functional upgrade: pre-#319 table (table-level UNIQUE on
    (provider_id, credential_origin), no sidecar_id) is rebuilt with the
    post-#319 shape; existing rows become deployment-wide (sidecar_id
    NULL). Idempotent on second run. Fresh DBs skip (no legacy constraint).
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with engine.connect() as conn:
        # Rebuild credential_tags as a pre-#319 legacy table.
        conn.execute(text("DROP TABLE credential_tags"))
        conn.execute(
            text(
                "CREATE TABLE credential_tags ("
                "id INTEGER PRIMARY KEY, provider_id VARCHAR NOT NULL, "
                "credential_origin VARCHAR NOT NULL, account_id VARCHAR NOT NULL, "
                "set_by VARCHAR NOT NULL, set_at TIMESTAMP NOT NULL, "
                "UNIQUE (provider_id, credential_origin) "
                "CONSTRAINT uq_credential_tag_identity)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO credential_tags "
                "(id, provider_id, credential_origin, account_id, set_by, set_at) "
                "VALUES (1, 'anthropic', 'path:/a', 'alice@example.com', "
                "'operator', '2026-01-01 00:00:00'),"
                "(2, 'minimax', 'provider:minimax', 's3ntin318@gmail.com', "
                "'operator', '2026-01-01 00:00:00')"
            )
        )
        conn.commit()

        _migrate_credential_tag_scoping(conn)

        # sidecar_id column exists; legacy rows are deployment-wide.
        rows = conn.execute(
            text("SELECT id, provider_id, account_id, sidecar_id FROM credential_tags ORDER BY id")
        ).fetchall()
        assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
            (1, "anthropic", "alice@example.com", None),
            (2, "minimax", "s3ntin318@gmail.com", None),
        ]

        # Legacy table constraint is gone; partial unique indexes present.
        table_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='credential_tags'")
        ).first()
        assert table_sql is not None and "uq_credential_tag_identity" not in table_sql[0]
        index_names = {r[1] for r in conn.execute(text("PRAGMA index_list(credential_tags)"))}
        assert "uq_credential_tag_deployment" in index_names
        assert "uq_credential_tag_sidecar" in index_names

        # Second run is a no-op (no legacy constraint in DDL).
        _migrate_credential_tag_scoping(conn)
        rows_again = conn.execute(
            text("SELECT id, sidecar_id FROM credential_tags ORDER BY id")
        ).fetchall()
        assert [(r[0], r[1]) for r in rows_again] == [(1, None), (2, None)]


def test_fresh_db_skips_credential_tag_scoping_migration():
    """create_all already builds the post-#319 shape — migration is a no-op."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='credential_tags'")
        ).first()
        _migrate_credential_tag_scoping(conn)
        after = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='credential_tags'")
        ).first()
        assert before == after
        index_names = {r[1] for r in conn.execute(text("PRAGMA index_list(credential_tags)"))}
        assert "uq_credential_tag_deployment" in index_names
        assert "uq_credential_tag_sidecar" in index_names


def test_drop_redundant_credential_tag_indexes():
    """Duplicate ``Field(index=True)`` indexes from earlier model versions
    are dropped at startup; fresh DBs never create them (#322 review)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def _index_names(conn) -> set[str]:
        return {
            r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
        }

    with engine.connect() as conn:
        fresh = _index_names(conn)
        assert "ix_credential_tags_sidecar_id" not in fresh
        assert "ix_credential_tags_sidecar" in fresh
        # Simulate a DB created by the previous model version.
        conn.execute(
            text("CREATE INDEX ix_credential_tags_sidecar_id ON credential_tags (sidecar_id)")
        )
        conn.execute(
            text("CREATE INDEX ix_credential_tags_provider_id ON credential_tags (provider_id)")
        )
        conn.commit()

        _drop_redundant_credential_tag_indexes(conn)
        _drop_redundant_credential_tag_indexes(conn)  # idempotent

        after = _index_names(conn)
        assert "ix_credential_tags_sidecar_id" not in after
        assert "ix_credential_tags_provider_id" not in after
        assert "ix_credential_tags_sidecar" in after


def test_pending_preview_columns_have_forward_safe_migrations():
    assert ("pending_credential_tags", "quota_preview_json", "TEXT") in _DEFERRED_COLUMNS
    assert (
        "pending_credential_tags",
        "quota_preview_observed_at",
        "DATETIME",
    ) in _DEFERRED_COLUMNS


def test_existing_pending_table_gets_preview_columns_idempotently():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE pending_credential_tags DROP COLUMN quota_preview_json"))
        conn.execute(
            text("ALTER TABLE pending_credential_tags DROP COLUMN quota_preview_observed_at")
        )
        conn.commit()

        _add_columns_if_missing(conn)
        _add_columns_if_missing(conn)

        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(pending_credential_tags)"))
        }
        assert "quota_preview_json" in columns
        assert "quota_preview_observed_at" in columns


def test_existing_credential_tags_table_gets_target_provider_column_idempotently():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE credential_tags DROP COLUMN target_provider_id"))
        conn.commit()

        _add_columns_if_missing(conn)
        _add_columns_if_missing(conn)

        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(credential_tags)"))}
        assert "target_provider_id" in columns


def test_keep_alive_column_is_added_to_existing_sidecar_registries():
    assert ("sidecar_registry", "keep_alive", "BOOLEAN") in _DEFERRED_COLUMNS
