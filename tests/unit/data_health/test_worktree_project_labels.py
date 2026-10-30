"""Tests for app/services/data_health/checks/worktree_project_labels.py."""

from __future__ import annotations

from sqlmodel import select

from app.models.db import UsageEvent
from app.services.data_health.checks.inconsistent_project_labels import (
    InconsistentProjectLabelsCheck,
)
from app.services.data_health.checks.worktree_project_labels import WorktreeProjectLabelsCheck
from tests.unit.data_health.conftest import make_event

_KEY = "worktree_project_labels"


def _event(session, event_id: str, *, cwd: str, project: str, project_ref=None, sid="s1"):
    make_event(
        session,
        event_id=event_id,
        session_id=sid,
        cwd=cwd,
        project=project,
        project_ref=project_ref,
    )


def test_detect_reports_a_worktree_label(session):
    _event(session, "1", cwd="/home/u/repo/.wt/feat-x", project="feat-x")

    report = WorktreeProjectLabelsCheck().detect(session)

    assert report.total_count == 1
    assert report.groups[0].key == _KEY
    assert report.groups[0].fixable is True
    assert report.groups[0].samples[0].detail == {"from": "feat-x", "to": "repo", "rows": 1}


def test_detect_ignores_a_clean_label(session):
    _event(session, "1", cwd="/home/u/repo", project="repo")

    assert WorktreeProjectLabelsCheck().detect(session).groups == []


def test_plan_needs_a_confirmation_and_does_not_write(session):
    _event(session, "1", cwd="/home/u/repo/.wt/feat-x", project="feat-x")

    plan = WorktreeProjectLabelsCheck().plan(session, _KEY, {})

    assert plan.confirmation_text
    assert plan.counts["rows_to_relabel"] == 1
    assert session.exec(select(UsageEvent)).one().project == "feat-x"


def test_apply_relabels_and_a_rescan_finds_nothing(session):
    _event(session, "1", cwd="/home/u/repo/.wt/feat-x", project="feat-x")
    check = WorktreeProjectLabelsCheck()

    result, hooks = check.apply(session, _KEY, {})

    assert result.counts["rows_relabeled"] == 1
    assert hooks == []
    assert session.exec(select(UsageEvent)).one().project == "repo"
    assert check.detect(session).groups == []


def test_an_unknown_group_key_is_refused(session):
    _event(session, "1", cwd="/home/u/repo/.wt/feat-x", project="feat-x")
    check = WorktreeProjectLabelsCheck()

    assert check.plan(session, "other", {}).counts["rows_to_relabel"] == 0
    result, _ = check.apply(session, "other", {})

    assert result.counts["rows_relabeled"] == 0
    assert session.exec(select(UsageEvent)).one().project == "feat-x"


def test_it_and_inconsistent_project_labels_do_not_undo_each_other(session):
    _event(session, "1", cwd="/home/u/folder/.wt/x", project="x")
    _event(session, "2", cwd="/home/u/folder/.wt/x", project="real-repo", project_ref="real-repo")
    worktree, inconsistent = WorktreeProjectLabelsCheck(), InconsistentProjectLabelsCheck()

    worktree.apply(session, _KEY, {})

    assert inconsistent.detect(session).groups == []
    assert {e.project for e in session.exec(select(UsageEvent)).all()} == {"real-repo"}


def test_plain_subfolder_drift_is_not_reported_or_rewritten(session):
    _event(session, "1", cwd="/home/u/alpha", project="alpha", sid="s2")
    _event(session, "2", cwd="/home/u/alpha/src", project="src", sid="s2")
    check = WorktreeProjectLabelsCheck()

    assert check.detect(session).groups == []
    check.apply(session, _KEY, {})

    assert {e.project for e in session.exec(select(UsageEvent)).all()} == {"alpha", "src"}


def test_a_configured_pattern_makes_a_sibling_worktree_relabel(session):
    import json

    from app.models.db import SystemConfig
    from app.services.worktree_patterns import invalidate_worktree_patterns

    _event(session, "1", cwd="/home/u/repo-wt/213-cross-host/app", project="app")
    check = WorktreeProjectLabelsCheck()
    assert check.detect(session).groups == []

    session.add(SystemConfig(worktree_patterns_json=json.dumps(["*-wt"])))
    session.commit()
    invalidate_worktree_patterns()

    report = check.detect(session)
    assert report.total_count == 1
    assert report.groups[0].samples[0].detail == {"from": "app", "to": "repo", "rows": 1}
    check.apply(session, _KEY, {})
    assert session.exec(select(UsageEvent)).one().project == "repo"
    assert check.detect(session).groups == []


def test_the_scope_prefilter_handles_like_metacharacters(session):
    import json

    from app.models.db import SystemConfig

    session.add(SystemConfig(worktree_patterns_json=json.dumps(["a_b%", "x*y"])))
    session.commit()
    _event(session, "1", cwd="/home/u/repo/a_b%/s", project="s")
    _event(session, "2", cwd="/home/u/repo2/xzzy", project="xzzy", sid="s2")
    _event(session, "3", cwd="/home/u/repo3/aXb%/s", project="s", sid="s3")

    plan = WorktreeProjectLabelsCheck().detect(session)
    assert plan.total_count == 2
