"""Integration tests for the admin audit log.

Closes audit findings S7 (admin mutations not persisted) and R10 (PII
redaction in error logs). Every successful state-changing admin call on
a sidecar must append a row to `audit_log` that records what happened,
who attempted it, and from where.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import AuditLog, SidecarRegistry
from tests.fixtures.test_client import TestClient


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


def test_keep_alive_change_writes_audit_row(client, session):
    _add_sidecar(session, "ka-host")
    r = client.put("/api/v1/fleet/sidecars/ka-host/keep-alive", json={"enabled": True})
    assert r.status_code == 200

    (row,) = _audit_rows(session)
    assert row.action == "sidecar.keep_alive"
    assert row.target_id == "ka-host"


def test_adopt_dry_run_writes_nothing_and_apply_is_audited(client, session):
    from datetime import timedelta

    device = "device-" + "b" * 32
    now = datetime.now(UTC)
    session.add(
        SidecarRegistry(
            sidecar_id="adopt-host",
            hostname="adopt-host",
            os_platform="Windows/11",
            first_seen=now - timedelta(days=9),
            last_seen=now - timedelta(minutes=5),
        )
    )
    session.add(
        SidecarRegistry(
            sidecar_id=device,
            hostname="adopt-host",
            os_platform="Windows/11",
            first_seen=now - timedelta(minutes=4),
            last_seen=now,
        )
    )
    session.commit()

    listed = client.get("/api/v1/fleet/sidecars").json()["sidecars"]
    by_id = {s["sidecar_id"]: s for s in listed}
    assert [c["sidecar_id"] for c in by_id[device]["adoptable_legacy"]] == ["adopt-host"]
    assert by_id["adopt-host"]["adoptable_legacy"] == []

    preview = client.post(
        f"/api/v1/fleet/sidecars/{device}/adopt", json={"legacy_sidecar_id": "adopt-host"}
    )
    assert preview.status_code == 200 and preview.json()["dry_run"] is True
    assert session.get(SidecarRegistry, "adopt-host") is not None
    assert session.exec(select(AuditLog).where(AuditLog.action == "sidecar.adopt")).first() is None

    applied = client.post(
        f"/api/v1/fleet/sidecars/{device}/adopt",
        json={"legacy_sidecar_id": "adopt-host", "dry_run": False},
    )
    assert applied.status_code == 200 and applied.json()["dry_run"] is False
    session.expire_all()
    assert session.get(SidecarRegistry, "adopt-host") is None
    row = session.exec(select(AuditLog).where(AuditLog.action == "sidecar.adopt")).one()
    assert row.target_id == device

    refused = client.post(
        f"/api/v1/fleet/sidecars/{device}/adopt",
        json={"legacy_sidecar_id": "nobody", "dry_run": False},
    )
    assert refused.status_code == 409
