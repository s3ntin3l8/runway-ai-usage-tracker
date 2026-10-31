"""The cloud-runner collector is on by default and ``CLAUDE_CLOUD_COLLECTOR_ENABLED=false`` stops it."""

import httpx
import pytest

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
