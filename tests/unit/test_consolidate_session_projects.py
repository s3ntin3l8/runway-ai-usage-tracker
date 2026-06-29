"""Tests for the per-session project consolidation — root pick, isolation, idempotency."""

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import UsageEvent
from scripts import consolidate_session_projects as cs

_TS = datetime(2026, 5, 8, 14, 0, 0, tzinfo=UTC)


def _session():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _add(session, event_id, *, session_id, cwd, project, provider="anthropic", tokens=10, cost=1.0):
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


def test_session_relabeled_to_root_including_null_cwd_row():
    s = _session()
    # One session drifting from root into a subfolder and a worktree, plus a
    # NULL-cwd row that must still inherit the canonical project.
    _add(s, "a", session_id="s1", cwd="/home/u/repo", project="repo")
    _add(s, "b", session_id="s1", cwd="/home/u/repo/services/api", project="api")
    _add(s, "c", session_id="s1", cwd="/home/u/repo/.claude/worktrees/x", project="x")
    _add(s, "d", session_id="s1", cwd=None, project=None)

    changed = cs.consolidate(s, ["anthropic"], dry_run=False)
    assert changed == 3  # row "a" already correct

    projects = _projects(s)
    assert projects == {"a": "repo", "b": "repo", "c": "repo", "d": "repo"}


def test_sessions_do_not_bleed_into_each_other():
    s = _session()
    _add(s, "a", session_id="s1", cwd="/home/u/repo-one/apps/web", project="web")
    _add(s, "b", session_id="s1", cwd="/home/u/repo-one", project="repo-one")
    _add(s, "c", session_id="s2", cwd="/home/u/repo-two/packages/db", project="db")
    _add(s, "d", session_id="s2", cwd="/home/u/repo-two", project="repo-two")

    cs.consolidate(s, ["anthropic"], dry_run=False)

    projects = _projects(s)
    assert projects["a"] == "repo-one"
    assert projects["b"] == "repo-one"
    assert projects["c"] == "repo-two"
    assert projects["d"] == "repo-two"


def test_dry_run_writes_nothing():
    s = _session()
    _add(s, "a", session_id="s1", cwd="/home/u/repo", project="repo")
    _add(s, "b", session_id="s1", cwd="/home/u/repo/services/api", project="api")

    assert cs.consolidate(s, ["anthropic"], dry_run=True) == 1
    assert _projects(s)["b"] == "api"  # unchanged


def test_idempotent():
    s = _session()
    _add(s, "a", session_id="s1", cwd="/home/u/repo", project="repo")
    _add(s, "b", session_id="s1", cwd="/home/u/repo/services/api", project="api")

    assert cs.consolidate(s, ["anthropic"], dry_run=False) == 1
    assert cs.consolidate(s, ["anthropic"], dry_run=False) == 0


def test_totals_unchanged():
    s = _session()
    _add(s, "a", session_id="s1", cwd="/home/u/repo", project="repo", tokens=10, cost=1.5)
    _add(
        s, "b", session_id="s1", cwd="/home/u/repo/services/api", project="api", tokens=20, cost=2.5
    )

    def totals():
        rows = s.exec(select(UsageEvent)).all()
        return sum(r.tokens_input for r in rows), sum(r.cost_usd for r in rows)

    before = totals()
    cs.consolidate(s, ["anthropic"], dry_run=False)
    assert totals() == before


def test_session_with_no_usable_cwd_is_left_alone():
    s = _session()
    _add(s, "a", session_id="s1", cwd=None, project=None)
    _add(s, "b", session_id="s1", cwd="/", project=None)

    assert cs.consolidate(s, ["anthropic"], dry_run=False) == 0


def test_provider_filter_scopes_rows():
    s = _session()
    _add(s, "a", session_id="s1", cwd="/home/u/repo/apps/web", project="web", provider="anthropic")
    _add(s, "b", session_id="s1", cwd="/home/u/repo", project="repo", provider="anthropic")
    _add(s, "c", session_id="s2", cwd="/home/u/other/svc", project="svc", provider="opencode")
    _add(s, "d", session_id="s2", cwd="/home/u/other", project="other", provider="opencode")

    # Only anthropic in scope → opencode rows untouched.
    cs.consolidate(s, ["anthropic"], dry_run=False)
    projects = _projects(s)
    assert projects["a"] == "repo"
    assert projects["c"] == "svc"  # opencode left as-is

    # None → all providers.
    cs.consolidate(s, None, dry_run=False)
    assert _projects(s)["c"] == "other"
