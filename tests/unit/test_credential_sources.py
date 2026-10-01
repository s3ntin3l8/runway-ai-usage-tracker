from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import CredentialSource
from app.services.credential_sources import (
    account_sources,
    describe_origin,
    prune_server_sources,
    record_source_result,
    register_server_source,
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


def _source_row(**overrides) -> CredentialSource:
    return CredentialSource(
        **{
            "provider_id": "openrouter",
            "account_id": "alice@example.com",
            "source_id": "host-a",
            "source_type": "file",
            "source_label": "auth.json",
            **overrides,
        }
    )


def test_record_source_result_stamps_provenance_for_each_outcome():
    row = _source_row()

    record_source_result(row, "healthy")
    assert (row.health, row.health_detail, row.last_error) == ("healthy", None, None)
    assert row.last_attempt_at is not None and row.last_success_at is not None
    first_success = row.last_success_at

    # A failure records the attempt and error but keeps the last success.
    record_source_result(row, "auth_failed")
    assert row.health == "auth_failed"
    assert row.last_error == "Authentication failed"
    assert row.last_success_at == first_success

    record_source_result(row, "unavailable")
    assert (row.health_detail, row.last_error) == ("Collection failed", "Collection failed")

    # Degraded still produced quota data: it counts as a success, with a note.
    record_source_result(row, "degraded")
    assert row.last_success_at is not None and row.last_success_at >= first_success
    assert row.last_error == "Some requests were rejected; quota was collected"


def test_register_server_source_is_idempotent_and_has_no_origin():
    with _mem_session() as session:
        first = register_server_source(
            session,
            provider_id="github",
            account_id="default",
            source_type="env",
            label="GITHUB_TOKEN",
        )
        again = register_server_source(
            session,
            provider_id="github",
            account_id="default",
            source_type="env",
            label="GITHUB_TOKEN",
        )
        assert first.id == again.id
        assert first.source_id == "server:github:env:GITHUB_TOKEN"
        # Origins are what operator tags and sidecar moves match on; a server env
        # var is not a sidecar origin.
        assert first.credential_origin is None and first.sidecar_id is None


def test_register_server_source_follows_identity_resolved_later():
    """First seen before its identity resolved (``default``), then under the real
    account: the source must move, not duplicate."""
    with _mem_session() as session:
        for account in ("default", "s3ntin3l8"):
            register_server_source(
                session,
                provider_id="github",
                account_id=account,
                source_type="env",
                label="GITHUB_TOKEN",
            )
        rows = session.exec(select(CredentialSource)).all()
        assert [r.account_id for r in rows] == ["s3ntin3l8"]


def test_register_server_source_drops_placeholder_when_real_row_exists():
    with _mem_session() as session:
        register_server_source(
            session, provider_id="github", account_id="s3ntin3l8", source_type="env", label="T"
        )
        # Legacy placeholder left behind for the same source.
        session.add(
            _source_row(
                provider_id="github",
                account_id="default",
                source_id="server:github:env:T",
                source_type="env",
                source_label="T",
            )
        )
        session.commit()
        register_server_source(
            session, provider_id="github", account_id="s3ntin3l8", source_type="env", label="T"
        )
        rows = session.exec(select(CredentialSource)).all()
        assert [r.account_id for r in rows] == ["s3ntin3l8"]


def test_register_server_source_drops_the_row_left_under_a_previous_account():
    """A rotated token resolving to a different login must not leave a duplicate."""
    with _mem_session() as session:
        for account in ("alice", "bob"):
            register_server_source(
                session, provider_id="github", account_id=account, source_type="env", label="T"
            )
        rows = session.exec(select(CredentialSource)).all()
        assert [r.account_id for r in rows] == ["bob"]


def test_prune_server_sources_removes_only_vanished_server_rows():
    with _mem_session() as session:
        for label in ("KEEP", "GONE"):
            register_server_source(
                session, provider_id="github", account_id="a", source_type="env", label=label
            )
        register_server_source(
            session, provider_id="openrouter", account_id="a", source_type="env", label="GONE"
        )
        touch_source(
            session,
            provider_id="github",
            account_id="a",
            source_id="sidecar:x",
            source_type="file",
            source_label="auth.json",
            sidecar_id="host-a",
        )
        removed = prune_server_sources(session, "github", {"server:github:env:KEEP"})
        left = {(r.provider_id, r.source_id) for r in session.exec(select(CredentialSource)).all()}

    assert removed == 1
    # Other providers' server rows and machine-reported rows are never touched.
    assert left == {
        ("github", "server:github:env:KEEP"),
        ("openrouter", "server:openrouter:env:GONE"),
        ("github", "sidecar:x"),
    }


def test_default_keyed_registration_adopts_the_resolved_row_and_keeps_its_provenance():
    """An unresolved collection (identity not obtained this cycle) must not displace the
    row a resolved one registered — that lost last_success_at and flipped the row between
    accounts while identity resolution flapped."""
    with _mem_session() as session:
        resolved = register_server_source(
            session, provider_id="github", account_id="s3ntin3l8", source_type="env", label="T"
        )
        record_source_result(resolved, "healthy")
        session.commit()
        success = resolved.last_success_at
        assert success is not None

        again = register_server_source(
            session, provider_id="github", account_id="default", source_type="env", label="T"
        )
        session.commit()

        rows = session.exec(select(CredentialSource)).all()
        assert [(r.account_id, r.id) for r in rows] == [("s3ntin3l8", resolved.id)]
        assert again.id == resolved.id and again.last_success_at == success


def test_default_keyed_registration_stays_default_when_nothing_is_resolved_yet():
    with _mem_session() as session:
        row = register_server_source(
            session, provider_id="github", account_id="default", source_type="env", label="T"
        )
        assert row.account_id == "default"
