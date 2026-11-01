"""Tests for app/services/maintenance/cloud_session_lead_repo.py — the relabel
of a multi-repository Claude cloud session's unlabelled events with the
repository its labelled events name most often.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import UsageEvent
from app.services.maintenance.cloud_session_lead_repo import (
    apply_cloud_session_lead_repo,
    plan_cloud_session_lead_repo,
    plan_cloud_session_lead_repos,
)

_TS = datetime(2026, 5, 8, 14, 0, 0, tzinfo=UTC)

#: A cloud session's stored id is a sha256 digest, never the raw session id.
_DIGEST = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
_OTHER_DIGEST = "fedcba9876543210fedcba9876543210"  # pragma: allowlist secret


def _session():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _cloud_event(
    session, event_id: str, *, session_id: str = _DIGEST, project=None, project_ref=None
):
    """A cloud-runner event: ids `cloud:<digest>:m:<message id>`."""
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc",
            event_id=event_id,
            ts=_TS,
            kind="message",
            session_id=session_id,
            sidecar_id="anthropic-cloud",
            cwd=None,
            project=project,
            project_ref=project_ref,
            tokens_input=100,
            cost_usd=0.05,
            attribution_source="default",
        )
    )
    session.commit()


def _message_event(
    session, event_id: str, *, session_id: str = _DIGEST, project=None, project_ref=None
):
    """A cloud-runner remainder event: ids `cloud:<digest>:r:<ordinal>:<slug>`."""
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc",
            event_id=event_id,
            ts=_TS,
            kind="message",
            session_id=session_id,
            sidecar_id="anthropic-cloud",
            cwd=None,
            project=project,
            project_ref=project_ref,
            tokens_input=10,
            cost_usd=0.01,
            attribution_source="default",
        )
    )
    session.commit()


def _plain_event(session, event_id: str, *, project=None, project_ref=None):
    """A non-cloud event."""
    session.add(
        UsageEvent(
            provider_id="minimax",
            account_id="acc",
            event_id=event_id,
            ts=_TS,
            kind="message",
            session_id="s1",
            sidecar_id="dev-01",
            cwd="/home/u/repo",
            project=project,
            project_ref=project_ref,
            tokens_input=10,
            cost_usd=0.01,
            attribution_source="default",
        )
    )
    session.commit()


def _labels(session, session_id: str = _DIGEST):
    return {
        r.event_id: (r.project, r.project_ref)
        for r in session.exec(select(UsageEvent).where(UsageEvent.session_id == session_id)).all()
    }


class TestPlan:
    def test_the_lead_repository_is_the_one_the_most_events_name(self):
        s = _session()
        for index in range(3):
            _cloud_event(
                s, f"cloud:{_DIGEST}:m:labelled-{index}", project="demo-a", project_ref="demo-a"
            )
        for index in range(5):
            _cloud_event(s, f"cloud:{_DIGEST}:m:unlabelled-{index}")

        plan = plan_cloud_session_lead_repo(s, _DIGEST)

        assert plan.lead_repo == "demo-a"
        assert plan.labelled_events == 3
        assert plan.unlabelled_events == 5
        assert plan.candidate_repos == 1
        assert plan.skipped_reason is None
        assert plan.fixable is True

    def test_a_tie_is_left_alone(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
        _cloud_event(s, f"cloud:{_DIGEST}:m:b", project="demo-b", project_ref="demo-b")
        _cloud_event(s, f"cloud:{_DIGEST}:m:c")

        plan = plan_cloud_session_lead_repo(s, _DIGEST)

        assert plan.lead_repo is None
        assert plan.skipped_reason == "tie"
        assert plan.fixable is False

    def test_a_session_with_no_labelled_event_is_left_alone(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a")
        _cloud_event(s, f"cloud:{_DIGEST}:m:b")

        plan = plan_cloud_session_lead_repo(s, _DIGEST)

        assert plan.lead_repo is None
        assert plan.skipped_reason == "no_labelled_events"
        assert plan.unlabelled_events == 2
        assert plan.fixable is False

    def test_a_session_with_no_unlabelled_event_is_not_a_candidate(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")

        assert plan_cloud_session_lead_repos(s) == []

    def test_the_remainder_events_count_too(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
        _message_event(s, f"cloud:{_DIGEST}:r:0:demo-a")

        plan = plan_cloud_session_lead_repo(s, _DIGEST)

        assert plan.unlabelled_events == 1
        assert plan.lead_repo == "demo-a"

    def test_the_plan_does_not_write(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
        _cloud_event(s, f"cloud:{_DIGEST}:m:b")

        plan_cloud_session_lead_repos(s)

        assert _labels(s)[f"cloud:{_DIGEST}:m:b"] == (None, None)

    def test_a_non_cloud_event_is_not_a_candidate(self):
        s = _session()
        _plain_event(s, "plain-1")

        assert plan_cloud_session_lead_repos(s) == []


class TestApply:
    def test_the_unlabelled_events_take_the_lead_repository(self):
        s = _session()
        for index in range(3):
            _cloud_event(
                s, f"cloud:{_DIGEST}:m:labelled-{index}", project="demo-a", project_ref="demo-a"
            )
        for index in range(5):
            _cloud_event(s, f"cloud:{_DIGEST}:m:unlabelled-{index}")

        result = apply_cloud_session_lead_repo(s, _DIGEST)

        assert result.events_labelled == 5
        assert result.lead_repo == "demo-a"
        for index in range(5):
            assert _labels(s)[f"cloud:{_DIGEST}:m:unlabelled-{index}"] == ("demo-a", "demo-a")

    def test_the_apply_is_idempotent(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
        _cloud_event(s, f"cloud:{_DIGEST}:m:b")

        first = apply_cloud_session_lead_repo(s, _DIGEST)
        second = apply_cloud_session_lead_repo(s, _DIGEST)

        assert first.events_labelled == 1
        assert second.events_labelled == 0
        assert plan_cloud_session_lead_repos(s) == []

    def test_a_tie_is_not_touched(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
        _cloud_event(s, f"cloud:{_DIGEST}:m:b", project="demo-b", project_ref="demo-b")
        _cloud_event(s, f"cloud:{_DIGEST}:m:c")

        result = apply_cloud_session_lead_repo(s, _DIGEST)

        assert result.events_labelled == 0
        assert result.skipped_reason == "tie"
        assert _labels(s)[f"cloud:{_DIGEST}:m:c"] == (None, None)

    def test_a_session_with_no_labelled_event_is_not_touched(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a")

        result = apply_cloud_session_lead_repo(s, _DIGEST)

        assert result.events_labelled == 0
        assert result.skipped_reason == "no_labelled_events"
        assert _labels(s)[f"cloud:{_DIGEST}:m:a"] == (None, None)

    def test_another_session_is_left_alone(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
        _cloud_event(s, f"cloud:{_DIGEST}:m:b")
        _cloud_event(s, f"cloud:{_OTHER_DIGEST}:m:c", session_id=_OTHER_DIGEST)

        result = apply_cloud_session_lead_repo(s, _DIGEST)

        assert result.events_labelled == 1
        assert _labels(s, _OTHER_DIGEST)[f"cloud:{_OTHER_DIGEST}:m:c"] == (None, None)

    def test_an_event_that_carries_another_repository_keeps_it(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
        _cloud_event(s, f"cloud:{_DIGEST}:m:b", project="demo-b", project_ref="demo-b")
        _cloud_event(s, f"cloud:{_DIGEST}:m:c")

        apply_cloud_session_lead_repo(s, _DIGEST)

        assert _labels(s)[f"cloud:{_DIGEST}:m:b"] == ("demo-b", "demo-b")

    def test_a_non_cloud_event_is_not_touched(self):
        s = _session()
        _cloud_event(s, f"cloud:{_DIGEST}:m:a", project="demo-a", project_ref="demo-a")
        _cloud_event(s, f"cloud:{_DIGEST}:m:b")
        _plain_event(s, "plain-1")

        apply_cloud_session_lead_repo(s, _DIGEST)

        plain = s.exec(select(UsageEvent).where(UsageEvent.event_id == "plain-1")).one()
        assert (plain.project, plain.project_ref) == (None, None)
