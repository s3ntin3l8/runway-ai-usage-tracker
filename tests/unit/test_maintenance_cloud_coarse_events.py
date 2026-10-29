"""Tests for app/services/maintenance/cloud_coarse_events.py."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import (
    ClaudeCloudCollectorState,
    ClaudeCloudSessionUsage,
    UsageEvent,
    UsagePeriodRollup,
)
from app.services.maintenance.cloud_coarse_events import (
    apply_cloud_coarse_events,
    plan_cloud_coarse_events,
)
from app.services.period_rollups import rebuild_rollups_for_pairs

DIGEST = "a" * 32


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _event(session: Session, event_id: str, **overrides) -> UsageEvent:
    base = {
        "provider_id": "anthropic",
        "account_id": "holder@example.com",
        "sidecar_id": "server",
        "event_id": event_id,
        "ts": datetime(2026, 9, 24, 12, tzinfo=UTC),
        "kind": "message",
        "model_id": "opus-5.5",
        "session_id": DIGEST,
        "entrypoint": "cloud",
        "tokens_input": 10,
        "tokens_output": 20,
        "tokens_cache_read": 300,
        "tokens_cache_create": 40,
        "cost_usd": 0.5,
    }
    base.update(overrides)
    row = UsageEvent(**base)
    session.add(row)
    session.commit()
    return row


def _seed(session: Session) -> None:
    _event(session, f"cloud:{DIGEST}:1")
    _event(session, f"cloud:{DIGEST}:2")
    _event(session, f"cloud:{'b' * 32}:1", session_id="b" * 32)
    # Not the first collector's: new-form cloud events and ordinary local events.
    _event(session, f"cloud:{DIGEST}:m:msg_1")
    _event(session, f"cloud:{DIGEST}:r:1:opus-5.5")
    _event(session, "msg_local_1", entrypoint="cli", sidecar_id="dev-01", session_id="local-1")
    _event(session, f"cloud:{DIGEST}:3", provider_id="openai")  # a different provider
    rebuild_rollups_for_pairs(
        session, {("anthropic", "holder@example.com"), ("openai", "holder@example.com")}
    )
    session.commit()


def test_plan_counts_only_the_first_collectors_events():
    session = _session()
    _seed(session)
    plan = plan_cloud_coarse_events(session)
    assert plan.events == 3 and plan.sessions == 2
    assert plan.tokens_output == 60 and plan.tokens_cache_read == 900
    assert plan.pairs == [("anthropic", "holder@example.com")]
    assert (
        plan.ts_min
        == plan.ts_max
        == datetime(2026, 9, 24, 12, tzinfo=UTC).replace(tzinfo=plan.ts_min.tzinfo)
    )
    assert len(plan.samples) == 3 and all(s.startswith("cloud:") for s in plan.samples)


def test_plan_is_read_only():
    session = _session()
    _seed(session)
    plan_cloud_coarse_events(session)
    assert len(session.exec(select(UsageEvent)).all()) == 7


def test_apply_removes_only_those_events_and_repairs_the_rollups():
    session = _session()
    _seed(session)
    result = apply_cloud_coarse_events(session)
    assert result.events_deleted == 3 and result.rollup_pairs_rebuilt == 1
    remaining = {e.event_id for e in session.exec(select(UsageEvent))}
    assert remaining == {
        f"cloud:{DIGEST}:m:msg_1",
        f"cloud:{DIGEST}:r:1:opus-5.5",
        "msg_local_1",
        f"cloud:{DIGEST}:3",
    }
    lifetime = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "anthropic",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).one()
    assert lifetime.msgs == 3  # the rollup follows the events that remain


def test_apply_is_idempotent():
    session = _session()
    _seed(session)
    apply_cloud_coarse_events(session)
    again = apply_cloud_coarse_events(session)
    assert (again.events_deleted, again.state_rows_deleted, again.windows_rebuilt) == (0, 0, 0)


def test_apply_clears_the_old_state_tables_but_keeps_them():
    session = _session()
    session.add(ClaudeCloudSessionUsage(account_id="holder@example.com", session_digest=DIGEST))
    session.add(ClaudeCloudCollectorState(account_id="holder@example.com"))
    session.commit()
    plan = plan_cloud_coarse_events(session)
    assert plan.events == 0 and plan.state_rows == 2
    result = apply_cloud_coarse_events(session)
    assert result.events_deleted == 0 and result.state_rows_deleted == 2
    assert session.exec(select(ClaudeCloudSessionUsage)).all() == []
    assert session.exec(select(ClaudeCloudCollectorState)).all() == []


def test_apply_with_nothing_to_remove_touches_nothing():
    session = _session()
    _event(session, f"cloud:{DIGEST}:m:msg_1")
    result = apply_cloud_coarse_events(session)
    assert result.events_deleted == result.state_rows_deleted == 0
    assert len(session.exec(select(UsageEvent)).all()) == 1
