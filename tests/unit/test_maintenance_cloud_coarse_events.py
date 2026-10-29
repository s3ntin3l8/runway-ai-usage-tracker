"""Tests for app/services/maintenance/cloud_coarse_events.py."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, col, create_engine, delete, select
from sqlmodel.pool import StaticPool

from app.models.db import (
    ClaudeCloudCollectorState,
    ClaudeCloudSessionUsage,
    UsageEvent,
    UsagePeriodRollup,
    UsageWindow,
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
    assert result.events_deleted == 3 and result.rollup_pairs_rebuilt >= 1
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
    assert again.events_deleted == 0
    assert len(session.exec(select(UsageEvent)).all()) == 4


def test_apply_leaves_the_old_state_tables_alone():
    # Clearing them under a still-scheduled first collector would reset its baseline and re-bill.
    session = _session()
    _seed(session)
    session.add(ClaudeCloudSessionUsage(account_id="holder@example.com", session_digest=DIGEST))
    session.add(ClaudeCloudCollectorState(account_id="holder@example.com"))
    session.commit()
    apply_cloud_coarse_events(session)
    assert len(session.exec(select(ClaudeCloudSessionUsage)).all()) == 1
    assert len(session.exec(select(ClaudeCloudCollectorState)).all()) == 1


def _closed_window(session: Session, **overrides) -> UsageWindow:
    base = {
        "provider_id": "anthropic",
        "account_id": "holder@example.com",
        "window_type": "weekly",
        "window_start": datetime(2026, 9, 21, tzinfo=UTC),
        "window_end": datetime(2026, 9, 28, tzinfo=UTC),
        "model_id": "",
        "sidecar_id": "",
        "msgs": 7,
        "tokens_output": 140,
        "limit_value": 100.0,
        "pct_used": 42.0,
    }
    base.update(overrides)
    window = UsageWindow(**base)
    session.add(window)
    session.commit()
    return window


def test_a_closed_window_follows_the_remaining_events_and_keeps_its_gauge():
    session = _session()
    _seed(session)
    _closed_window(session)
    result = apply_cloud_coarse_events(session)
    assert result.windows_rebuilt >= 1
    window = session.exec(
        select(UsageWindow).where(UsageWindow.model_id == "", UsageWindow.sidecar_id == "")
    ).one()
    assert window.msgs == 3  # the three events left in that week
    assert window.tokens_output == 60
    assert window.limit_value == 100.0 and window.pct_used == 42.0


def test_a_run_that_stopped_after_the_delete_has_its_rollups_repaired_by_running_apply_again():
    session = _session()
    _seed(session)
    # Simulate the crash: the events are gone, the rollups still count them.
    coarse = [f"cloud:{DIGEST}:1", f"cloud:{DIGEST}:2", f"cloud:{'b' * 32}:1"]
    session.exec(delete(UsageEvent).where(col(UsageEvent.event_id).in_(coarse)))  # type: ignore[call-overload]
    session.commit()
    assert plan_cloud_coarse_events(session).events == 0
    result = apply_cloud_coarse_events(session)
    assert result.events_deleted == 0 and result.rollup_pairs_rebuilt >= 1
    lifetime = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "anthropic",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).one()
    assert lifetime.msgs == 3


def test_a_closed_window_without_events_is_left_alone_when_nothing_was_removed():
    # A window replay drops any window it cannot rebuild from events; a run that removed nothing
    # must not touch them (an archived gauge row can outlive its events).
    session = _session()
    _event(session, f"cloud:{DIGEST}:m:msg_1", ts=datetime(2026, 1, 5, tzinfo=UTC))
    _closed_window(
        session,
        window_start=datetime(2026, 3, 2, tzinfo=UTC),
        window_end=datetime(2026, 3, 9, tzinfo=UTC),
        msgs=900,
    )
    result = apply_cloud_coarse_events(session)
    assert result.windows_rebuilt == 0
    window = session.exec(select(UsageWindow)).one()
    assert window.msgs == 900 and window.limit_value == 100.0


def test_a_window_outside_the_removed_range_is_not_rebuilt():
    session = _session()
    _seed(session)
    _closed_window(
        session,
        window_start=datetime(2026, 3, 2, tzinfo=UTC),
        window_end=datetime(2026, 3, 9, tzinfo=UTC),
        msgs=900,
    )
    apply_cloud_coarse_events(session)
    window = session.exec(select(UsageWindow)).one()
    assert window.msgs == 900


def test_plan_counts_the_windows_it_would_rebuild():
    session = _session()
    _seed(session)
    _closed_window(session)  # the week of the events
    _closed_window(
        session,
        window_start=datetime(2026, 3, 2, tzinfo=UTC),
        window_end=datetime(2026, 3, 9, tzinfo=UTC),
    )
    assert plan_cloud_coarse_events(session).windows == 1


def test_apply_with_nothing_to_remove_deletes_nothing():
    session = _session()
    _event(session, f"cloud:{DIGEST}:m:msg_1")
    result = apply_cloud_coarse_events(session)
    assert result.events_deleted == 0
    assert len(session.exec(select(UsageEvent)).all()) == 1
