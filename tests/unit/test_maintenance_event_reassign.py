"""Tests for app/services/maintenance/event_reassign.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.maintenance.event_reassign import (
    ReassignPlan,
    apply_reassign_default,
    plan_reassign_default,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _event(session: Session, *, event_id: str = "x", **overrides) -> UsageEvent:
    base = {
        "provider_id": "minimax",
        "account_id": "default",
        "sidecar_id": "dev-01",
        "event_id": f"msg_{event_id}",
        "ts": datetime(2026, 9, 1, tzinfo=UTC),
        "kind": "message",
        "model_id": "MiniMax-M3",
        "tokens_input": 100,
        "tokens_output": 20,
        "cost_usd": 0.05,
        "attribution_source": "default",
    }
    base.update(overrides)
    ev = UsageEvent(**base)
    session.add(ev)
    session.commit()
    return ev


def test_plan_finds_all_events_under_source_when_no_event_ids_given():
    session = _session()
    _event(session, event_id="1")
    _event(session, event_id="2")

    plan = plan_reassign_default(
        session, provider_id="minimax", source="default", target="alice@example.com"
    )

    assert plan.count == 2


def test_plan_scopes_to_explicit_event_ids():
    session = _session()
    _event(session, event_id="1")
    _event(session, event_id="2")

    plan = plan_reassign_default(
        session,
        provider_id="minimax",
        source="default",
        target="alice@example.com",
        event_ids=["msg_1"],
    )

    assert plan.count == 1
    assert plan.samples[0].event_id == "msg_1"


def test_plan_raises_for_a_missing_explicit_event_id():
    session = _session()
    _event(session, event_id="1")

    with pytest.raises(ValueError, match="msg_nonexistent"):
        plan_reassign_default(
            session,
            provider_id="minimax",
            source="default",
            target="alice@example.com",
            event_ids=["msg_1", "msg_nonexistent"],
        )


def test_plan_is_read_only():
    session = _session()
    _event(session, event_id="1")

    plan_reassign_default(
        session, provider_id="minimax", source="default", target="alice@example.com"
    )

    ev = session.exec(select(UsageEvent)).one()
    assert ev.account_id == "default"


def test_apply_moves_events_and_sets_attribution_source_tag():
    session = _session()
    _event(session, event_id="1")

    result = apply_reassign_default(
        session, provider_id="minimax", source="default", target="alice@example.com"
    )

    assert result.moved == 1
    ev = session.exec(select(UsageEvent)).one()
    assert ev.account_id == "alice@example.com"
    assert ev.attribution_source == "tag"


def test_apply_rebuilds_rollups_for_both_accounts():
    session = _session()
    _event(session, event_id="1")
    _event(session, event_id="2", account_id="alice@example.com", attribution_source="tag")

    apply_reassign_default(
        session, provider_id="minimax", source="default", target="alice@example.com"
    )

    default_lifetime = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "minimax",
            UsagePeriodRollup.account_id == "default",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert default_lifetime is None  # nothing left under default

    alice_lifetime = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "minimax",
            UsagePeriodRollup.account_id == "alice@example.com",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert alice_lifetime is not None
    assert alice_lifetime.msgs == 2  # both the moved and the pre-existing event


def test_apply_with_no_matching_events_is_a_safe_no_op():
    session = _session()

    result = apply_reassign_default(
        session, provider_id="minimax", source="default", target="alice@example.com"
    )

    assert result.moved == 0
    assert result.rollups_rebuilt_pairs == 0
    assert result.windows_rebuilt == 0


def test_apply_only_moves_events_for_the_given_provider():
    session = _session()
    _event(session, event_id="1", provider_id="minimax")
    _event(session, event_id="2", provider_id="kimi_coding")

    apply_reassign_default(
        session, provider_id="minimax", source="default", target="alice@example.com"
    )

    kimi_ev = session.exec(select(UsageEvent).where(UsageEvent.provider_id == "kimi_coding")).one()
    assert kimi_ev.account_id == "default"  # untouched — different provider


def test_reassign_plan_defaults_are_empty():
    plan = ReassignPlan()
    assert plan.count == 0
    assert plan.samples == []
