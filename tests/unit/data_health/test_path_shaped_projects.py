"""Tests for app/services/data_health/checks/path_shaped_projects.py."""

from __future__ import annotations

from sqlmodel import select

from app.models.db import UsageEvent
from app.services.data_health.checks.path_shaped_projects import PathShapedProjectsCheck
from tests.unit.data_health.conftest import make_event


def _check() -> PathShapedProjectsCheck:
    return PathShapedProjectsCheck()


def _path_shaped(session, event_id: str, cwd: str, project: str, **overrides):
    make_event(
        session,
        event_id=event_id,
        session_id="s1",
        cwd=cwd,
        project=project,
        **overrides,
    )


def test_detect_reports_a_path_shaped_label(session):
    _path_shaped(session, "1", r"D:\Projects\repo", r"D:\Projects\repo")

    report = _check().detect(session)

    assert report.total_count == 1
    assert report.groups[0].key == "path_shaped_projects"
    assert report.groups[0].count == 1
    assert report.groups[0].fixable is True


def test_detect_ignores_a_clean_label(session):
    _path_shaped(session, "1", "/home/u/repo", "repo")

    report = _check().detect(session)

    assert report.total_count == 0
    assert report.groups == []


def test_detect_reports_the_label_mapping(session):
    _path_shaped(session, "1", r"D:\Projects\repo", r"D:\Projects\repo")

    report = _check().detect(session)

    group = report.groups[0]
    assert group.samples[0].detail == {"from": r"D:\Projects\repo", "to": "repo", "rows": 1}
    assert group.detail["sessions"] == 1


def test_detect_reports_rows_it_cannot_relabel(session):
    _path_shaped(session, "1", "C:\\", "C:\\")

    report = _check().detect(session)

    assert report.groups[0].fixable is False
    assert report.groups[0].not_fixable_reason


def test_plan_is_read_only(session):
    _path_shaped(session, "1", r"D:\Projects\repo", r"D:\Projects\repo")

    plan = _check().plan(session, "path_shaped_projects", {})

    assert plan.counts["path_shaped_rows"] == 1
    ev = session.exec(select(UsageEvent)).one()
    assert ev.project == r"D:\Projects\repo"


def test_apply_relabels_and_clears_the_finding(session):
    _path_shaped(session, "1", r"D:\Projects\repo", r"D:\Projects\repo")

    result, hooks = _check().apply(session, "path_shaped_projects", {})

    assert result.counts["rows_relabeled"] == 1
    assert hooks == []
    ev = session.exec(select(UsageEvent)).one()
    assert ev.project == "repo"
    assert _check().detect(session).total_count == 0


def test_apply_leaves_the_unfixable_group_alone(session):
    _path_shaped(session, "1", "C:\\", "C:\\")

    result, _hooks = _check().apply(session, "path_shaped_projects_unfixable", {})

    assert result.counts["rows_relabeled"] == 0
    ev = session.exec(select(UsageEvent)).one()
    assert ev.project == "C:\\"
