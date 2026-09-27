"""Unit tests for audit_log.record_as — the Request-less variant used by
background jobs (e.g. a Data Health fix) that know their own actor identity
directly instead of pulling it from request.state.
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import AuditLog
from app.services.audit_log import record_as


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_record_as_writes_a_row_with_explicit_attribution():
    session = _session()
    record_as(
        session,
        actor="data-health-job",
        actor_type="system",
        actor_meta={"job_id": "abc123"},
        source_ip=None,
        action="data_health.fix_applied",
        target_id="legacy_provider_ids/opencode-xai",
        payload={"retagged": 57},
    )
    row = session.exec(select(AuditLog)).one()
    assert row.actor == "data-health-job"
    assert row.actor_type == "system"
    assert row.actor_meta_json == '{"job_id":"abc123"}'
    assert row.source_ip is None
    assert row.action == "data_health.fix_applied"
    assert row.target_id == "legacy_provider_ids/opencode-xai"
    assert row.payload_json == '{"retagged":57}'


def test_record_as_defaults_optional_fields_to_none():
    session = _session()
    record_as(session, actor="scripts/recost_events.py", action="recost.applied", target_id=None)
    row = session.exec(select(AuditLog)).one()
    assert row.actor == "scripts/recost_events.py"
    assert row.actor_type is None
    assert row.actor_meta_json is None
    assert row.source_ip is None
    assert row.target_id is None
    assert row.payload_json is None


def test_record_as_swallows_write_failures():
    """A logging failure must never raise into the caller — mirrors
    record()'s own contract, exercised here via a session already closed
    to force the write to fail."""
    session = _session()
    session.close()
    record_as(session, actor="x", action="y", target_id=None)  # must not raise
