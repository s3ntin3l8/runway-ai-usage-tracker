from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import CredentialSource
from app.services.credential_sources import (
    account_sources,
    describe_origin,
    record_source_health,
    sidecar_source_id,
    touch_source,
)


def test_describe_origin_returns_safe_type_and_label():
    assert describe_origin("env:OPENROUTER_API_KEY") == ("env", "OPENROUTER_API_KEY")
    assert describe_origin("path:/home/alice/.config/auth.json#fingerprint") == (
        "file",
        "auth.json",
    )
    assert describe_origin("cookie:browser") == ("sidecar", "Browser cookie")
    assert describe_origin(None) == ("sidecar", "Sidecar credential")


def test_sidecar_source_id_is_host_scoped_and_stable():
    source_id = sidecar_source_id("host-a", "path:~/.config/auth.json")
    assert source_id == sidecar_source_id("host-a", "path:~/.config/auth.json")
    assert source_id != sidecar_source_id("host-b", "path:~/.config/auth.json")


def test_touch_source_refresh_preserves_preferences_and_health():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        sidecar = touch_source(
            session,
            provider_id="openrouter",
            account_id="Alice@example.com",
            source_id="host-a",
            source_type="file",
            source_label="auth.json",
            credential_origin="path:auth.json",
            sidecar_id="host-a",
        )
        session.commit()
        sidecar.enabled = False
        sidecar.priority = 5
        sidecar.health = "unavailable"
        sidecar.health_detail = "Missing from last scan"
        session.add(sidecar)
        session.commit()

        refreshed = touch_source(
            session,
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="host-a",
            source_type="env",
            source_label="OPENROUTER_API_KEY",
            credential_origin="env:OPENROUTER_API_KEY",
            sidecar_id="host-a",
        )
        session.commit()

        assert refreshed.enabled is False
        assert refreshed.priority == 5
        assert refreshed.health == "unavailable"
        assert refreshed.health_detail == "Missing from last scan"
        assert refreshed.source_label == "OPENROUTER_API_KEY"


def test_config_source_is_inserted_first_and_shifts_existing_priority():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        sidecar = touch_source(
            session,
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="host-a",
            source_type="file",
            source_label="auth.json",
        )
        sidecar.priority = 5
        session.add(sidecar)
        config = touch_source(
            session,
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="config:openrouter:alice@example.com",
            source_type="config",
            source_label="Manual configuration",
        )
        session.commit()
        assert config.priority == 0
        assert sidecar.priority == 6
        assert account_sources(session, "openrouter", "ALICE@example.com") == [
            config,
            sidecar,
        ]


def test_record_source_health_updates_only_matching_source():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            CredentialSource(
                provider_id="openrouter",
                account_id="alice@example.com",
                source_id="host-a",
                source_type="file",
                source_label="auth.json",
                sidecar_id="host-a",
                credential_origin="path:/auth.json",
                last_seen=datetime.now(UTC),
            )
        )
        session.commit()
        record_source_health(session, "openrouter", "ALICE@example.com", "host-a", "auth_failed")
        session.commit()
        row = session.get(CredentialSource, 1)
        assert row is not None
        assert row.health == "auth_failed"
        assert row.health_detail == "Authentication failed"
        record_source_health(session, "openrouter", "alice@example.com", "host-a", "unavailable")
        session.commit()
        session.refresh(row)
        assert row.health == "unavailable"
        assert row.health_detail == "Collection failed"
        record_source_health(session, "openrouter", "alice@example.com", "host-a", "degraded")
        session.commit()
        session.refresh(row)
        assert row.health == "degraded"
        assert row.health_detail == "Some requests were rejected; quota was collected"
        record_source_health(session, "openrouter", "alice@example.com", "unknown", "healthy")
