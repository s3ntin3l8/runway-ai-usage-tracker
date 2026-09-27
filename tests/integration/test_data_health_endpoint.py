"""Integration tests: `/api/v1/system/data-health/*`.

Two engines are in play for these routes and both must point at the same
test database: `Depends(get_session)` (request-scoped, overridden the usual
way) backs `preview` and the `apply` audit write, while the scan/apply work
itself opens its own session against `app.services.data_health.jobs.engine`
(see that module's docstring for why) — so this file also monkeypatches
that module attribute, not just the FastAPI dependency.

Scans and applies are fire-and-forget `asyncio.create_task`s. A bare
`TestClient(app)` (no `with`) opens and tears down a fresh event loop *per
request*, which cancels any task still in flight when the request that
started it returns — so a scan or apply started by one call could never be
observed completing by a later call, deterministically, not just under
load. These tests run as `async def` against `httpx.AsyncClient` +
`ASGITransport` instead, so every call in a test shares one event loop and
a started task is guaranteed to still be running (or already awaited via
`fresh_jobs.wait_for_scan`/`wait_for_job`) the next time it's checked — no
polling, no rate-limit budget to manage, no lifespan startup to mock.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session, SQLModel, create_engine

import app.api.endpoints.data_health as data_health_endpoint
from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine, get_session
from app.main import app
from app.services.data_health.jobs import DataHealthJobs
from tests.unit.data_health.conftest import make_config

# A separate module handle (rather than `import ... as jobs_module`
# alongside the `from ... import DataHealthJobs` above) — CodeQL flags
# importing the same module both ways.
jobs_module = importlib.import_module("app.services.data_health.jobs")


@pytest.fixture
def session(monkeypatch, tmp_path: Path):
    # File-backed, not `:memory:` — the apply job's background thread opens
    # its own `Session(engine)` concurrently with the test's own session;
    # WAL mode + the 30s busy_timeout (`configure_sqlite_engine`, same as
    # the production engine in app/core/db.py) gives that worker thread a
    # real independent connection instead of contending on one, matching
    # how production actually runs.
    db_path = tmp_path / "data_health_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args=SQLITE_CONNECT_ARGS)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(jobs_module, "engine", engine)
    fresh_jobs = DataHealthJobs()
    monkeypatch.setattr(jobs_module, "jobs", fresh_jobs)
    # The endpoint module bound `jobs` at import time via `from ... import
    # jobs` — that name must be patched too, not just the source module's.
    monkeypatch.setattr(data_health_endpoint, "jobs", fresh_jobs)

    fake_cache = AsyncMock()
    fake_cache.get_with_metadata.return_value = None
    with (
        patch("app.services.token_cache.token_cache", fake_cache),
        patch("app.services.collector_manager.manager") as fake_manager,
        Session(engine) as s,
    ):
        # config_default_keyed's fix runs a real async hook
        # (config_rekey._make_token_cache_move_hook) that touches the
        # process-wide token_cache singleton and syncs collectors — both
        # would otherwise do real work (the singleton's own lock, a real
        # per-provider network path). Same fakes test_maintenance_config_rekey.py
        # uses for the same hook.
        fake_manager._sync_collectors = AsyncMock()
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def jobs(session) -> DataHealthJobs:
    """The same `DataHealthJobs` instance the `session` fixture wired into
    both the jobs module and the endpoint module — exposed so tests can
    `await` a scan/apply directly instead of polling over HTTP."""
    return jobs_module.jobs


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_get_report_triggers_a_scan_on_first_call(session, jobs):
    async with _client() as client:
        response = await client.get("/api/v1/system/data-health/")

    assert response.status_code == 200
    body = response.json()
    assert body["scanning"] is True
    assert body["checks"] == []

    # Drain the scan this triggered so it doesn't outlive the test.
    if jobs.scanning:
        await jobs._scan_task


async def test_rescan_then_get_report_returns_findings(session, jobs):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )

    async with _client() as client:
        rescan_response = await client.post("/api/v1/system/data-health/rescan")
        assert rescan_response.status_code == 202

        await jobs.wait_for_scan()

        report = (await client.get("/api/v1/system/data-health/")).json()

    assert not report["scanning"]
    check = next(c for c in report["checks"] if c["check_id"] == "config_default_keyed")
    assert check["total_count"] == 1


async def test_preview_a_fixable_group(session):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )

    async with _client() as client:
        response = await client.post(
            "/api/v1/system/data-health/config_default_keyed/preview",
            json={"group_key": "minimax", "params": {}},
        )

    assert response.status_code == 200
    assert "alice@example.com" in response.json()["summary"]


async def test_preview_unknown_check_id_returns_404(session):
    async with _client() as client:
        response = await client.post(
            "/api/v1/system/data-health/not-a-check/preview",
            json={"group_key": "x", "params": {}},
        )

    assert response.status_code == 404


async def test_apply_without_confirm_is_rejected(session):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )

    async with _client() as client:
        response = await client.post(
            "/api/v1/system/data-health/config_default_keyed/apply",
            json={"group_key": "minimax", "params": {}, "confirm": False},
        )

    assert response.status_code == 400


async def test_apply_before_any_scan_returns_400(session):
    async with _client() as client:
        response = await client.post(
            "/api/v1/system/data-health/config_default_keyed/apply",
            json={"group_key": "minimax", "params": {}, "confirm": True},
        )

    assert response.status_code == 400


async def test_full_apply_flow_rescan_apply_job_status(session, jobs):
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )

    async with _client() as client:
        await client.post("/api/v1/system/data-health/rescan")
        await jobs.wait_for_scan()

        apply_response = await client.post(
            "/api/v1/system/data-health/config_default_keyed/apply",
            json={"group_key": "minimax", "params": {}, "confirm": True},
        )
        assert apply_response.status_code == 202
        job_id = apply_response.json()["job_id"]

        await jobs.wait_for_job(job_id)

        job = (await client.get(f"/api/v1/system/data-health/jobs/{job_id}")).json()

    assert job["status"] == "succeeded", job
    assert job["result"]["counts"]["credential_tags_moved"] == 0


async def test_apply_on_a_blocked_check_returns_409(session, jobs):
    """lone_default_events is blocked while config_default_keyed still has
    findings — the server enforces this, not just the UI."""
    make_config(
        session, provider_id="minimax", account_id="default", account_label="alice@example.com"
    )

    async with _client() as client:
        await client.post("/api/v1/system/data-health/rescan")
        await jobs.wait_for_scan()

        response = await client.post(
            "/api/v1/system/data-health/lone_default_events/apply",
            json={"group_key": "minimax", "params": {}, "confirm": True},
        )

    assert response.status_code == 409


async def test_get_unknown_job_returns_404(session):
    async with _client() as client:
        response = await client.get("/api/v1/system/data-health/jobs/not-a-real-job")

    assert response.status_code == 404
