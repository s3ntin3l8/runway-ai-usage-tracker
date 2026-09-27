"""Tests for the quota_snapshots table-rebuild migration (D6).

Long-lived databases carry ``uq_quota_snapshots_identity`` baked into
CREATE TABLE as a 5-column constraint (no ``variant``), predating the
column's addition. SQLite can't ALTER a table constraint in place, so a
second variant sharing every other key at the same timestamp (e.g. two
Antigravity quota gauges — "frontier" and "gemini" — polled in the same
minute) is silently rejected by the stale autoindex rather than stored.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.core.db import _rebuild_quota_snapshot_table_for_variant


def _legacy_engine():
    """A DB whose quota_snapshots table predates the variant column."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE quota_snapshots"))
        conn.execute(
            text(
                "CREATE TABLE quota_snapshots ("
                "id INTEGER NOT NULL, "
                "provider_id VARCHAR NOT NULL, "
                "account_id VARCHAR NOT NULL, "
                "window_type VARCHAR NOT NULL, "
                "model_id VARCHAR NOT NULL DEFAULT '', "
                "ts DATETIME NOT NULL, "
                "pct_used FLOAT, "
                "reset_at DATETIME, "
                "variant TEXT NOT NULL DEFAULT '', "
                "PRIMARY KEY (id), "
                "CONSTRAINT uq_quota_snapshots_identity "
                "UNIQUE (provider_id, account_id, window_type, model_id, ts))"
            )
        )
        conn.execute(text("CREATE INDEX ix_quota_snapshots_ts ON quota_snapshots (ts)"))
        conn.execute(
            text(
                "CREATE INDEX ix_quota_snapshots_lookup ON quota_snapshots "
                "(provider_id, account_id, window_type, ts)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO quota_snapshots "
                "(id, provider_id, account_id, window_type, model_id, ts, pct_used, reset_at, variant) "
                "VALUES (1, 'antigravity', 's3ntin3l8@gmail.com', 'session', '', "
                "'2026-09-25 06:21:00', 0.0, '2026-09-26 06:21:00', 'frontier')"
            )
        )
        conn.commit()
    return engine


def test_legacy_table_silently_drops_a_second_variant_at_the_same_timestamp():
    """Establishes the bug: before the rebuild, the stale 5-column autoindex
    rejects a second variant sharing the rest of the key at the same ts."""
    engine = _legacy_engine()
    with engine.connect() as conn:
        # Same provider/account/window/ts as the seeded row, different variant.
        try:
            conn.execute(
                text(
                    "INSERT INTO quota_snapshots "
                    "(provider_id, account_id, window_type, model_id, ts, pct_used, reset_at, variant) "
                    "VALUES ('antigravity', 's3ntin3l8@gmail.com', 'session', '', "
                    "'2026-09-25 06:21:00', 0.77, '2026-09-26 06:21:00', 'gemini')"
                )
            )
            conn.commit()
        except IntegrityError:
            pass
        else:
            raise AssertionError("expected the legacy autoindex to reject the second variant")


def test_rebuild_quota_snapshot_table_for_variant_upgrades_legacy_table():
    """The migration rebuilds the table so the constraint covers variant,
    preserves existing rows, and both variants can then coexist."""
    engine = _legacy_engine()
    with engine.connect() as conn:
        _rebuild_quota_snapshot_table_for_variant(conn)

        # Existing row is preserved.
        rows = conn.execute(
            text("SELECT id, provider_id, variant, pct_used FROM quota_snapshots ORDER BY id")
        ).fetchall()
        assert [(r[0], r[1], r[2], r[3]) for r in rows] == [(1, "antigravity", "frontier", 0.0)]

        # Legacy 5-column constraint is gone from the table DDL.
        table_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='quota_snapshots'")
        ).first()
        assert table_sql is not None
        assert "UNIQUE (provider_id, account_id, window_type, model_id, ts)" not in table_sql[0]

        # A second variant at the same (provider, account, window, model, ts)
        # now inserts successfully instead of being silently dropped.
        conn.execute(
            text(
                "INSERT INTO quota_snapshots "
                "(provider_id, account_id, window_type, model_id, ts, pct_used, reset_at, variant) "
                "VALUES ('antigravity', 's3ntin3l8@gmail.com', 'session', '', "
                "'2026-09-25 06:21:00', 0.77, '2026-09-26 06:21:00', 'gemini')"
            )
        )
        conn.commit()
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM quota_snapshots WHERE provider_id='antigravity' "
                "AND ts='2026-09-25 06:21:00'"
            )
        ).scalar()
        assert count == 2

        # The plain (non-unique) indexes the old table had survive the rebuild.
        index_names = {r[1] for r in conn.execute(text("PRAGMA index_list(quota_snapshots)"))}
        assert "ix_quota_snapshots_lookup" in index_names

        # Second run is a no-op (no legacy constraint left in the DDL).
        before = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='quota_snapshots'")
        ).first()
        _rebuild_quota_snapshot_table_for_variant(conn)
        after = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='quota_snapshots'")
        ).first()
        assert before == after


def test_detects_the_shape_produced_by_months_of_the_old_index_only_fix():
    """The real production shape: the old ``_rebuild_quota_snapshot_indexes``
    migration ran on every boot for months, so alongside the legacy 5-column
    autoindex there's also an explicit, correctly 6-column
    ``uq_quota_snapshots_identity`` *named* index (a different object, same
    name as the constraint) — an exact-DDL-substring check could plausibly
    be confused by the coexisting wider index; the structural PRAGMA check
    must still detect the constraint itself needs rebuilding."""
    from app.core.db import _quota_snapshot_table_constraint_covers_variant

    engine = _legacy_engine()
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_quota_snapshots_identity ON quota_snapshots "
                "(provider_id, account_id, window_type, variant, model_id, ts)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX ix_quota_snapshots_series_ts ON quota_snapshots "
                "(provider_id, account_id, window_type, variant, model_id, ts)"
            )
        )
        conn.commit()

        assert _quota_snapshot_table_constraint_covers_variant(conn) is False

        _rebuild_quota_snapshot_table_for_variant(conn)

        assert _quota_snapshot_table_constraint_covers_variant(conn) is True
        rows = conn.execute(text("SELECT id, variant FROM quota_snapshots")).fetchall()
        assert rows == [(1, "frontier")]


def test_fresh_db_skips_quota_snapshot_table_rebuild():
    """create_all() already bakes variant into the constraint — no-op."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='quota_snapshots'")
        ).first()
        _rebuild_quota_snapshot_table_for_variant(conn)
        after = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='quota_snapshots'")
        ).first()
        assert before == after
