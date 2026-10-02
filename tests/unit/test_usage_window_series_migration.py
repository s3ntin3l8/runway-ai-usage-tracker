"""Migration coverage for quota-series identity in archived usage windows."""

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.core.db import _rebuild_usage_window_table_for_series_identity
from app.models.db import UsageWindow


def _legacy_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE usage_windows"))
        cols = [
            c
            for c in UsageWindow.__table__.columns
            if c.name not in {"series_model_id", "series_variant"}
        ]
        definitions = []
        for col in cols:
            type_sql = col.type.compile(dialect=conn.engine.dialect)
            if col.primary_key:
                definitions.append(f"{col.name} {type_sql} NOT NULL PRIMARY KEY")
            else:
                definitions.append(
                    f"{col.name} {type_sql}{' NOT NULL' if not col.nullable else ''}"
                )
        definitions.append(
            "CONSTRAINT uq_usage_windows_identity UNIQUE "
            "(provider_id, account_id, window_type, window_end, model_id, sidecar_id)"
        )
        conn.execute(text(f"CREATE TABLE usage_windows ({', '.join(definitions)})"))
        conn.execute(
            text(
                "INSERT INTO usage_windows "
                "(id, provider_id, account_id, window_type, window_start, window_end, model_id, "
                "sidecar_id, msgs, tokens_input, tokens_output, tokens_cache_read, "
                "tokens_cache_create, tokens_reasoning, cost_usd, limit_value, pct_used) "
                "VALUES (1, 'anthropic', 'me@example.com', 'weekly', '2026-05-05', "
                "'2026-05-12', '', '', 3, 100, 50, 0, 0, 0, 1.25, 1000, 10)"
            )
        )
        conn.commit()
    return engine


def test_rebuild_adds_series_identity_and_preserves_legacy_rows():
    engine = _legacy_engine()
    with engine.connect() as conn:
        _rebuild_usage_window_table_for_series_identity(conn)
        row = conn.execute(
            text(
                "SELECT series_model_id, series_variant, msgs, cost_usd "
                "FROM usage_windows WHERE id=1"
            )
        ).one()
        assert row == ("", "", 3, 1.25)
        assert conn.execute(
            text(
                "SELECT 1 FROM runway_schema_migrations "
                "WHERE migration_id='usage_windows_series_identity_v1'"
            )
        ).first()

        columns = {r[1] for r in conn.execute(text("PRAGMA table_info(usage_windows)"))}
        assert {"series_model_id", "series_variant"} <= columns

        conn.execute(
            text(
                "INSERT INTO usage_windows "
                "(provider_id, account_id, window_type, window_start, window_end, "
                "series_model_id, series_variant, model_id, sidecar_id, msgs, "
                "tokens_input, tokens_output, tokens_cache_read, tokens_cache_create, "
                "tokens_reasoning, cost_usd) "
                "VALUES ('anthropic', 'me@example.com', 'weekly', '2026-05-05', "
                "'2026-05-12', 'sonnet', 'default', '', '', 0, 0, 0, 0, 0, 0, 0)"
            )
        )
        conn.commit()
        assert conn.execute(text("SELECT COUNT(*) FROM usage_windows")).scalar_one() == 2


def test_fresh_usage_window_schema_skips_rebuild():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='usage_windows'")
        ).scalar_one()
        _rebuild_usage_window_table_for_series_identity(conn)
        _rebuild_usage_window_table_for_series_identity(conn)
        after = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='usage_windows'")
        ).scalar_one()
        assert before == after
        assert conn.execute(
            text(
                "SELECT 1 FROM runway_schema_migrations "
                "WHERE migration_id='usage_windows_series_identity_v1'"
            )
        ).first()
