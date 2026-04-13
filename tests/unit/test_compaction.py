import pytest
from datetime import datetime, timedelta, timezone
from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool
from app.models.db import UsageSnapshot


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _snap(ts, used, limit, provider="anthropic", account="acc1"):
    """Helper: create a UsageSnapshot with raw_metadata_json set (not yet compacted)."""
    return UsageSnapshot(
        timestamp=ts,
        provider_id=provider,
        account_id=account,
        service_name="Test Service",
        used_value=used,
        limit_value=limit,
        unit_type="tokens",
        health="good",
        data_source="oauth",
        window_type="monthly",
        raw_metadata_json='{"test": true}',  # non-NULL = not compacted
    )


def test_hourly_compaction_merges_rows(session):
    """Rows 60-180 days old are compacted to one row per hour-bucket."""
    from app.services.compaction import compact_snapshots

    old = datetime.now(timezone.utc) - timedelta(days=90)
    for used in [100.0, 200.0, 300.0]:
        session.add(_snap(old, used, 1000.0))
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 1
    rows = session.exec(select(UsageSnapshot)).all()
    assert len(rows) == 1
    assert rows[0].used_value == pytest.approx(200.0)  # avg(100, 200, 300)
    assert rows[0].raw_metadata_json is None  # compacted marker


def test_daily_compaction_merges_rows(session):
    """Rows 180+ days old are compacted to one row per day-bucket."""
    from app.services.compaction import compact_snapshots

    old = datetime.now(timezone.utc) - timedelta(days=200)
    for used in [50.0, 150.0]:
        session.add(_snap(old, used, 500.0))
    session.commit()

    result = compact_snapshots(session)

    assert result["daily_compacted"] == 1
    rows = session.exec(select(UsageSnapshot)).all()
    assert len(rows) == 1
    assert rows[0].used_value == pytest.approx(100.0)  # avg(50, 150)


def test_recent_rows_not_compacted(session):
    """Rows less than 60 days old are left untouched."""
    from app.services.compaction import compact_snapshots

    recent = datetime.now(timezone.utc) - timedelta(days=10)
    session.add(_snap(recent, 100.0, 1000.0))
    session.add(_snap(recent, 200.0, 1000.0))
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 0
    assert result["daily_compacted"] == 0
    assert len(session.exec(select(UsageSnapshot)).all()) == 2


def test_already_compacted_rows_skipped(session):
    """Rows with raw_metadata_json=NULL are not re-compacted."""
    from app.services.compaction import compact_snapshots

    old = datetime.now(timezone.utc) - timedelta(days=90)
    snap = _snap(old, 100.0, 1000.0)
    snap.raw_metadata_json = None  # already compacted
    session.add(snap)
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 0
    assert len(session.exec(select(UsageSnapshot)).all()) == 1  # untouched


def test_single_row_per_bucket_not_compacted(session):
    """A bucket with only one row is not touched."""
    from app.services.compaction import compact_snapshots

    # Two rows in DIFFERENT hours, within the hourly compaction range (60-180 days old)
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=90)
    old1 = base.replace(hour=10)
    old2 = base.replace(hour=11)
    session.add(_snap(old1, 100.0, 1000.0))
    session.add(_snap(old2, 200.0, 1000.0))
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 0
    assert len(session.exec(select(UsageSnapshot)).all()) == 2


def test_multiple_providers_compacted_independently(session):
    """Different providers are compacted into separate rows."""
    from app.services.compaction import compact_snapshots

    old = datetime.now(timezone.utc) - timedelta(days=90)
    for provider in ["anthropic", "openai"]:
        for used in [100.0, 200.0]:
            session.add(_snap(old, used, 1000.0, provider=provider))
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 2
    rows = session.exec(select(UsageSnapshot)).all()
    assert len(rows) == 2
    providers = {r.provider_id for r in rows}
    assert providers == {"anthropic", "openai"}
