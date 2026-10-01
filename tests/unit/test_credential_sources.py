from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import CredentialSource
from app.services.credential_sources import (
    account_sources,
    describe_origin,
    record_source_health,
    resolve_source_account,
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


def _mem_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_touch_source_without_metadata_preserves_expiry_and_token_types():
    """Ingest knows the secrets but not the health metadata the manifest reported;
    its refresh must not wipe the expiry/token types the manifest recorded."""
    expires = datetime(2030, 1, 1, tzinfo=UTC)
    common = {
        "provider_id": "gemini",
        "account_id": "alice@example.com",
        "source_id": "sidecar:host-a:oauth",
        "source_type": "file",
        "source_label": "oauth_creds.json",
        "sidecar_id": "host-a",
    }
    with _mem_session() as session:
        touch_source(session, **common, credential_expires_at=expires, token_types=["oauth_token"])
        row = touch_source(session, **common)  # ingest-style: no metadata
        assert row.token_types_json == '["oauth_token"]'
        assert row.credential_expires_at is not None
        assert row.credential_expires_at.replace(tzinfo=UTC) == expires


def test_touch_source_explicit_none_clears_expiry():
    """``None`` is a real value ("this credential has no expiry"), unlike omission."""
    common = {
        "provider_id": "gemini",
        "account_id": "alice@example.com",
        "source_id": "sidecar:host-a:oauth",
        "source_type": "file",
        "source_label": "oauth_creds.json",
        "sidecar_id": "host-a",
    }
    with _mem_session() as session:
        touch_source(
            session,
            **common,
            credential_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            token_types=["oauth_token"],
        )
        row = touch_source(session, **common, credential_expires_at=None, token_types=[])
        assert row.credential_expires_at is None
        assert row.token_types_json == "[]"


def test_resolve_source_account_prefers_real_identity_over_default():
    common = {
        "provider_id": "chatgpt",
        "source_id": "sidecar:host-a:auth",
        "source_type": "file",
        "source_label": "auth.json",
        "sidecar_id": "host-a",
    }
    with _mem_session() as session:
        assert resolve_source_account(session, "chatgpt", "sidecar:host-a:auth") is None
        touch_source(session, **common, account_id="default")
        assert resolve_source_account(session, "chatgpt", "sidecar:host-a:auth") == "default"
        touch_source(session, **common, account_id="alice@example.com")
        assert (
            resolve_source_account(session, "chatgpt", "sidecar:host-a:auth") == "alice@example.com"
        )
        assert resolve_source_account(session, "gemini", "sidecar:host-a:auth") is None
