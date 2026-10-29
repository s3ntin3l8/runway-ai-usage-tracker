"""Tests for app/services/data_health/checks/cloud_coarse_events.py."""

from __future__ import annotations

from sqlmodel import select

from app.models.db import ClaudeCloudSessionUsage, UsageEvent
from app.services.data_health.checks.cloud_coarse_events import GROUP_KEY, CloudCoarseEventsCheck
from tests.unit.data_health.conftest import make_event

DIGEST = "c" * 32


def _coarse(session, number, **overrides):
    return make_event(
        session,
        event_id=f"unused-{number}",
        provider_id="anthropic",
        account_id="holder@example.com",
        sidecar_id="server",
        model_id="opus-5.5",
        entrypoint="cloud",
        session_id=DIGEST,
        **overrides,
    )


def _with_id(session, event_id):
    row = _coarse(session, event_id)
    row.event_id = event_id
    session.add(row)
    session.commit()
    return row


def test_detect_is_quiet_when_there_is_nothing_to_remove(session):
    _with_id(session, f"cloud:{DIGEST}:m:msg_1")
    report = CloudCoarseEventsCheck().detect(session)
    assert report.total_count == 0 and report.groups == []


def test_detect_finds_the_first_collectors_events(session):
    _with_id(session, f"cloud:{DIGEST}:1")
    _with_id(session, f"cloud:{DIGEST}:2")
    _with_id(session, f"cloud:{DIGEST}:m:msg_1")
    report = CloudCoarseEventsCheck().detect(session)
    assert report.total_count == 2
    group = report.groups[0]
    assert group.key == GROUP_KEY and group.fixable
    assert group.detail == {"events": 2, "sessions": 1}
    assert {s.label for s in group.samples} == {f"cloud:{DIGEST}:1", f"cloud:{DIGEST}:2"}


def test_detect_ignores_the_old_state_tables(session):
    session.add(ClaudeCloudSessionUsage(account_id="holder@example.com", session_digest=DIGEST))
    session.commit()
    assert CloudCoarseEventsCheck().detect(session).total_count == 0


def test_plan_is_read_only_and_asks_for_confirmation(session):
    _with_id(session, f"cloud:{DIGEST}:1")
    plan = CloudCoarseEventsCheck().plan(session, GROUP_KEY, {})
    assert plan.counts["events"] == 1 and plan.confirmation_text
    assert len(session.exec(select(UsageEvent)).all()) == 1


def test_apply_removes_the_events(session):
    _with_id(session, f"cloud:{DIGEST}:1")
    keep_id = _with_id(session, f"cloud:{DIGEST}:m:msg_1").event_id
    result, hooks = CloudCoarseEventsCheck().apply(session, GROUP_KEY, {})
    assert hooks == [] and result.counts["events_deleted"] == 1
    assert [e.event_id for e in session.exec(select(UsageEvent))] == [keep_id]
    assert CloudCoarseEventsCheck().detect(session).total_count == 0
