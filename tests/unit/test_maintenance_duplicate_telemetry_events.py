"""Tests for app/services/maintenance/duplicate_telemetry_events.py."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup, UsageWindow
from app.services.data_health.checks.duplicate_telemetry_events import (
    GROUP_KEY,
    DuplicateTelemetryEventsCheck,
)
from app.services.maintenance.duplicate_telemetry_events import (
    TELEMETRY_PREFIX,
    apply_duplicate_telemetry_events,
    plan_duplicate_telemetry_events,
)
from app.services.period_rollups import rebuild_rollups_for_pairs
from scripts.sidecar_pkg.otel_receiver import EVENT_ID_PREFIX

ACCOUNT = "holder@example.com"


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _event(session: Session, event_id: str, request_id: str | None, **overrides) -> None:
    values: dict = {
        "provider_id": "anthropic",
        "account_id": ACCOUNT,
        "sidecar_id": "macbook",
        "event_id": event_id,
        "request_id": request_id,
        "ts": datetime(2026, 11, 27, 11, 35, tzinfo=UTC),
        "kind": "message",
        "model_id": "sonnet-5.5",
        "session_id": "desk-1",
        "tokens_input": 10,
        "tokens_output": 100,
        "tokens_cache_read": 1000,
        "tokens_cache_create": 0,
        "cost_usd": 0.05,
    }
    values.update(overrides)
    session.add(UsageEvent(**values))
    session.commit()


def _seed(session: Session) -> None:
    # Desktop Code main loop: stored from the transcript and again from telemetry ("sdk").
    _event(session, "msg_1", "req_1", entrypoint="claude-desktop")
    _event(session, "otel|req_1", "req_1", query_source="sdk")
    _event(session, "msg_2", "req_2", entrypoint="claude-desktop")
    _event(session, "otel|req_2", "req_2", query_source="sdk")
    # A genuine side call: no transcript event shares its request id.
    _event(session, "otel|req_3", "req_3", query_source="prompt_suggestion")
    # Same request id in another session or provider is not this call.
    _event(session, "msg_4", "req_4", session_id="other")
    _event(session, "otel|req_4", "req_4", query_source="sdk")
    _event(session, "msg_5", "req_5", provider_id="openai")
    _event(session, "otel|req_5", "req_5", query_source="sdk")
    # A transcript event without a request id matches nothing.
    _event(session, "msg_6", None)
    _event(session, "otel|req_6", "req_6", query_source="sdk")
    rebuild_rollups_for_pairs(session, {("anthropic", ACCOUNT), ("openai", ACCOUNT)})
    session.commit()


def _lifetime(session: Session) -> UsagePeriodRollup:
    return session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "anthropic",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).one()


def test_plan_finds_only_telemetry_copies_of_transcript_calls() -> None:
    session = _session()
    _seed(session)
    plan = plan_duplicate_telemetry_events(session)
    assert plan.events == 2 and plan.sessions == 1
    assert plan.samples == ["otel|req_1", "otel|req_2"]
    assert abs(plan.cost_usd - 0.10) < 1e-9 and plan.tokens_output == 200
    assert plan.accounts == [ACCOUNT]


def test_apply_removes_the_copies_keeps_the_transcript_and_rebuilds_rollups() -> None:
    session = _session()
    _seed(session)
    assert _lifetime(session).msgs == 10  # 11 seeded, one is openai

    result = apply_duplicate_telemetry_events(session)
    assert result.events_deleted == 2 and result.rollup_pairs_rebuilt == 1

    ids = set(session.exec(select(UsageEvent.event_id)))
    assert {"msg_1", "msg_2", "otel|req_3", "otel|req_4", "otel|req_6"} <= ids
    assert not {"otel|req_1", "otel|req_2"} & ids
    assert _lifetime(session).msgs == 8
    # A second run has nothing left to do.
    assert apply_duplicate_telemetry_events(session).events_deleted == 0


def test_check_reports_plans_and_fixes_the_duplicates() -> None:
    session = _session()
    check = DuplicateTelemetryEventsCheck()
    assert check.detect(session).total_count == 0

    _seed(session)
    report = check.detect(session)
    assert report.total_count == 2
    group = report.groups[0]
    assert group.key == GROUP_KEY and group.fixable
    assert group.detail == {"events": 2, "sessions": 1, "cost_usd": 0.1}

    plan = check.plan(session, GROUP_KEY, {})
    assert plan.counts["events"] == 2 and plan.confirmation_text
    assert session.exec(select(UsageEvent).where(UsageEvent.event_id == "otel|req_1")).first()

    fixed, hooks = check.apply(session, GROUP_KEY, {})
    assert fixed.counts["events_deleted"] == 2 and hooks == []
    assert check.detect(session).total_count == 0


def test_the_prefix_matches_the_sidecar_receivers_event_ids() -> None:
    # A rename in the receiver would otherwise make this check a silent 0.
    assert TELEMETRY_PREFIX == EVENT_ID_PREFIX


def test_apply_deletes_in_batches_and_rebuilds_an_overlapping_closed_window() -> None:
    session = _session()
    _seed(session)
    session.add(
        UsageWindow(
            provider_id="anthropic",
            account_id=ACCOUNT,
            window_type="weekly",
            window_start=datetime(2026, 11, 24, tzinfo=UTC),
            window_end=datetime(2026, 12, 1, tzinfo=UTC),
            model_id="",
            sidecar_id="",
            msgs=10,
            tokens_output=1000,
            limit_value=100.0,
            pct_used=42.0,
        )
    )
    session.commit()

    result = apply_duplicate_telemetry_events(session, batch_size=1)
    assert result.events_deleted == 2
    assert result.windows_rebuilt == 1
    window = session.exec(
        select(UsageWindow).where(UsageWindow.model_id == "", UsageWindow.sidecar_id == "")
    ).one()
    assert window.msgs == 8  # the anthropic events left in that week
    assert window.tokens_output == 800
    assert window.pct_used == 42.0  # the scrape-time gauge survives the rebuild
