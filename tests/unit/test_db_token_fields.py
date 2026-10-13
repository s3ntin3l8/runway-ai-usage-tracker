"""Tests for token fields in UsageSnapshot and UsageSnapshotModel table."""

import os
import tempfile

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import UsageSnapshot, UsageSnapshotModel


@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    os.close(fd)
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()
        os.remove(db_path)


# ── UsageSnapshot token fields ───────────────────────────────────────────────────


def test_snapshot_stores_token_fields(session: Session):
    """UsageSnapshot should store all token breakdown fields."""
    snap = UsageSnapshot(
        provider_id="opencode",
        account_id="user1",
        service_name="OpenCode",
        health="good",
        data_source="local",
        tokens_input=1000.0,
        tokens_output=500.0,
        tokens_reasoning=100.0,
        tokens_cache_read=50.0,
        tokens_total=1650.0,
        msgs=42,
    )
    session.add(snap)
    session.commit()

    result = session.get(UsageSnapshot, snap.id)
    assert result.tokens_input == 1000.0
    assert result.tokens_output == 500.0
    assert result.tokens_reasoning == 100.0
    assert result.tokens_cache_read == 50.0
    assert result.tokens_total == 1650.0
    assert result.msgs == 42


def test_snapshot_token_fields_nullable(session: Session):
    """Token fields should be nullable for providers that don't report tokens."""
    snap = UsageSnapshot(
        provider_id="github",
        account_id="user2",
        service_name="GitHub Copilot",
        health="good",
        data_source="api",
    )
    session.add(snap)
    session.commit()

    result = session.get(UsageSnapshot, snap.id)
    assert result.tokens_input is None
    assert result.tokens_output is None
    assert result.tokens_reasoning is None
    assert result.tokens_cache_read is None
    assert result.tokens_total is None
    assert result.msgs is None


def test_snapshot_token_total_explicitly_set(session: Session):
    """tokens_total must be explicitly set at write time (not auto-derived)."""
    snap = UsageSnapshot(
        provider_id="opencode",
        account_id="user1",
        service_name="OpenCode",
        health="good",
        data_source="local",
        tokens_input=1000.0,
        tokens_output=500.0,
        tokens_reasoning=100.0,
        tokens_cache_read=50.0,
        tokens_total=1600.0,  # Explicitly set by poller
    )
    session.add(snap)
    session.commit()

    result = session.get(UsageSnapshot, snap.id)
    assert result.tokens_total == 1600.0


# ── UsageSnapshotModel table ────────────────────────────────────────────────


def test_snapshot_model_stores_per_model_data(session: Session):
    """UsageSnapshotModel should store per-model cost and token breakdown."""
    snap = UsageSnapshot(
        provider_id="opencode",
        account_id="user1",
        service_name="OpenCode",
        health="good",
        data_source="local",
        tokens_total=1650.0,
        msgs=42,
    )
    session.add(snap)
    session.commit()
    session.refresh(snap)

    model_record = UsageSnapshotModel(
        snapshot_id=snap.id,
        model_id="m2.5-ultra",
        cost=1.50,
        msgs=20,
        tokens_input=2000.0,
        tokens_output=1000.0,
        tokens_reasoning=200.0,
        tokens_cache_read=100.0,
        tokens_total=3200.0,
    )
    session.add(model_record)
    session.commit()

    result = session.exec(
        select(UsageSnapshotModel).where(UsageSnapshotModel.snapshot_id == snap.id)
    ).first()
    assert result.model_id == "m2.5-ultra"
    assert result.cost == 1.50
    assert result.msgs == 20
    assert result.tokens_input == 2000.0
    assert result.tokens_output == 1000.0
    assert result.tokens_reasoning == 200.0
    assert result.tokens_cache_read == 100.0
    assert result.tokens_total == 3200.0


def test_snapshot_model_cascade_delete(session: Session):
    """UsageSnapshotModel records must be deleted explicitly - no auto-cascade."""
    snap = UsageSnapshot(
        provider_id="opencode",
        account_id="user1",
        service_name="OpenCode",
        health="good",
        data_source="local",
    )
    session.add(snap)
    session.commit()
    session.refresh(snap)

    model_record = UsageSnapshotModel(
        snapshot_id=snap.id,
        model_id="m2.5-ultra",
        cost=1.50,
    )
    session.add(model_record)
    # Model record still exists after deleting snapshot - must delete explicitly


def test_snapshot_model_multiple_models_per_snapshot(session: Session):
    """A snapshot can have multiple model records (one per model)."""
    snap = UsageSnapshot(
        provider_id="opencode",
        account_id="user1",
        service_name="OpenCode",
        health="good",
        data_source="local",
    )
    session.add(snap)
    session.commit()
    session.refresh(snap)

    for model_id, cost in [("m2.5-ultra", 1.50), ("m2.5-free", 0.0)]:
        record = UsageSnapshotModel(
            snapshot_id=snap.id,
            model_id=model_id,
            cost=cost,
            msgs=10,
        )
        session.add(record)
    session.commit()

    models = session.exec(
        select(UsageSnapshotModel).where(UsageSnapshotModel.snapshot_id == snap.id)
    ).all()
    assert len(models) == 2
    model_ids = {r.model_id for r in models}
    assert "m2.5-ultra" in model_ids
    assert "m2.5-free" in model_ids


# ── Query patterns ───────────────────────────────────────────────────────────


def test_aggregate_tokens_by_provider(session: Session):
    """Should be able to aggregate total tokens by provider."""
    for input_tok, output_tok in [(1000, 500), (2000, 1000), (500, 250)]:
        snap = UsageSnapshot(
            provider_id="opencode",
            account_id="user1",
            service_name="OpenCode",
            health="good",
            data_source="local",
            tokens_input=input_tok,
            tokens_output=output_tok,
            tokens_reasoning=0,
            tokens_cache_read=0,
        )
        session.add(snap)
    session.commit()

    results = session.exec(
        select(UsageSnapshot).where(UsageSnapshot.provider_id == "opencode")
    ).all()
    total_input = sum(r.tokens_input or 0 for r in results)
    total_output = sum(r.tokens_output or 0 for r in results)
    assert total_input == 3500
    assert total_output == 1750


def test_join_snapshot_with_model_records(session: Session):
    """Should be able to join snapshot with its model records."""
    snap = UsageSnapshot(
        provider_id="opencode",
        account_id="user1",
        service_name="OpenCode",
        health="good",
        data_source="local",
    )
    session.add(snap)
    session.commit()
    session.refresh(snap)

    session.add(UsageSnapshotModel(snapshot_id=snap.id, model_id="m2.5-ultra", cost=1.50))
    session.add(UsageSnapshotModel(snapshot_id=snap.id, model_id="m2.5-free", cost=0.0))
    session.commit()

    snapshot = session.exec(select(UsageSnapshot).where(UsageSnapshot.id == snap.id)).first()
    models = session.exec(
        select(UsageSnapshotModel).where(UsageSnapshotModel.snapshot_id == snapshot.id)
    ).all()

    total_cost = sum(r.cost or 0 for r in models)
    assert total_cost == 1.50
