"""Unit tests for app.services.stale_reason."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import CredentialSource, ProviderConfig, SidecarSourceContribution
from app.services.stale_reason import (
    STALE_REASON_CREDENTIAL_FAILING,
    STALE_REASON_NO_CREDENTIAL,
    apply_stale_reason,
    derive_stale_reason,
)
from app.services.token_cache import TokenCache

PROVIDER = "anthropic_console"
ACCOUNT = "org-123"


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_no_credential_when_enabled_config_has_no_sources_or_cache(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    session.commit()

    cache = TokenCache()
    assert (
        derive_stale_reason(session, PROVIDER, ACCOUNT, cache=cache) == STALE_REASON_NO_CREDENTIAL
    )


def test_only_null_payload_contribution_still_gives_no_credential(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    session.add(
        SidecarSourceContribution(
            device_id="mac-1",
            provider_id=PROVIDER,
            credential_source_id="src-1",
            origin="browser",
            account_id=ACCOUNT,
            payload_encrypted=None,
        )
    )
    session.commit()

    cache = TokenCache()
    assert (
        derive_stale_reason(session, PROVIDER, ACCOUNT, cache=cache) == STALE_REASON_NO_CREDENTIAL
    )


def test_non_null_payload_contribution_gives_no_reason(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    session.add(
        SidecarSourceContribution(
            device_id="mac-1",
            provider_id=PROVIDER,
            credential_source_id="src-1",
            origin="browser",
            account_id=ACCOUNT,
            payload_encrypted="enc-blob",
        )
    )
    session.commit()

    cache = TokenCache()
    assert derive_stale_reason(session, PROVIDER, ACCOUNT, cache=cache) is None


def test_config_with_api_key_or_cookie_gives_no_reason(session: Session):
    session.add(
        ProviderConfig(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            enabled=True,
            api_key_encrypted="enc-key",  # pragma: allowlist secret
        )
    )
    session.add(
        ProviderConfig(
            provider_id=PROVIDER,
            account_id="org-cookie",
            enabled=True,
            session_cookie_encrypted="enc-cookie",  # pragma: allowlist secret
        )
    )
    session.commit()

    cache = TokenCache()
    assert derive_stale_reason(session, PROVIDER, ACCOUNT, cache=cache) is None
    assert derive_stale_reason(session, PROVIDER, "org-cookie", cache=cache) is None


def test_token_cache_entry_gives_no_reason(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    session.commit()

    cache = TokenCache()
    cache.seed_sync(PROVIDER, ACCOUNT, {"session_key": "sk-test"})
    assert derive_stale_reason(session, PROVIDER, ACCOUNT, cache=cache) is None


def test_all_unhealthy_sources_gives_credential_failing(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    session.add(
        CredentialSource(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            source_id="src-1",
            source_type="sidecar",
            source_label="Chrome",
            health="auth_failed",
            last_attempt_at=datetime.now(UTC),
        )
    )
    session.add(
        CredentialSource(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            source_id="src-2",
            source_type="sidecar",
            source_label="Safari",
            health="unavailable",
            last_attempt_at=datetime.now(UTC),
        )
    )
    session.commit()

    cache = TokenCache()
    assert (
        derive_stale_reason(session, PROVIDER, ACCOUNT, cache=cache)
        == STALE_REASON_CREDENTIAL_FAILING
    )


def test_healthy_source_gives_no_reason(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    session.add(
        CredentialSource(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            source_id="src-1",
            source_type="sidecar",
            source_label="Chrome",
            health="auth_failed",
            last_attempt_at=datetime.now(UTC),
        )
    )
    session.add(
        CredentialSource(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            source_id="src-2",
            source_type="sidecar",
            source_label="Safari",
            health="healthy",
            last_attempt_at=datetime.now(UTC),
            last_success_at=datetime.now(UTC),
        )
    )
    session.commit()

    cache = TokenCache()
    assert derive_stale_reason(session, PROVIDER, ACCOUNT, cache=cache) is None


def test_disabled_or_archived_config_gives_no_reason(session: Session):
    session.add(
        ProviderConfig(provider_id=PROVIDER, account_id="archived-org", enabled=True, archived=True)
    )
    session.add(ProviderConfig(provider_id=PROVIDER, account_id="disabled-org", enabled=False))
    session.add(
        CredentialSource(
            provider_id=PROVIDER,
            account_id="archived-org",
            source_id="src-1",
            source_type="sidecar",
            source_label="Chrome",
            health="auth_failed",
            last_attempt_at=datetime.now(UTC),
        )
    )
    session.commit()

    cache = TokenCache()
    assert derive_stale_reason(session, PROVIDER, "archived-org", cache=cache) is None
    assert derive_stale_reason(session, PROVIDER, "disabled-org", cache=cache) is None


def test_disabled_source_ignored_for_no_credential(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    session.add(
        CredentialSource(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            source_id="src-disabled",
            source_type="sidecar",
            source_label="Chrome",
            enabled=False,
            health="auth_failed",
        )
    )
    session.commit()

    cache = TokenCache()
    assert (
        derive_stale_reason(session, PROVIDER, ACCOUNT, cache=cache) == STALE_REASON_NO_CREDENTIAL
    )


def test_apply_stale_reason_only_sets_on_stale_cards(session: Session):
    session.add(ProviderConfig(provider_id=PROVIDER, account_id=ACCOUNT, enabled=True))
    session.commit()

    cache = TokenCache()
    fresh_card = {
        "provider_id": PROVIDER,
        "account_id": ACCOUNT,
        "stale": False,
        "stale_reason": "stale-leftover",
    }
    apply_stale_reason(session, fresh_card, cache=cache)
    assert "stale_reason" not in fresh_card

    stale_card = {
        "provider_id": PROVIDER,
        "account_id": ACCOUNT,
        "stale": True,
    }
    apply_stale_reason(session, stale_card, cache=cache)
    assert stale_card["stale_reason"] == STALE_REASON_NO_CREDENTIAL
