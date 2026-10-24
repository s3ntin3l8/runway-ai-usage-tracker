"""Tests for app/services/data_health/checks/inconsistent_project_labels.py."""

from __future__ import annotations

from sqlmodel import select

from app.models.db import UsageEvent
from app.services.data_health.checks.inconsistent_project_labels import (
    InconsistentProjectLabelsCheck,
)
from tests.unit.data_health.conftest import make_event


def _check() -> InconsistentProjectLabelsCheck:
    return InconsistentProjectLabelsCheck()


def _event(session, event_id: str, *, cwd: str, project: str, project_ref: str | None = None):
    make_event(
        session,
        event_id=event_id,
        session_id="s1",
        cwd=cwd,
        project=project,
        project_ref=project_ref,
    )


def test_detect_ignores_rows_without_a_reported_repository(session):
    _event(session, "1", cwd="/home/u/ai-usage-tracker", project="ai-usage-tracker")

    report = _check().detect(session)

    assert report.total_count == 0
    assert report.groups == []


def test_detect_reports_an_older_row_of_a_known_directory(session):
    _event(session, "1", cwd="/home/u/ai-usage-tracker", project="ai-usage-tracker")
    _event(
        session,
        "2",
        cwd="/home/u/ai-usage-tracker",
        project="runway-ai-usage-tracker",
        project_ref="runway-ai-usage-tracker",
    )

    report = _check().detect(session)

    assert report.total_count == 1
    assert report.groups[0].key == "inconsistent_project_labels"
    assert report.groups[0].fixable is True


def test_plan_is_read_only(session):
    _event(session, "1", cwd="/home/u/ai-usage-tracker", project="ai-usage-tracker")
    _event(
        session,
        "2",
        cwd="/home/u/ai-usage-tracker",
        project="runway-ai-usage-tracker",
        project_ref="runway-ai-usage-tracker",
    )

    plan = _check().plan(session, "inconsistent_project_labels", {})

    assert plan.counts["rows_to_relabel"] == 1
    ev = session.exec(select(UsageEvent).where(UsageEvent.event_id == "msg_1")).one()
    assert ev.project == "ai-usage-tracker"


def test_apply_moves_the_older_row_and_clears_the_finding(session):
    _event(session, "1", cwd="/home/u/ai-usage-tracker", project="ai-usage-tracker")
    _event(
        session,
        "2",
        cwd="/home/u/ai-usage-tracker",
        project="runway-ai-usage-tracker",
        project_ref="runway-ai-usage-tracker",
    )

    result, hooks = _check().apply(session, "inconsistent_project_labels", {})

    assert result.counts["rows_relabeled"] == 1
    assert hooks == []
    assert _check().detect(session).total_count == 0


def test_apply_unifies_a_folder_named_differently(session):
    _event(session, "1", cwd=r"C:\Users\u\Projects\ai-usage-tracker", project="ai-usage-tracker")
    _event(
        session,
        "2",
        cwd=r"C:\Users\u\Projects\ai-usage-tracker",
        project="runway-ai-usage-tracker",
        project_ref="runway-ai-usage-tracker",
    )

    _check().apply(session, "inconsistent_project_labels", {})

    projects = {r.event_id: r.project for r in session.exec(select(UsageEvent)).all()}
    assert set(projects.values()) == {"runway-ai-usage-tracker"}
