"""Shared helpers and fixtures for multi-account provider config integration tests."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app
from tests.fixtures.test_client import TestClient

CONSOLE_ORG_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    os.close(fd)
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()
        os.remove(db_path)


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    # list_provider_configs unions token_cache into `accounts` — clear the
    # in-memory cache so empty-state assertions aren't polluted by prior tests.
    from app.services.token_cache import token_cache

    token_cache._cache.clear()
    token_cache._source_cache.clear()
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()
    token_cache._cache.clear()
    token_cache._source_cache.clear()


def _admin_headers() -> dict[str, str]:
    """Set the X-Admin-Key the PUT endpoint requires."""
    from app.core.config import settings

    if settings.ADMIN_API_KEY:
        return {"X-Admin-Key": settings.ADMIN_API_KEY}
    return {}


def _seed_latest_usage(session: Session, provider_id: str, account_id: str) -> None:
    """Insert a synthetic `latest_usage` row so the GET /provider-configs
    endpoint sees the (provider_id, account_id) pair as having live data."""
    from datetime import UTC, datetime

    from app.models.db import LatestUsage

    row = LatestUsage(
        provider_id=provider_id,
        account_id=account_id,
        window_type="weekly",
        variant="default",
        model_id="claude-opus-4-7",
        card_json="{}",
        updated_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()


def _mock_console_access(monkeypatch):
    from app.api.endpoints import system_provider_config as system

    orgs = [{"id": CONSOLE_ORG_ID, "name": "API organization"}]
    monkeypatch.setattr(system, "console_organizations", AsyncMock(return_value=orgs))
    monkeypatch.setattr(
        system, "console_get", AsyncMock(return_value={"amount": 10000, "currency": "USD"})
    )
