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


def test_touch_source_preserves_preferences_and_config_source_is_first():
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
        config = touch_source(
            session,
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="config:openrouter:alice@example.com",
            source_type="config",
            source_label="Manual configuration",
        )
        session.commit()

        assert refreshed.enabled is False
        assert refreshed.priority == 6
        assert refreshed.health == "healthy"
        assert refreshed.health_detail is None
        assert refreshed.source_label == "OPENROUTER_API_KEY"
        assert config.priority == 0
        assert account_sources(session, "openrouter", "ALICE@example.com") == [
            config,
            refreshed,
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
                source_type="sidecar",
                source_label="auth.json",
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
        record_source_health(session, "openrouter", "alice@example.com", "unknown", "healthy")
