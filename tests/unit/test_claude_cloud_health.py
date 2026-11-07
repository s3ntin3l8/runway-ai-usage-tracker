"""The cloud-runner collector persists one health row per account and the status API reports it."""

import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.api.endpoints.system_health import router
from app.core.config import settings
from app.core.db import SQLITE_CONNECT_ARGS, _add_columns_if_missing, get_session
from app.models.db import ClaudeCloudCollectorState
from app.services import claude_cloud_collector as collector
from app.services import claude_cloud_probe as probe
from tests.fixtures.test_client import TestClient

ACCOUNT = "holder@example.com"
OTHER = "second@example.com"
SECRET = "cse_privatesessionid"  # pragma: allowlist secret


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(collector, "engine", engine)
    yield engine
    engine.dispose()


@pytest.fixture
def run(engine, monkeypatch):
    """Run ``collect_all`` for the given accounts, each returning the next scripted report."""
    outcomes: dict[str, list[collector.CycleReport]] = {}

    async def candidates(provider):
        return [
            {"account_slot": account, "source_id": f"src-{account}", "credential_origin": None}
            for account in outcomes
        ]

    async def collect_account(account, source_id, client, migration_pending=None):
        return outcomes[account].pop(0)

    monkeypatch.setattr(
        probe, "token_cache", SimpleNamespace(get_account_source_candidates=candidates)
    )
    monkeypatch.setattr(collector, "_migration_pending", lambda: False)
    monkeypatch.setattr(collector, "collect_account", collect_account)

    async def go(scripted):
        outcomes.clear()
        outcomes.update({account: [report] for account, report in scripted.items()})
        async with httpx.AsyncClient() as client:
            return await collector.collect_all(client)

    return go


def rep(outcome="ok", events=0, backlog=False):
    return collector.CycleReport(outcome=outcome, events=events, backlog=backlog)


def row(engine, account=ACCOUNT):
    with Session(engine) as session:
        return session.get(ClaudeCloudCollectorState, account)


async def cycle(run, report, account=ACCOUNT):
    return await run({account: report})


@pytest.mark.asyncio
async def test_an_ok_cycle_writes_one_row_and_counts_cycles(run, engine):
    await cycle(run, rep("ok", events=7))
    state = row(engine)
    assert (state.consecutive_failures, state.last_events, state.cycles) == (0, 7, 1)
    assert state.last_outcome == "ok" and state.last_cycle_at > 0
    await cycle(run, rep("ok", events=2))
    assert row(engine).cycles == 2
    with Session(engine) as session:
        assert len(session.exec(select(ClaudeCloudCollectorState)).all()) == 1


@pytest.mark.asyncio
async def test_failures_accumulate_and_an_ok_resets_them(run, engine):
    for _ in range(3):
        await cycle(run, rep("internal_error"))
    assert row(engine).consecutive_failures == 3
    await cycle(run, rep("ok"))
    assert row(engine).consecutive_failures == 0


@pytest.mark.asyncio
async def test_partial_sets_backlog_and_counts_as_success(run, engine):
    await cycle(run, rep("storage_error"))
    await cycle(run, rep("partial", backlog=True))
    state = row(engine)
    assert (state.consecutive_failures, state.backlog) == (0, 1)


@pytest.mark.asyncio
async def test_a_busy_cycle_only_stamps_the_time(run, engine):
    await cycle(run, rep("internal_error", events=4))
    before = row(engine)
    failures, cycles, stamp = before.consecutive_failures, before.cycles, before.last_cycle_at
    time.sleep(0.01)
    await cycle(run, rep("busy"))
    after = row(engine)
    assert (after.consecutive_failures, after.cycles, after.last_events) == (failures, cycles, 4)
    assert after.last_cycle_at > stamp


@pytest.mark.asyncio
async def test_a_failed_write_does_not_stop_the_other_accounts(run, engine, monkeypatch, caplog):
    real = collector._record_cycle

    def flaky(account, report):
        if account == ACCOUNT:
            raise OperationalError("stmt", {}, Exception("database is locked " + SECRET))
        real(account, report)

    monkeypatch.setattr(collector, "_record_cycle", flaky)
    reports = await run({ACCOUNT: rep(), OTHER: rep(events=3)})
    assert set(reports) == {ACCOUNT, OTHER}
    assert row(engine, ACCOUNT) is None and row(engine, OTHER).last_events == 3
    assert "OperationalError" in caplog.text and SECRET not in caplog.text


# -- API ----------------------------------------------------------------------------------------


@pytest.fixture
def api(engine, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin")
    monkeypatch.setattr(settings, "CLAUDE_CLOUD_COLLECTOR_ENABLED", True)
    monkeypatch.setattr(settings, "CLAUDE_CLOUD_COLLECTOR_INTERVAL_SECONDS", 1800)
    monkeypatch.setattr(
        "app.api.endpoints.system_health.manager",
        SimpleNamespace(_sync_collectors=_noop, get_collector_stats=lambda: {"collectors": []}),
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/system")
    with Session(engine) as session:
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield (
                lambda: client.get("/api/v1/system/status", headers={"X-Admin-Key": "admin"}),
                session,
            )


async def _noop():
    return None


def add(session, account=ACCOUNT, **fields):
    values = {"last_cycle_at": time.time(), "last_outcome": "ok", "cycles": 1} | fields
    session.add(ClaudeCloudCollectorState(account_id=account, **values))
    session.commit()


def test_three_failures_are_unhealthy(api):
    get, session = api
    add(session, last_outcome="internal_error", consecutive_failures=3)
    body = get().json()
    assert body["collectors"] == []
    assert body["claude_cloud"]["enabled"] is True
    [entry] = body["claude_cloud"]["accounts"]
    assert entry["healthy"] is False and entry["consecutive_failures"] == 3


def test_a_stale_ok_row_is_unhealthy_and_a_fresh_one_is_healthy(api):
    get, session = api
    add(session, ACCOUNT, last_cycle_at=time.time() - 3 * 1800 - 60)
    add(session, OTHER, last_events=5, backlog=1)
    by_id = {a["account_id"]: a for a in get().json()["claude_cloud"]["accounts"]}
    assert by_id[ACCOUNT]["healthy"] is False
    assert by_id[OTHER]["healthy"] is True and by_id[OTHER]["backlog"] is True


def test_no_rows_or_flag_off_gives_no_accounts(api, monkeypatch):
    get, session = api
    assert get().json()["claude_cloud"] == {"enabled": True, "accounts": []}
    add(session)
    monkeypatch.setattr(settings, "CLAUDE_CLOUD_COLLECTOR_ENABLED", False)
    assert get().json()["claude_cloud"] == {"enabled": False, "accounts": []}


def test_the_response_holds_no_session_detail(api):
    get, session = api
    add(session, last_outcome="internal_error", consecutive_failures=1)
    entry = get().json()["claude_cloud"]["accounts"][0]
    assert set(entry) == {
        "account_id",
        "last_outcome",
        "last_cycle_at",
        "last_events",
        "consecutive_failures",
        "backlog",
        "healthy",
    }


# -- migration ----------------------------------------------------------------------------------


def test_an_old_table_gains_the_columns_and_old_rows_default_to_zero():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text("ALTER TABLE claude_cloud_collector_state DROP COLUMN consecutive_failures")
        )
        conn.execute(text("ALTER TABLE claude_cloud_collector_state DROP COLUMN backlog"))
        conn.execute(
            text(
                "INSERT INTO claude_cloud_collector_state (account_id, cycles, last_cycle_at, last_outcome, last_events, last_regressions) VALUES ('old@example.com', 4, 1.0, 'ok', 2, 0)"
            )
        )
        conn.commit()
        _add_columns_if_missing(conn)
        _add_columns_if_missing(conn)  # idempotent
    with Session(engine) as session:
        state = session.get(ClaudeCloudCollectorState, "old@example.com")
        assert (state.cycles, state.consecutive_failures, state.backlog) == (4, 0, 0)
    engine.dispose()
