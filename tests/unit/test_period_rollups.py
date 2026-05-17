from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.period_rollups import update_rollups_for_event


def _engine():
    e = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(e)
    return e


def _session_from(engine):
    return Session(engine)


def _session():
    engine = _engine()
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _make_event(event_id="msg_x", **overrides):
    base = {
        "provider_id": "anthropic",
        "account_id": "user@x.com",
        "sidecar_id": "dev-01",
        "event_id": event_id,
        "ts": datetime(2026, 5, 8, 14, 23, tzinfo=UTC),
        "model_id": "sonnet",
        "tokens_input": 100,
        "tokens_output": 200,
        "tokens_cache_read": 0,
        "tokens_cache_create": 0,
        "tokens_reasoning": 0,
        "cost_usd": 0.018,
    }
    base.update(overrides)
    return UsageEvent(**base)


def test_single_event_creates_grain_rows():
    """One event creates 4 rollup rows per period: ('',''), (model,''), ('',sidecar), (model,sidecar)."""
    s = _session()
    e = UsageEvent(
        provider_id="anthropic",
        account_id="user@x.com",
        sidecar_id="dev-01",
        event_id="msg_1",
        ts=datetime(2026, 5, 8, 14, 23, tzinfo=UTC),
        model_id="sonnet",
        tokens_input=100,
        tokens_output=200,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
        cost_usd=0.018,
    )
    s.add(e)
    s.commit()
    s.refresh(e)
    update_rollups_for_event(s, e)

    rows = s.exec(select(UsagePeriodRollup)).all()
    # Periods: hour, day, month, year, lifetime = 5
    # Grains per period: ('',''), ('sonnet',''), ('','dev-01'), ('sonnet','dev-01') = 4
    # Total = 20
    assert len(rows) == 20


def test_two_events_same_period_increment():
    s = _session()
    for i in range(2):
        e = UsageEvent(
            provider_id="anthropic",
            account_id="user@x.com",
            sidecar_id="dev-01",
            event_id=f"msg_{i}",
            ts=datetime(2026, 5, 8, 14, 23, tzinfo=UTC),
            model_id="sonnet",
            tokens_input=100,
            tokens_output=200,
        )
        s.add(e)
        s.commit()
        s.refresh(e)
        update_rollups_for_event(s, e)

    row = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "anthropic",
            UsagePeriodRollup.account_id == "user@x.com",
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.period_key == "2026-05-08",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert row.tokens_input == 200
    assert row.tokens_output == 400
    assert row.msgs == 2


def test_engine_enables_wal_and_sets_busy_timeout(tmp_path):
    """The "connect" event listener must enable WAL and busy_timeout on
    every connection, before any transaction begins.

    Regression test for a production incident: PRAGMA journal_mode=WAL was
    being run via SQLAlchemy's execute() path, which wrapped it in BEGIN
    IMMEDIATE — journal_mode silently no-ops inside a transaction, so WAL
    was never enabled. Every read transaction then serialised through the
    same lock as every write, and the dashboard timed out under fleet
    contention.
    """
    db_path = tmp_path / "pragma_check.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args=SQLITE_CONNECT_ARGS)
    configure_sqlite_engine(engine)

    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        cur.execute("PRAGMA busy_timeout")
        busy_ms = cur.fetchone()[0]
        cur.close()
    finally:
        raw.close()

    assert mode == "wal", f"expected WAL journal mode, got {mode!r}"
    assert busy_ms >= 30000, f"expected busy_timeout >= 30s, got {busy_ms}ms"


def test_concurrent_rollup_updates_do_not_lose_increments(tmp_path):
    """Many threads incrementing the same rollup grain must produce a sum
    equal to the event count — no lost updates, no SQLite-locked failures.

    Reproduces the unserialised read-modify-write race in production: two
    sidecars push events for the same (provider, account, period) at roughly
    the same moment; two transactions both SELECT, both mutate from the
    stale value, last writer overwrites the first. After the atomic-upsert
    fix every increment lands.

    Uses a file-backed DB + per-thread engines so each thread gets its own
    SQLite connection (StaticPool would serialize them through one). Also
    exercises busy_timeout: without it, concurrent writers fail with
    `database is locked` before the lost-update bug can even manifest.
    """
    import threading

    from sqlalchemy.pool import NullPool

    db_path = tmp_path / "race.db"
    url = f"sqlite:///{db_path}"

    setup_engine = create_engine(url, connect_args=SQLITE_CONNECT_ARGS)
    configure_sqlite_engine(setup_engine)
    SQLModel.metadata.create_all(setup_engine)

    threads = 4
    per_thread = 25
    errors: list[BaseException] = []
    barrier = threading.Barrier(threads)

    def worker(prefix: str) -> None:
        engine = create_engine(url, connect_args=SQLITE_CONNECT_ARGS, poolclass=NullPool)
        configure_sqlite_engine(engine)
        session = Session(engine)
        try:
            barrier.wait()  # synchronise the start so they actually race
            for i in range(per_thread):
                ev = _make_event(event_id=f"{prefix}_{i}")
                session.add(ev)
                session.flush()
                update_rollups_for_event(session, ev)
                session.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()
            engine.dispose()

    ts = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errors, f"workers raised: {errors}"

    # Verify from a fresh session.
    verify = Session(setup_engine)
    day = verify.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.period_key == "2026-05-08",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    expected = threads * per_thread
    assert day is not None, "day rollup row missing"
    assert day.msgs == expected, f"expected msgs={expected}, got {day.msgs}"
    assert day.tokens_input == expected * 100
    assert day.tokens_output == expected * 200
