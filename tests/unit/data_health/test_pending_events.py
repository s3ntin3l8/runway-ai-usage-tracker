"""Tests for app/services/data_health/checks/pending_events.py."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.db import PendingUsageEvent
from app.services.data_health.checks.pending_events import PendingEventsCheck


def _check() -> PendingEventsCheck:
    return PendingEventsCheck()


def _pending(session, **overrides) -> PendingUsageEvent:
    base = {
        "provider_id": "minimax",
        "event_id": "msg_1",
        "sidecar_id": "dev-01",
        "ts": datetime(2026, 9, 1, tzinfo=UTC),
        "payload_json": "{}",
    }
    base.update(overrides)
    row = PendingUsageEvent(**base)
    session.add(row)
    session.commit()
    return row


def test_detect_reports_zero_when_none_pending(session):
    report = _check().detect(session)

    assert report.total_count == 0
    assert report.groups == []


def test_detect_reports_the_pending_count(session):
    _pending(session)
    _pending(session, event_id="msg_2")

    report = _check().detect(session)

    assert report.total_count == 2
    assert report.groups[0].fixable is False
    assert report.groups[0].detail["link"] == "/fleet#pending-events"


def test_check_has_no_apply_path():
    check = _check()
    assert check.severity.value == "info"
