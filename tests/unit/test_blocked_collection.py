"""Which unmapped credentials are silently stopping collection (#493)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import CredentialSource, PendingCredentialTag
from app.services.blocked_collection import (
    SERVER_IDENTITY_PROVIDERS,
    TOKEN_WITHHELD,
    blocked_collection,
    withholds_token,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _pending(provider="deepseek", sidecar="alpha", origin="env:KEY", reason=None):
    return PendingCredentialTag(
        sidecar_id=sidecar, provider_id=provider, credential_origin=origin, reason=reason
    )


def _source(provider="deepseek", last_success_at=None, enabled=True, source_id="src:a"):
    return CredentialSource(
        provider_id=provider,
        account_id="alice@example.com",
        source_id=source_id,
        source_type="file",
        source_label="x",
        enabled=enabled,
        last_success_at=last_success_at,
    )


def test_sidecar_and_server_agree_on_which_providers_verify_by_source():
    from scripts.sidecar import _SERVER_IDENTITY_PROVIDERS

    assert SERVER_IDENTITY_PROVIDERS == _SERVER_IDENTITY_PROVIDERS


def test_withholds_token_trusts_the_reason_then_infers_for_older_sidecars():
    assert withholds_token(_pending(reason=TOKEN_WITHHELD)) is True
    assert withholds_token(_pending(provider="anthropic", reason=TOKEN_WITHHELD)) is True
    assert withholds_token(_pending(reason="other")) is False
    # An older sidecar sends no reason: a provider the server cannot verify always withholds,
    # one it can verify ships its token (pending verification, not blocked).
    assert withholds_token(_pending(provider="deepseek")) is True
    assert withholds_token(_pending(provider="anthropic")) is False


def test_blocked_only_when_the_provider_has_no_fresh_collection():
    with _session() as session:
        row = _pending()
        assert [b.provider_id for b in blocked_collection(session, [row], now=NOW)] == ["deepseek"]

        # Nothing ever succeeded, or it was a long time ago: still dark.
        session.add(_source(last_success_at=NOW - timedelta(hours=7)))
        session.commit()
        assert len(blocked_collection(session, [row], now=NOW)) == 1

        # Another source collected recently: the provider works, the origin is just redundant.
        session.add(_source(last_success_at=NOW - timedelta(hours=1), source_id="src:b"))
        session.commit()
        assert blocked_collection(session, [row], now=NOW) == []


def test_a_disabled_or_other_providers_success_does_not_vouch():
    with _session() as session:
        session.add(_source(last_success_at=NOW, enabled=False))
        session.add(_source(provider="openrouter", last_success_at=NOW, source_id="src:o"))
        session.commit()
        assert len(blocked_collection(session, [_pending()], now=NOW)) == 1


def test_origins_that_are_not_withheld_are_ignored_and_results_are_stable():
    with _session() as session:
        rows = [
            _pending(provider="anthropic"),  # verifier handles it
            _pending(provider="kimi_coding", sidecar="beta", origin="cookie:b"),
            _pending(provider="deepseek", sidecar="alpha", origin="env:KEY"),
        ]
        out = blocked_collection(session, rows, now=NOW)
        assert [(b.provider_id, b.sidecar_id) for b in out] == [
            ("deepseek", "alpha"),
            ("kimi_coding", "beta"),
        ]
