"""Tests for app/services/data_health/checks/cloud_events_reset.py."""

from __future__ import annotations

from sqlmodel import select

from app.models.db import ClaudeCloudSessionCheckpoint, UsageEvent
from app.services.data_health.checks.cloud_events_reset import GROUP_KEY, CloudEventsResetCheck
from tests.unit.data_health.conftest import make_event

DIGEST = "c" * 32


def _cloud(session, event_id, sidecar_id="server"):
    row = make_event(
        session,
        event_id="unused-" + event_id,
        provider_id="anthropic",
        account_id="holder@example.com",
        sidecar_id=sidecar_id,
        model_id="opus-5.5",
        entrypoint="cloud",
        session_id=DIGEST,
    )
    row.event_id = event_id
    session.add(row)
    session.commit()
    return row


def test_detect_is_quiet_without_cloud_events(session):
    report = CloudEventsResetCheck().detect(session)
    assert report.total_count == 0 and report.groups == []


def test_detect_is_quiet_once_events_carry_the_cloud_source_id(session):
    _cloud(session, f"cloud:{DIGEST}:m:msg_1", sidecar_id="anthropic-cloud")
    assert CloudEventsResetCheck().detect(session).total_count == 0


def test_detect_finds_events_under_the_old_source_id(session):
    _cloud(session, f"cloud:{DIGEST}:m:msg_1")
    _cloud(session, f"cloud:{DIGEST}:r:1:opus-5.5")
    _cloud(session, f"cloud:{DIGEST}:1")  # the first collector's: not this fix's business
    session.add(
        ClaudeCloudSessionCheckpoint(account_id="holder@example.com", session_digest=DIGEST)
    )
    session.commit()
    report = CloudEventsResetCheck().detect(session)
    assert report.total_count == 2
    group = report.groups[0]
    assert group.key == GROUP_KEY and group.fixable
    assert group.detail == {"events": 2, "sessions": 1, "checkpoints": 1, "windows": 0}


def test_plan_is_read_only_and_asks_for_confirmation(session):
    _cloud(session, f"cloud:{DIGEST}:m:msg_1")
    plan = CloudEventsResetCheck().plan(session, GROUP_KEY, {})
    assert plan.counts["events"] == 1 and "windows_to_rebuild" in plan.counts
    assert plan.confirmation_text
    assert len(session.exec(select(UsageEvent)).all()) == 1


def test_apply_removes_events_and_checkpoints_and_goes_quiet(session):
    _cloud(session, f"cloud:{DIGEST}:m:msg_1")
    keep = _cloud(session, f"cloud:{DIGEST}:1").event_id
    session.add(
        ClaudeCloudSessionCheckpoint(account_id="holder@example.com", session_digest=DIGEST)
    )
    session.commit()
    result, hooks = CloudEventsResetCheck().apply(session, GROUP_KEY, {})
    assert hooks == []
    assert result.counts["events_deleted"] == 1 and result.counts["checkpoints_deleted"] == 1
    assert [e.event_id for e in session.exec(select(UsageEvent))] == [keep]
    assert CloudEventsResetCheck().detect(session).total_count == 0
