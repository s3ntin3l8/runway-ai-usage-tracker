"""Integration tests for the admin audit log.

Closes audit findings S7 (admin mutations not persisted) and R10 (PII
redaction in error logs). Every successful state-changing admin call on
a sidecar must append a row to `audit_log` that records what happened,
who attempted it, and from where.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import AuditLog, SidecarRegistry


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
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _add_sidecar(session: Session, sidecar_id: str) -> None:
    session.add(
        SidecarRegistry(
            sidecar_id=sidecar_id,
            hostname=sidecar_id,
            last_seen=datetime.now(UTC),
            first_seen=datetime.now(UTC),
        )
    )
    session.commit()


def _audit_rows(session: Session) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).order_by(AuditLog.ts)).all())


def test_pause_writes_audit_row(client, session):
    _add_sidecar(session, "pause-host")
    r = client.post("/api/v1/fleet/sidecars/pause-host/pause")
    assert r.status_code == 200

    rows = _audit_rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "sidecar.pause"
    assert row.target_id == "pause-host"
    assert row.actor  # populated, exact value depends on auth context
    assert row.source_ip  # captured from the request


def test_resume_writes_audit_row(client, session):
    _add_sidecar(session, "resume-host")
    r = client.post("/api/v1/fleet/sidecars/resume-host/resume")
    assert r.status_code == 200

    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].action == "sidecar.resume"
    assert rows[0].target_id == "resume-host"


def test_delete_writes_audit_row(client, session):
    _add_sidecar(session, "del-host")
    r = client.delete("/api/v1/fleet/sidecars/del-host")
    assert r.status_code == 200

    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].action == "sidecar.delete"
    assert rows[0].target_id == "del-host"


def test_patch_writes_audit_row(client, session):
    _add_sidecar(session, "patch-host")
    r = client.patch(
        "/api/v1/fleet/sidecars/patch-host",
        json={"custom_name": "Workstation", "tags": ["primary"]},
    )
    assert r.status_code == 200

    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].action == "sidecar.update"
    assert rows[0].target_id == "patch-host"


def test_update_now_writes_audit_row(client, session):
    _add_sidecar(session, "upd-host")
    r = client.post("/api/v1/fleet/sidecars/upd-host/update")
    assert r.status_code == 200
    assert r.json()["status"] == "update_queued"

    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].action == "sidecar.update_now"
    assert rows[0].target_id == "upd-host"


def test_failed_mutation_does_not_write_audit_row(client, session):
    """A 404 (sidecar not found) must NOT leave a row — audit logs are for
    successful state changes, not failed attempts."""
    r = client.post("/api/v1/fleet/sidecars/ghost/pause")
    assert r.status_code == 404
    assert _audit_rows(session) == []


def test_audit_log_endpoint_returns_recent_entries(client, session):
    """GET /api/v1/system/audit-log returns rows in newest-first order."""
    _add_sidecar(session, "h-a")
    _add_sidecar(session, "h-b")
    client.post("/api/v1/fleet/sidecars/h-a/pause")
    client.post("/api/v1/fleet/sidecars/h-b/pause")

    r = client.get("/api/v1/system/audit-log")
    assert r.status_code == 200
    data = r.json()
    entries = data["entries"]
    assert len(entries) == 2
    # Newest first
    assert entries[0]["target_id"] == "h-b"
    assert entries[1]["target_id"] == "h-a"
    # Schema check
    for e in entries:
        assert {"id", "ts", "actor", "source_ip", "action", "target_id"} <= e.keys()
