"""Tests for app/services/data_health/checks/cloud_session_lead_repo.py."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import select

from app.models.db import UsageEvent
from app.services.data_health.checks.cloud_session_lead_repo import (
    CloudSessionLeadRepoCheck,
)

_DIGEST = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret


def _cloud_event(session, event_id: str, *, project=None, project_ref=None, sid=_DIGEST):
    """A cloud-runner event.

    The row is built here rather than through `make_event`, which prefixes the
    event id with `msg_` — a cloud event's id starts with `cloud:`.
    """
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="default",
            event_id=event_id,
            ts=datetime(2026, 9, 1, tzinfo=UTC),
            kind="message",
            session_id=sid,
            sidecar_id="anthropic-cloud",
            cwd=None,
            project=project,
            project_ref=project_ref,
            model_id="claude-opus-5",
            tokens_input=100,
            tokens_output=20,
            cost_usd=0.05,
            attribution_source="default",
        )
    )
    session.commit()


def _labels(session, sid=_DIGEST):
    return {
        r.event_id: (r.project, r.project_ref)
        for r in session.exec(select(UsageEvent).where(UsageEvent.session_id == sid)).all()
    }


def test_detect_reports_a_session_with_unlabelled_events(session):
    for index in range(3):
        _cloud_event(
            session, f"cloud:{_DIGEST}:m:labelled-{index}", project="demo-a", project_ref="demo-a"
        )
    for index in range(5):
        _cloud_event(session, f"cloud:{_DIGEST}:m:unlabelled-{index}")

    report = CloudSessionLeadRepoCheck().detect(session)

    assert report.total_count == 5
    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.key == _DIGEST
    assert group.fixable is True
    assert group.count == 5
    assert group.samples[0].detail == {"rows": 5}


def test_the_label_and_the_detail_carry_counts_only(session):
    _cloud_event(session, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
    _cloud_event(session, f"cloud:{_DIGEST}:m:b")

    group = CloudSessionLeadRepoCheck().detect(session).groups[0]

    assert _DIGEST not in group.label
    assert "demo-a" not in group.label
    assert _DIGEST not in str(group.detail)
    assert "demo-a" not in str(group.detail)
    assert group.detail == {
        "unlabelled_events": 1,
        "labelled_events": 1,
        "candidate_repos": 1,
    }


def test_a_tie_is_reported_but_not_fixable(session):
    _cloud_event(session, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
    _cloud_event(session, f"cloud:{_DIGEST}:m:b", project="demo-b", project_ref="demo-b")
    _cloud_event(session, f"cloud:{_DIGEST}:m:c")

    report = CloudSessionLeadRepoCheck().detect(session)

    assert report.total_count == 1
    group = report.groups[0]
    assert group.fixable is False
    assert group.not_fixable_reason


def test_a_session_with_no_labelled_event_is_reported_but_not_fixable(session):
    _cloud_event(session, f"cloud:{_DIGEST}:m:a")

    group = CloudSessionLeadRepoCheck().detect(session).groups[0]

    assert group.fixable is False
    assert group.not_fixable_reason


def test_a_session_without_unlabelled_events_is_not_reported(session):
    _cloud_event(session, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")

    assert CloudSessionLeadRepoCheck().detect(session).groups == []


def test_plan_needs_a_confirmation_and_does_not_write(session):
    _cloud_event(session, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
    _cloud_event(session, f"cloud:{_DIGEST}:m:b")

    plan = CloudSessionLeadRepoCheck().plan(session, _DIGEST, {})

    assert plan.confirmation_text
    assert plan.counts["events_to_label"] == 1
    assert _labels(session)[f"cloud:{_DIGEST}:m:b"] == (None, None)


def test_apply_labels_the_session_and_a_rescan_finds_nothing(session):
    for index in range(3):
        _cloud_event(
            session, f"cloud:{_DIGEST}:m:labelled-{index}", project="demo-a", project_ref="demo-a"
        )
    for index in range(5):
        _cloud_event(session, f"cloud:{_DIGEST}:m:unlabelled-{index}")
    check = CloudSessionLeadRepoCheck()

    result, hooks = check.apply(session, _DIGEST, {})

    assert result.counts["events_labelled"] == 5
    assert hooks == []
    assert _labels(session)[f"cloud:{_DIGEST}:m:unlabelled-0"] == ("demo-a", "demo-a")
    assert check.detect(session).groups == []


def test_an_unknown_group_key_is_refused(session):
    _cloud_event(session, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
    _cloud_event(session, f"cloud:{_DIGEST}:m:b")
    check = CloudSessionLeadRepoCheck()

    assert check.plan(session, "other", {}).counts["events_to_label"] == 0
    result, _ = check.apply(session, "other", {})

    assert result.counts["events_labelled"] == 0
    assert _labels(session)[f"cloud:{_DIGEST}:m:b"] == (None, None)
