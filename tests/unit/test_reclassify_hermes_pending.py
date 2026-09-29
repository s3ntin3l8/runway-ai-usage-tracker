"""Unit tests for scripts/reclassify_hermes_pending.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import PendingUsageEvent, UsageEvent
from scripts.reclassify_hermes_pending import (
    determine_canonical_provider,
    reclassify_pending,
    reclassify_usage_events,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_determine_canonical_provider():
    assert determine_canonical_provider("hermes-xai-oauth", "grok-4.7") == "xai"
    assert determine_canonical_provider("hermes-auto", "deepseek-v4-flash-free") == "opencode-free"
    assert determine_canonical_provider("hermes-auto", "deepseek-v4-flash") == "opencode"
    assert determine_canonical_provider("hermes", "kimi-for-coding") == "kimi_coding"
    assert determine_canonical_provider("hermes", "kimi-k2.8") == "kimi_coding"
    assert determine_canonical_provider("hermes", "grok-4.7") == "xai"
    assert determine_canonical_provider("hermes", "MiniMax-M3") == "minimax"
    assert determine_canonical_provider("hermes", "deepseek-v4-flash") == "opencode"
    assert (
        determine_canonical_provider("hermes", "nvidia/nemotron-3-ultra-550b-a55b:free")
        == "opencode-free"
    )
    assert determine_canonical_provider("unrelated", "some-model") == "unrelated"


def test_reclassify_pending_dry_run_and_apply(db_session: Session):
    now = datetime.now(UTC)

    # Insert pending events
    p1 = PendingUsageEvent(
        provider_id="hermes-xai-oauth",
        event_id="ev-xai-1",
        sidecar_id="sc-1",
        ts=now,
        payload_json=json.dumps({"provider_id": "hermes-xai-oauth", "model_id": "grok-4.7"}),
    )
    p2 = PendingUsageEvent(
        provider_id="hermes-auto",
        event_id="ev-auto-free",
        sidecar_id="sc-1",
        ts=now,
        payload_json=json.dumps(
            {"provider_id": "hermes-auto", "model_id": "deepseek-v4-flash-free"}
        ),
    )
    p3 = PendingUsageEvent(
        provider_id="hermes",
        event_id="ev-hermes-kimi",
        sidecar_id="sc-1",
        ts=now,
        payload_json=json.dumps({"provider_id": "hermes", "model_id": "kimi-for-coding"}),
    )
    p4 = PendingUsageEvent(
        provider_id="hermes",
        event_id="ev-hermes-oc",
        sidecar_id="sc-1",
        ts=now,
        payload_json=json.dumps({"provider_id": "hermes", "model_id": "deepseek-v4-flash"}),
    )
    db_session.add_all([p1, p2, p3, p4])
    db_session.commit()

    # 1. Dry run: counts 4, no DB mutation
    count = reclassify_pending(db_session, dry_run=True)
    assert count == 4

    rows = db_session.exec(select(PendingUsageEvent)).all()
    assert {r.provider_id for r in rows} == {"hermes-xai-oauth", "hermes-auto", "hermes"}

    # 2. Apply: mutates DB
    count = reclassify_pending(db_session, dry_run=False)
    assert count == 4

    rows = db_session.exec(select(PendingUsageEvent)).all()
    by_event = {r.event_id: r for r in rows}

    assert by_event["ev-xai-1"].provider_id == "xai"
    assert json.loads(by_event["ev-xai-1"].payload_json)["provider_id"] == "xai"

    assert by_event["ev-auto-free"].provider_id == "opencode-free"
    assert json.loads(by_event["ev-auto-free"].payload_json)["provider_id"] == "opencode-free"

    assert by_event["ev-hermes-kimi"].provider_id == "kimi_coding"
    assert json.loads(by_event["ev-hermes-kimi"].payload_json)["provider_id"] == "kimi_coding"

    assert by_event["ev-hermes-oc"].provider_id == "opencode"
    assert json.loads(by_event["ev-hermes-oc"].payload_json)["provider_id"] == "opencode"


def test_reclassify_usage_events(db_session: Session):
    now = datetime.now(UTC)

    ev1 = UsageEvent(
        provider_id="hermes-xai-oauth",
        account_id="default",
        sidecar_id="sc-1",
        event_id="ev-xai-usage",
        ts=now,
        kind="message",
        model_id="grok-4.7",
        tokens_input=100,
        tokens_output=50,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
        cost_usd=0.0,
        tool_calls=0,
        ingested_at=now,
    )
    db_session.add(ev1)
    db_session.commit()

    count = reclassify_usage_events(db_session, dry_run=True)
    assert count == 1
    assert db_session.exec(select(UsageEvent)).first().provider_id == "hermes-xai-oauth"

    count = reclassify_usage_events(db_session, dry_run=False)
    assert count == 1
    assert db_session.exec(select(UsageEvent)).first().provider_id == "xai"
