"""The cloud-runner collector is on by default and ``CLAUDE_CLOUD_COLLECTOR_ENABLED=false`` stops it."""

import asyncio

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

import app.main as app_main
from app.core.config import Settings
from app.services import claude_cloud_collector as collector


def _settings(monkeypatch, value=None) -> Settings:
    monkeypatch.delenv("CLAUDE_CLOUD_COLLECTOR_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("CLAUDE_CLOUD_COLLECTOR_ENABLED", value)
    return Settings(_env_file=None)


def test_the_collector_is_enabled_by_default(monkeypatch):
    assert _settings(monkeypatch).CLAUDE_CLOUD_COLLECTOR_ENABLED is True


def test_the_flag_set_to_false_disables_it(monkeypatch):
    assert _settings(monkeypatch, "false").CLAUDE_CLOUD_COLLECTOR_ENABLED is False


def test_the_test_suite_does_not_run_the_collector_unless_a_test_asks(monkeypatch):
    # conftest turns the flag off so no test's app lifespan starts a polling loop.
    assert app_main.settings.CLAUDE_CLOUD_COLLECTOR_ENABLED is False


@pytest.mark.asyncio
async def test_a_cycle_without_a_login_makes_no_request():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the collector must not reach the provider without a login")

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        assert await collector.collect_all(client) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_the_lifespan_starts_the_loop_only_when_enabled(monkeypatch, enabled):
    import app.core.db as core_db
    import app.services.accumulator as accumulator

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(core_db, "engine", engine)
    monkeypatch.setattr(app_main, "init_db", lambda: None)
    monkeypatch.setattr(app_main, "_expire_pending_quota_previews", lambda: None)
    monkeypatch.setattr(accumulator, "evict_orphan_error_rows", lambda _session: None)

    async def collect_all() -> list[dict]:
        return []

    async def nothing() -> None:
        return None

    monkeypatch.setattr(app_main.manager, "collect_all", collect_all)
    monkeypatch.setattr(app_main.manager, "close", nothing)
    monkeypatch.setattr(app_main.poller, "start", lambda: None)
    monkeypatch.setattr(app_main.poller, "stop", nothing)
    monkeypatch.setattr(app_main.sidecar_version_checker, "start", lambda: None)
    monkeypatch.setattr(app_main.sidecar_version_checker, "stop", nothing)
    monkeypatch.setattr(app_main.token_auto_refresher, "stop", nothing)
    monkeypatch.setattr(app_main.settings, "TOKEN_AUTO_REFRESH_ENABLED", False)
    monkeypatch.setattr(app_main.settings, "CLAUDE_CLOUD_COLLECTOR_ENABLED", enabled)
    started: list[bool] = []
    monkeypatch.setattr(collector.ClaudeCloudCollector, "start", lambda self: started.append(True))

    async with app_main.lifespan(app_main.app):
        await asyncio.sleep(0)

    assert started == ([True] if enabled else [])
