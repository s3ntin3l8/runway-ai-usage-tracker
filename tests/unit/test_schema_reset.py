from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool


def test_new_tables_exist():
    """All four new tables are created by metadata.create_all."""
    from app.models.db import (  # noqa: F401  registers ORM metadata
        ProviderPricing,
        UsageEvent,
        UsagePeriodRollup,
        UsageWindow,
    )

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        from sqlalchemy import text

        names = [
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        ]
    assert "usage_events" in names
    assert "usage_windows" in names
    assert "usage_period_rollup" in names
    assert "provider_pricing" in names


def test_legacy_tables_absent():
    from app.models import db as m

    assert not hasattr(m, "UsageSnapshot")
    assert not hasattr(m, "UsageSnapshotModel")
    assert not hasattr(m, "CumulativeUsage")
