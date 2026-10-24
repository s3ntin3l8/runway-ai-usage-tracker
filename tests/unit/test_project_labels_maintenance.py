"""Tests for app/services/maintenance/project_labels.py — the relabel of the
events whose `project` label is a path, and the consolidation it shares with
the CLI wrapper.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import UsageEvent
from app.services.maintenance.project_labels import (
    _is_path_shaped,
    _path_shaped_filter,
    apply_path_shaped_projects,
    apply_repo_relabels,
    plan_path_shaped_projects,
    plan_repo_relabels,
    plan_session_relabels,
)

_TS = datetime(2026, 5, 8, 14, 0, 0, tzinfo=UTC)


def _session():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _add(
    session,
    event_id,
    *,
    session_id,
    cwd,
    project,
    provider="minimax",
    tokens=10,
    cost=1.0,
):
    session.add(
        UsageEvent(
            provider_id=provider,
            account_id="acc",
            event_id=event_id,
            ts=_TS,
            kind="message",
            session_id=session_id,
            cwd=cwd,
            project=project,
            tokens_input=tokens,
            cost_usd=cost,
        )
    )
    session.commit()


def _projects(session):
    return {r.event_id: r.project for r in session.exec(select(UsageEvent)).all()}


class TestPathShapedRule:
    """The scope is picked in SQL and the rows are counted in Python — the two
    must be the same rule, or the fix reports rows it did not move."""

    def test_the_sql_filter_and_the_python_rule_agree(self):
        s = _session()
        labels = ["repo", r"D:\repo", "D:/repo", r"C:\Users\u\repo", None, "", "a b"]
        for index, label in enumerate(labels):
            _add(s, str(index), session_id=f"s{index}", cwd="/home/u/repo", project=label)

        sql_rows = {row.id for row in s.exec(select(UsageEvent).where(_path_shaped_filter())).all()}
        python_rows = {
            row.id for row in s.exec(select(UsageEvent)).all() if _is_path_shaped(row.project)
        }

        assert sql_rows == python_rows
        assert len(sql_rows) == 3  # the three path-shaped labels


class TestPlanPathShapedProjects:
    def test_a_path_shaped_label_is_found(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd=r"D:\Projects\repo", project=r"D:\Projects\repo")

        plan = plan_path_shaped_projects(s)

        assert plan.path_shaped_rows == 1
        assert plan.sessions == 1
        assert plan.label_map == [(r"D:\Projects\repo", "repo", 1)]

    def test_a_clean_label_is_not_found(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd="/home/u/repo", project="repo")

        plan = plan_path_shaped_projects(s)

        assert plan.path_shaped_rows == 0
        assert plan.sessions == 0

    def test_a_session_without_a_usable_cwd_is_reported_unfixable(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd="C:\\", project="C:\\")

        plan = plan_path_shaped_projects(s)

        assert plan.path_shaped_rows == 0
        assert plan.unfixable_rows == 1
        assert plan.unfixable_sessions == 1

    def test_the_plan_does_not_write(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd=r"D:\Projects\repo", project=r"D:\Projects\repo")

        plan_path_shaped_projects(s)

        assert _projects(s) == {"a": r"D:\Projects\repo"}


class TestApplyPathShapedProjects:
    def test_the_session_is_relabelled_with_its_project_name(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd=r"D:\Projects\repo", project=r"D:\Projects\repo")
        _add(s, "b", session_id="s1", cwd=None, project=None)

        result = apply_path_shaped_projects(s, plan_path_shaped_projects(s))

        assert result.relabeled == 2
        assert _projects(s) == {"a": "repo", "b": "repo"}

    def test_a_drifted_cwd_collapses_onto_the_project_root(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd=r"D:\repo", project=r"D:\repo")
        _add(s, "b", session_id="s1", cwd=r"D:\repo\apps\web", project=r"D:\repo\apps\web")

        apply_path_shaped_projects(s, plan_path_shaped_projects(s))

        assert _projects(s) == {"a": "repo", "b": "repo"}

    def test_a_clean_label_is_left_alone(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd="/home/u/repo", project="repo")

        result = apply_path_shaped_projects(s, plan_path_shaped_projects(s))

        assert result.relabeled == 0
        assert _projects(s) == {"a": "repo"}

    def test_the_apply_is_idempotent(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd=r"D:\repo", project=r"D:\repo")

        first = apply_path_shaped_projects(s, plan_path_shaped_projects(s))
        second = apply_path_shaped_projects(s, plan_path_shaped_projects(s))

        assert first.relabeled == 1
        assert second.relabeled == 0

    def test_totals_are_unchanged(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd=r"D:\repo", project=r"D:\repo", tokens=10, cost=1.5)
        _add(s, "b", session_id="s1", cwd=None, project=None, tokens=20, cost=2.5)

        def totals():
            rows = s.exec(select(UsageEvent)).all()
            return sum(r.tokens_input for r in rows), sum(r.cost_usd for r in rows)

        before = totals()
        apply_path_shaped_projects(s, plan_path_shaped_projects(s))
        assert totals() == before

    def test_the_raw_cwd_is_kept(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd=r"D:\repo", project=r"D:\repo")

        apply_path_shaped_projects(s, plan_path_shaped_projects(s))

        row = s.exec(select(UsageEvent)).one()
        assert row.cwd == r"D:\repo"


class TestPlanSessionRelabels:
    def test_a_scattered_session_is_planned_even_without_a_path(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd="/home/u/repo", project="repo")
        _add(s, "b", session_id="s1", cwd="/home/u/repo/services/api", project="api")

        plan = plan_session_relabels(s)

        assert plan.sessions == 1
        assert plan.rows_to_relabel == 1
        assert plan.label_map == [("api", "repo", 1)]

    def test_the_provider_filter_scopes_the_plan(self):
        s = _session()
        _add(
            s,
            "a",
            session_id="s1",
            cwd="/home/u/repo/apps/web",
            project="web",
            provider="anthropic",
        )
        _add(s, "b", session_id="s1", cwd="/home/u/repo", project="repo", provider="anthropic")
        _add(s, "c", session_id="s2", cwd="/home/u/other/svc", project="svc", provider="opencode")

        plan = plan_session_relabels(s, providers=["anthropic"])

        assert plan.sessions == 1
        assert plan.label_map == [("web", "repo", 1)]


class TestRepoRelabels:
    """The rows collected before the client reported a repository name."""

    def test_a_row_without_a_reported_repository_is_left_alone(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd="/home/u/ai-usage-tracker", project="ai-usage-tracker")

        plan = plan_repo_relabels(s)

        assert plan.total == 0
        assert plan.relabels == []

    def test_rows_already_following_the_repository_are_left_alone(self):
        s = _session()
        _add(
            s,
            "a",
            session_id="s1",
            cwd="/home/u/ai-usage-tracker",
            project="runway-ai-usage-tracker",
            project_ref="runway-ai-usage-tracker",
        )

        plan = plan_repo_relabels(s)

        assert plan.total == 0

    def test_an_older_row_of_the_same_directory_is_planned(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd="/home/u/ai-usage-tracker", project="ai-usage-tracker")
        _add(
            s,
            "b",
            session_id="s2",
            cwd="/home/u/ai-usage-tracker",
            project="runway-ai-usage-tracker",
            project_ref="runway-ai-usage-tracker",
        )

        plan = plan_repo_relabels(s)

        assert plan.total == 1
        assert plan.relabels[0].project_ref == "runway-ai-usage-tracker"
        assert plan.relabels[0].rows == 2

    def test_the_apply_moves_the_older_row(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd="/home/u/ai-usage-tracker", project="ai-usage-tracker")
        _add(
            s,
            "b",
            session_id="s2",
            cwd="/home/u/ai-usage-tracker",
            project="runway-ai-usage-tracker",
            project_ref="runway-ai-usage-tracker",
        )

        result = apply_repo_relabels(s, plan_repo_relabels(s))

        assert result.relabeled == 1
        assert _projects(s) == {"a": "runway-ai-usage-tracker", "b": "runway-ai-usage-tracker"}

    def test_the_apply_rewrites_the_label_of_a_folder_named_differently(self):
        s = _session()
        _add(
            s,
            "a",
            session_id="s1",
            cwd=r"C:\Users\u\Projects\ai-usage-tracker",
            project="ai-usage-tracker",
        )
        _add(
            s,
            "b",
            session_id="s2",
            cwd=r"C:\Users\u\Projects\ai-usage-tracker",
            project="runway-ai-usage-tracker",
            project_ref="runway-ai-usage-tracker",
        )

        apply_repo_relabels(s, plan_repo_relabels(s))

        assert _projects(s) == {
            "a": "runway-ai-usage-tracker",
            "b": "runway-ai-usage-tracker",
        }

    def test_the_apply_is_idempotent(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd="/home/u/ai-usage-tracker", project="ai-usage-tracker")
        _add(
            s,
            "b",
            session_id="s2",
            cwd="/home/u/ai-usage-tracker",
            project="runway-ai-usage-tracker",
            project_ref="runway-ai-usage-tracker",
        )

        first = apply_repo_relabels(s, plan_repo_relabels(s))
        second = apply_repo_relabels(s, plan_repo_relabels(s))

        assert first.relabeled == 1
        assert second.relabeled == 0

    def test_the_raw_context_is_kept(self):
        s = _session()
        _add(s, "a", session_id="s1", cwd="/home/u/ai-usage-tracker", project="ai-usage-tracker")
        _add(
            s,
            "b",
            session_id="s2",
            cwd="/home/u/ai-usage-tracker",
            project="runway-ai-usage-tracker",
            project_ref="runway-ai-usage-tracker",
        )

        apply_repo_relabels(s, plan_repo_relabels(s))

        rows = {r.event_id: r for r in s.exec(select(UsageEvent)).all()}
        assert rows["a"].cwd == "/home/u/ai-usage-tracker"
        assert rows["a"].project_ref is None
