"""Server update-notification surface: /settings flags + /check-updates trigger.

The Settings → About card and the in-app update banner read `latest_version` /
`update_available` from /settings; the Fleet "Check for updates" button POSTs to
/check-updates to force an immediate GitHub release poll. Both reuse the shared
`sidecar_version_checker` cache (the repo's latest release tag doubles as the
server's, since release-please tags the whole repo).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app import __version__
from app.core.db import get_session
from app.main import app
from app.models.db import AuditLog
from app.services.sidecar_version_checker import sidecar_version_checker


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session):
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _audit_rows(session: Session) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).order_by(AuditLog.ts)).all())


def test_settings_flags_update_when_latest_is_ahead(client, monkeypatch):
    monkeypatch.setattr(sidecar_version_checker, "get_latest", lambda: "999.0.0")
    body = client.get("/api/v1/system/settings").json()
    assert body["latest_version"] == "999.0.0"
    assert body["update_available"] is True


def test_settings_no_update_when_latest_unknown(client, monkeypatch):
    # None == never polled / offline -> never flag an update.
    monkeypatch.setattr(sidecar_version_checker, "get_latest", lambda: None)
    body = client.get("/api/v1/system/settings").json()
    assert body["latest_version"] is None
    assert body["update_available"] is False


def test_settings_no_update_when_current(client, monkeypatch):
    monkeypatch.setattr(sidecar_version_checker, "get_latest", lambda: __version__)
    body = client.get("/api/v1/system/settings").json()
    assert body["update_available"] is False


def test_check_updates_refreshes_and_reports(client, session, monkeypatch):
    mock_check = AsyncMock(return_value="999.0.0")
    monkeypatch.setattr(sidecar_version_checker, "check_now", mock_check)

    r = client.post("/api/v1/system/check-updates")
    assert r.status_code == 200
    body = r.json()
    assert body["current_version"] == __version__
    assert body["latest_version"] == "999.0.0"
    assert body["update_available"] is True
    mock_check.assert_awaited_once()


def test_check_updates_writes_audit_row(client, session, monkeypatch):
    monkeypatch.setattr(sidecar_version_checker, "check_now", AsyncMock(return_value=__version__))

    r = client.post("/api/v1/system/check-updates")
    assert r.status_code == 200
    assert r.json()["update_available"] is False

    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].action == "system.check_updates"
    assert rows[0].target_id is None
