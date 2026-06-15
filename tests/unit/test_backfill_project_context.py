"""Tests for the project-context backfill — match, guard, and idempotency."""

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import UsageEvent
from app.models.schemas import UsageEventPush
from scripts import backfill_project_context as bf

_TS = datetime(2026, 5, 8, 14, 0, 0, tzinfo=UTC)


def _session():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _add(session, event_id, *, cwd=None, project=None):
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="acc",
            event_id=event_id,
            ts=_TS,
            kind="message",
            cwd=cwd,
            project=project,
        )
    )
    session.commit()


def _push(event_id, **kw):
    return UsageEventPush(
        provider_id="anthropic",
        account_id="backfill",
        event_id=event_id,
        ts="2026-05-08T14:00:00+00:00",
        **kw,
    )


def test_enriches_null_rows_and_skips_filled(monkeypatch):
    s = _session()
    _add(s, "match")  # NULL cwd → should be enriched
    _add(s, "already", cwd="/old", project="old")  # already filled → untouched
    _add(s, "nolog")  # NULL but no matching push → stays NULL

    pushes = {
        "match": _push(
            "match", cwd="/home/u/repo/", git_branch="dev", tool_names=["Read"], latency_ms=99
        ),
        "already": _push("already", cwd="/should/not/apply"),
    }
    monkeypatch.setattr(
        bf, "_collect_pushes", lambda provider: pushes if provider == "anthropic" else {}
    )

    changed = bf.backfill(s, ["anthropic"], dry_run=False)
    assert changed == 1

    rows = {r.event_id: r for r in s.exec(select(UsageEvent)).all()}
    assert rows["match"].cwd == "/home/u/repo/"
    assert rows["match"].project == "repo"  # server-derived basename
    assert rows["match"].git_branch == "dev"
    assert rows["match"].tools_json == '["Read"]'
    assert rows["match"].latency_ms == 99
    # Pre-filled row is guarded by `cwd IS NULL` — never overwritten.
    assert rows["already"].cwd == "/old"
    assert rows["nolog"].cwd is None


def test_backfill_is_idempotent(monkeypatch):
    s = _session()
    _add(s, "match")
    pushes = {"match": _push("match", cwd="/home/u/repo")}
    monkeypatch.setattr(
        bf, "_collect_pushes", lambda provider: pushes if provider == "anthropic" else {}
    )

    assert bf.backfill(s, ["anthropic"], dry_run=False) == 1
    # Second run: the row now has cwd, so the NULL-guard skips it.
    assert bf.backfill(s, ["anthropic"], dry_run=False) == 0


def test_dry_run_writes_nothing(monkeypatch):
    s = _session()
    _add(s, "match")
    pushes = {"match": _push("match", cwd="/home/u/repo")}
    monkeypatch.setattr(
        bf, "_collect_pushes", lambda provider: pushes if provider == "anthropic" else {}
    )

    assert bf.backfill(s, ["anthropic"], dry_run=True) == 1
    assert s.exec(select(UsageEvent)).first().cwd is None  # nothing persisted
