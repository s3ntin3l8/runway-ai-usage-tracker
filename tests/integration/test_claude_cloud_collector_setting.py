"""The Claude cloud-runner collector switch: helper, app-config API and the live start/stop."""

import os
import tempfile

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.main import app
from app.services.claude_cloud_setting import claude_cloud_collector_enabled
from tests.fixtures.test_client import TestClient

URL = "/api/v1/system/app-config"
AUTH = {"X-Admin-Key": "cloud-switch-admin"}


class FakeCollector:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")


@pytest.fixture
def db_url():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    try:
        yield url
    finally:
        os.remove(path)


@pytest.fixture
def engine(db_url):
    eng = create_engine(db_url, connect_args={"check_same_thread": False})
    yield eng
    eng.dispose()


@pytest.fixture
def fake(monkeypatch, engine):
    limiter.reset()
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "cloud-switch-admin")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "")
    collector = FakeCollector()
    monkeypatch.setattr(app.state, "claude_cloud_collector", collector, raising=False)

    def override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield collector
    app.dependency_overrides.clear()
    limiter.reset()


@pytest.fixture
def client(fake):
    return TestClient(app, base_url="http://testserver")


@pytest.mark.parametrize("env", [True, False])
def test_helper_uses_env_when_nothing_stored(monkeypatch, engine, env):
    monkeypatch.setattr(settings, "CLAUDE_CLOUD_COLLECTOR_ENABLED", env)
    with Session(engine) as s:
        assert claude_cloud_collector_enabled(s) is env


@pytest.mark.parametrize("env", [True, False])
@pytest.mark.parametrize("stored", [True, False])
def test_helper_stored_value_beats_env(monkeypatch, engine, env, stored):
    from app.models.db import SystemConfig

    monkeypatch.setattr(settings, "CLAUDE_CLOUD_COLLECTOR_ENABLED", env)
    with Session(engine) as s:
        s.add(SystemConfig(claude_cloud_collector_enabled=stored))
        s.commit()
        assert claude_cloud_collector_enabled(s) is stored


def test_put_requires_admin_key(client, fake):
    r = client.put(URL, json={"claude_cloud_collector_enabled": False})
    assert r.status_code in (401, 403)
    assert fake.calls == []


def test_put_stores_value_and_get_returns_it(client, engine, monkeypatch):
    monkeypatch.setattr(settings, "CLAUDE_CLOUD_COLLECTOR_ENABLED", True)
    body = client.get(URL, headers=AUTH).json()
    assert body["claude_cloud_collector_enabled"] is None
    assert body["claude_cloud_collector_effective"] is True

    assert (
        client.put(URL, json={"claude_cloud_collector_enabled": False}, headers=AUTH).status_code
        == 200
    )
    body = client.get(URL, headers=AUTH).json()
    assert body["claude_cloud_collector_enabled"] is False
    assert body["claude_cloud_collector_effective"] is False
    # Survives a new session on the same database.
    with Session(engine) as s:
        assert claude_cloud_collector_enabled(s) is False


def test_put_starts_and_stops_the_live_collector(client, fake):
    assert (
        client.put(URL, json={"claude_cloud_collector_enabled": False}, headers=AUTH).status_code
        == 200
    )
    assert fake.calls == ["stop"]
    assert (
        client.put(URL, json={"claude_cloud_collector_enabled": True}, headers=AUTH).status_code
        == 200
    )
    assert fake.calls == ["stop", "start"]


def test_repeated_identical_puts_are_harmless(client, fake):
    for _ in range(2):
        assert (
            client.put(
                URL, json={"claude_cloud_collector_enabled": False}, headers=AUTH
            ).status_code
            == 200
        )
    assert client.get(URL, headers=AUTH).json()["claude_cloud_collector_enabled"] is False
    assert fake.calls == ["stop", "stop"]  # the real stop() is idempotent


def test_put_without_the_field_leaves_the_collector_alone(client, fake):
    assert client.put(URL, json={"sidecar_auto_update": True}, headers=AUTH).status_code == 200
    assert fake.calls == []


def test_put_without_a_collector_on_app_state_still_saves(client, monkeypatch):
    monkeypatch.delattr(app.state, "claude_cloud_collector")
    assert (
        client.put(URL, json={"claude_cloud_collector_enabled": False}, headers=AUTH).status_code
        == 200
    )
    assert client.get(URL, headers=AUTH).json()["claude_cloud_collector_enabled"] is False
