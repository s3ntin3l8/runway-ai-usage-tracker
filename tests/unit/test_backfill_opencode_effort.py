"""Tests for the OpenCode effort backfill — match, guard, dry-run, idempotency."""

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import UsageEvent
from app.models.schemas import UsageEventPush
from scripts import backfill_opencode_effort as bf

_TS = datetime(2026, 5, 8, 14, 0, 0, tzinfo=UTC)


def _session():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _add(session, event_id, *, provider_id="opencode", account_id="default", effort=None):
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id=account_id,
            event_id=event_id,
            ts=_TS,
            kind="message",
            effort=effort,
        )
    )
    session.commit()


def _push(event_id, *, effort=None, account_id="default", provider_id="opencode"):
    return UsageEventPush(
        provider_id=provider_id,
        account_id=account_id,
        event_id=event_id,
        ts="2026-05-08T14:00:00+00:00",
        effort=effort,
    )


def test_updates_null_effort_and_skips_filled():
    s = _session()
    _add(s, "match")  # effort NULL → should be filled
    _add(s, "already", effort="low")  # row has low, push has high → update
    _add(s, "nolog")  # NULL but no matching push → stays NULL
    # Non-opencode provider is out of scope for the SQL scan.
    _add(s, "other", provider_id="anthropic")

    pushes = {
        "match": _push("match", effort="high"),
        "already": _push("already", effort="high"),
        "other": _push("other", effort="medium"),
    }

    changed, touched = bf.phase_b_effort(s, pushes, dry_run=False)
    # "match" (null→high) and "already" (low→high) change; "other" is out of scope.
    assert changed == 2
    assert touched == {"opencode"}

    rows = {r.event_id: r for r in s.exec(select(UsageEvent)).all()}
    assert rows["match"].effort == "high"
    assert rows["already"].effort == "high"
    assert rows["nolog"].effort is None
    assert rows["other"].effort is None  # anthropic never selected


def test_covers_opencode_prefix_and_canonical_retags():
    s = _session()
    _add(s, "msg_go", provider_id="opencode")
    _add(s, "msg_free", provider_id="opencode-free")
    _add(s, "msg_minimax", provider_id="minimax")
    _add(s, "msg_kimi", provider_id="kimi_coding")
    _add(s, "msg_unrelated", provider_id="anthropic")

    pushes = {
        "msg_go": _push("msg_go", effort="high"),
        "msg_free": _push("msg_free", effort="medium"),
        "msg_minimax": _push("msg_minimax", effort="high"),
        "msg_kimi": _push("msg_kimi", effort="medium"),
        "msg_unrelated": _push("msg_unrelated", effort="high"),
    }

    changed, touched = bf.phase_b_effort(s, pushes, dry_run=False)
    assert changed == 4
    assert touched == {"opencode", "opencode-free", "minimax", "kimi_coding"}

    rows = {(r.provider_id, r.event_id): r for r in s.exec(select(UsageEvent)).all()}
    assert rows[("opencode", "msg_go")].effort == "high"
    assert rows[("opencode-free", "msg_free")].effort == "medium"
    assert rows[("minimax", "msg_minimax")].effort == "high"
    assert rows[("kimi_coding", "msg_kimi")].effort == "medium"
    assert rows[("anthropic", "msg_unrelated")].effort is None


def test_dry_run_writes_nothing():
    s = _session()
    _add(s, "match")
    pushes = {"match": _push("match", effort="high")}

    changed, touched = bf.phase_b_effort(s, pushes, dry_run=True)
    assert changed == 1
    assert touched == {"opencode"}  # reported for Phase C messaging even on dry-run
    assert s.exec(select(UsageEvent)).first().effort is None  # nothing persisted


def test_backfill_is_idempotent():
    s = _session()
    _add(s, "match")
    pushes = {"match": _push("match", effort="high")}

    changed1, _ = bf.phase_b_effort(s, pushes, dry_run=False)
    assert changed1 == 1
    # Second run: effort already matches → 0 changes.
    changed2, _ = bf.phase_b_effort(s, pushes, dry_run=False)
    assert changed2 == 0
    assert s.exec(select(UsageEvent)).first().effort == "high"


def test_prefers_account_match_on_shared_event_id():
    """Same event_id under two accounts: prefer the row matching the push's account."""
    s = _session()
    _add(s, "shared", account_id="other@host", effort=None)
    _add(s, "shared", account_id="user@opencode.test", effort=None)

    pushes = {"shared": _push("shared", effort="high", account_id="user@opencode.test")}

    changed, _ = bf.phase_b_effort(s, pushes, dry_run=False)
    assert changed == 1

    rows = {r.account_id: r for r in s.exec(select(UsageEvent)).all()}
    assert rows["user@opencode.test"].effort == "high"
    assert rows["other@host"].effort is None


def test_main_dry_run_flag(monkeypatch):
    """argparse --dry-run routes through run() without raising."""
    called = {}

    def _fake_run(db_path, dry_run, skip_rollups):
        called.update(db_path=db_path, dry_run=dry_run, skip_rollups=skip_rollups)

    monkeypatch.setattr(bf, "run", _fake_run)
    assert bf.main(["--dry-run"]) == 0
    assert called["dry_run"] is True
    assert called["db_path"] is None
    assert called["skip_rollups"] is False
