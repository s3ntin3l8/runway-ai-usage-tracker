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
    # Model a pre-migration DB: the scripts under test clean up
    # cross-account duplicate events that the (provider_id, event_id)
    # unique index now prevents on fresh databases.
    with engine.begin() as _conn:
        _conn.exec_driver_sql("DROP INDEX uq_usage_events_provider_event")
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


def test_prefers_provider_match_on_shared_event_id():
    """Same event_id under opencode + kimi_coding: the push's provider wins.

    The push's provider (``opencode``) sorts *after* ``kimi_coding`` in the
    SQL ``ORDER BY``, so without ``_pick_target``'s provider preference the
    kimi_coding row would be picked first and this test would fail.
    """
    s = _session()
    _add(s, "shared", provider_id="opencode", effort=None)
    _add(s, "shared", provider_id="kimi_coding", effort=None)

    pushes = {"shared": _push("shared", effort="medium", provider_id="opencode")}

    changed, touched = bf.phase_b_effort(s, pushes, dry_run=False)
    assert changed == 1
    assert touched == {"opencode"}

    rows = {r.provider_id: r for r in s.exec(select(UsageEvent)).all()}
    assert rows["opencode"].effort == "medium"
    assert rows["kimi_coding"].effort is None


def test_null_push_does_not_clear_existing_effort():
    """Fill-only: a push with no variant must not wipe a non-NULL effort."""
    s = _session()
    _add(s, "filled", effort="high")
    _add(s, "empty", effort=None)

    pushes = {
        "filled": _push("filled", effort=None),
        "empty": _push("empty", effort=None),
    }

    changed, touched = bf.phase_b_effort(s, pushes, dry_run=False)
    assert changed == 0
    assert touched == set()

    rows = {r.event_id: r for r in s.exec(select(UsageEvent)).all()}
    assert rows["filled"].effort == "high"
    assert rows["empty"].effort is None


def test_missing_db_override_exits_nonzero(tmp_path, monkeypatch):
    """A typo'd --db must not read as success."""
    monkeypatch.setattr(bf, "init_db", lambda: None)
    missing = tmp_path / "nope.db"
    assert bf.main(["--db", str(missing)]) == 1


def test_collect_pushes_resolves_identity_per_path(monkeypatch):
    """Each DB path resolves its own account_id — not db_paths[0] for all."""
    from pathlib import Path

    seen: list[str] = []

    def _account(path):
        seen.append(str(path))
        return f"acct-for-{path.name}"

    def _parse(path, *, account_id, since):
        return [
            UsageEventPush(
                provider_id="opencode",
                account_id=account_id,
                event_id=f"ev-{path.name}",
                ts="2026-05-08T14:00:00+00:00",
                effort="high",
            )
        ]

    monkeypatch.setattr(bf, "_opencode_account_email", _account)
    monkeypatch.setattr(bf, "parse_opencode_events", _parse)

    paths = [Path("/tmp/a.db"), Path("/tmp/b.db")]
    pushes = bf._collect_pushes(paths)

    assert seen == ["/tmp/a.db", "/tmp/b.db"]
    assert pushes["ev-a.db"].account_id == "acct-for-a.db"
    assert pushes["ev-b.db"].account_id == "acct-for-b.db"


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


def test_run_end_to_end_dry_run_and_apply(tmp_path, monkeypatch):
    """Exercise run() itself: discovery → collect → phase B → (optionally) rollups."""
    import sqlite3

    from app.core.db import configure_sqlite_engine

    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        (
            "msg1",
            "ses1",
            1778248860000,
            1778248860000,
            '{"role":"assistant","providerID":"opencode","modelID":"glm",'
            '"tokens":{"input":10,"output":5},"variant":"high"}',
        ),
    )
    conn.commit()
    conn.close()

    # Point the module's engine at an isolated file DB under tmp_path;
    # skip init_db() (it would touch the shared app engine / lock the test DB).
    monkeypatch.setattr(bf, "init_db", lambda: None)
    test_engine = create_engine(f"sqlite:///{tmp_path}/runway.db", connect_args=SQLITE_CONNECT_ARGS)
    configure_sqlite_engine(test_engine)
    SQLModel.metadata.create_all(test_engine)
    # Model a pre-migration DB: the scripts under test clean up
    # cross-account duplicate events that the (provider_id, event_id)
    # unique index now prevents on fresh databases.
    with test_engine.begin() as _conn:
        _conn.exec_driver_sql("DROP INDEX uq_usage_events_provider_event")
    monkeypatch.setattr(bf, "engine", test_engine)

    session = Session(test_engine)
    pushes_probe = bf._collect_pushes([db_path])
    assert pushes_probe, "fixture should parse at least one push"
    real_eid = next(iter(pushes_probe))
    _add(session, real_eid, effort=None)

    rollup_calls: list[list[str]] = []
    monkeypatch.setattr(
        bf,
        "backfill_rollups",
        lambda providers: rollup_calls.append(list(providers)) or 0,
    )

    # Dry-run: no write, no rollups.
    bf.run(db_path=db_path, dry_run=True, skip_rollups=False)
    with Session(test_engine) as verify:
        row = verify.exec(select(UsageEvent).where(UsageEvent.event_id == real_eid)).one()
        assert row.effort is None
    assert rollup_calls == []

    # Apply: writes effort and rebuilds rollups for touched providers.
    bf.run(db_path=db_path, dry_run=False, skip_rollups=False)
    with Session(test_engine) as verify:
        row = verify.exec(select(UsageEvent).where(UsageEvent.event_id == real_eid)).one()
        assert row.effort == pushes_probe[real_eid].effort
        assert rollup_calls and rollup_calls[0] == sorted({row.provider_id})

    # Apply with --skip-rollups: effort already set (idempotent), no Phase C.
    rollup_calls.clear()
    bf.run(db_path=db_path, dry_run=False, skip_rollups=True)
    assert rollup_calls == []
