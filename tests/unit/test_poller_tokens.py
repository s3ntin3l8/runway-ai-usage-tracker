"""Tests for poller token field population."""

import os
import tempfile
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import UsageSnapshot, UsageSnapshotModel
from app.models.schemas import LimitCard


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


# ── Token extraction from LimitCard ─────────────────────────────────────────


def test_snapshot_populated_from_card_token_usage(session: Session):
    """Poller should populate token fields from card.token_usage."""
    card = LimitCard(
        provider_id="opencode",
        account_id="user1",
        service_name="OpenCode",
        health="good",
        data_source="local",
        used_value=50.0,
        limit_value=100.0,
        unit_type="percent",
        token_usage={
            "input": 1000,
            "output": 500,
            "reasoning": 100,
            "cache_read": 50,
            "total": 1600,
        },
        msgs=42,
        by_model={
            "m2.5-ultra": {
                "cost": 1.50,
                "msgs": 20,
                "tokens": {"input": 1000, "output": 500, "reasoning": 100, "cache_read": 50},
            },
            "m2.5-free": {
                "cost": 0.0,
                "msgs": 22,
                "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0},
            },
        },
    )

    # Simulate what the poller does
    snapshot = UsageSnapshot(
        provider_id=card.provider_id,
        account_id=card.account_id,
        account_label=card.account_label,
        service_name=card.service_name,
        used_value=card.used_value,
        limit_value=card.limit_value,
        unit_type=card.unit_type,
        currency=card.currency,
        tier=card.tier,
        model_id=card.model_id,
        window_type=card.window_type,
        variant=card.variant,
        health=card.health,
        sidecar_id=card.sidecar_id,
        is_unlimited=card.is_unlimited,
        data_source=card.data_source,
        error_type=card.error_type,
        timestamp=datetime.now(UTC),
        tokens_input=card.token_usage.get("input") if card.token_usage else None,
        tokens_output=card.token_usage.get("output") if card.token_usage else None,
        tokens_reasoning=card.token_usage.get("reasoning") if card.token_usage else None,
        tokens_cache_read=card.token_usage.get("cache_read") if card.token_usage else None,
        tokens_total=card.token_usage.get("total") if card.token_usage else None,
        msgs=card.msgs,
    )
    snapshot.raw_metadata = card.metadata
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)

    result = session.get(UsageSnapshot, snapshot.id)
    assert result.tokens_input == 1000
    assert result.tokens_output == 500
    assert result.tokens_reasoning == 100
    assert result.tokens_cache_read == 50
    assert result.tokens_total == 1600
    assert result.msgs == 42

    # Also create model records
    if card.by_model:
        for model_id, model_data in card.by_model.items():
            model_record = UsageSnapshotModel(
                snapshot_id=snapshot.id,
                model_id=model_id,
                cost=model_data.get("cost"),
                msgs=model_data.get("msgs"),
                tokens_input=model_data.get("tokens", {}).get("input"),
                tokens_output=model_data.get("tokens", {}).get("output"),
                tokens_reasoning=model_data.get("tokens", {}).get("reasoning"),
                tokens_cache_read=model_data.get("tokens", {}).get("cache_read"),
                tokens_total=sum(
                    model_data.get("tokens", {}).get(k, 0) or 0
                    for k in ["input", "output", "reasoning"]
                ),
            )
            session.add(model_record)
        session.commit()

    models = session.exec(
        select(UsageSnapshotModel).where(UsageSnapshotModel.snapshot_id == snapshot.id)
    ).all()
    assert len(models) == 2

    ultra = next(m for m in models if m.model_id == "m2.5-ultra")
    assert ultra.cost == 1.50
    assert ultra.msgs == 20
    assert ultra.tokens_input == 1000
    assert ultra.tokens_output == 500
    assert ultra.tokens_total == 1600

    free = next(m for m in models if m.model_id == "m2.5-free")
    assert free.cost == 0.0
    assert free.msgs == 22


def test_snapshot_no_token_usage(session: Session):
    """When card has no token_usage, fields remain NULL."""
    card = LimitCard(
        provider_id="github",
        account_id="user2",
        service_name="GitHub Copilot",
        health="good",
        data_source="api",
        used_value=50.0,
        limit_value=100.0,
        unit_type="percent",
    )

    snapshot = UsageSnapshot(
        provider_id=card.provider_id,
        account_id=card.account_id,
        service_name=card.service_name,
        used_value=card.used_value,
        limit_value=card.limit_value,
        unit_type=card.unit_type,
        health=card.health,
        data_source=card.data_source,
        timestamp=datetime.now(UTC),
        tokens_input=None,
        tokens_output=None,
        tokens_reasoning=None,
        tokens_cache_read=None,
        tokens_total=None,
        msgs=None,
    )
    snapshot.raw_metadata = card.metadata
    session.add(snapshot)
    session.commit()

    result = session.get(UsageSnapshot, snapshot.id)
    assert result.tokens_input is None
    assert result.tokens_output is None
    assert result.msgs is None
