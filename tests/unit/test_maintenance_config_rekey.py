"""Tests for app/services/maintenance/config_rekey.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import CredentialTag, ProviderConfig, WebhookConfig
from app.services.maintenance.config_rekey import (
    RekeyCollisionError,
    apply_rekey_config,
    plan_rekey_config,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _config(session: Session, account_id: str, **overrides) -> ProviderConfig:
    base = {"provider_id": "minimax", "account_id": account_id, "account_label": account_id}
    base.update(overrides)
    row = ProviderConfig(**base)
    session.add(row)
    session.commit()
    return row


def test_plan_reports_no_collision_for_the_common_case():
    session = _session()
    _config(session, "default")

    plan = plan_rekey_config(
        session, provider_id="minimax", old_account_id="default", new_account_id="alice@example.com"
    )

    assert plan.provider_config_exists_at_target is False


def test_plan_is_read_only():
    session = _session()
    _config(session, "default")

    plan_rekey_config(
        session, provider_id="minimax", old_account_id="default", new_account_id="alice@example.com"
    )

    row = session.exec(select(ProviderConfig)).one()
    assert row.account_id == "default"


def test_apply_moves_the_config_row():
    session = _session()
    _config(session, "default", api_key_encrypted="cred-blob-abc123")  # pragma: allowlist secret

    result, hooks = apply_rekey_config(
        session, provider_id="minimax", old_account_id="default", new_account_id="alice@example.com"
    )

    assert result.provider_config_moved is True
    row = session.exec(select(ProviderConfig)).one()
    assert row.account_id == "alice@example.com"
    preserved = row.api_key_encrypted
    assert preserved == "cred-blob-abc123"  # pragma: allowlist secret
    assert len(hooks) == 1


def test_apply_raises_on_collision_by_default_and_writes_nothing():
    session = _session()
    _config(session, "default")
    _config(session, "alice@example.com")  # target already has its own config

    with pytest.raises(RekeyCollisionError):
        apply_rekey_config(
            session,
            provider_id="minimax",
            old_account_id="default",
            new_account_id="alice@example.com",
        )

    rows = {r.account_id for r in session.exec(select(ProviderConfig))}
    assert rows == {"default", "alice@example.com"}  # nothing moved


def test_apply_archives_source_on_collision_when_requested():
    session = _session()
    _config(session, "default")
    _config(session, "alice@example.com")

    result, _hooks = apply_rekey_config(
        session,
        provider_id="minimax",
        old_account_id="default",
        new_account_id="alice@example.com",
        on_collision="archive_default",
    )

    assert result.provider_config_archived_source is True
    assert result.provider_config_moved is False
    default_row = session.exec(
        select(ProviderConfig).where(ProviderConfig.account_id == "default")
    ).one()
    assert default_row.archived is True
    assert default_row.enabled is False


def test_apply_raises_when_no_config_exists_at_old_account_id():
    session = _session()
    with pytest.raises(ValueError, match="No provider_config"):
        apply_rekey_config(
            session,
            provider_id="minimax",
            old_account_id="default",
            new_account_id="alice@example.com",
        )


def test_apply_moves_credential_tags():
    session = _session()
    _config(session, "default")
    session.add(
        CredentialTag(
            provider_id="minimax",
            credential_origin="path:/home/u/.minimax/creds.json",
            account_id="default",
            set_by="operator",
        )
    )
    session.commit()

    result, _hooks = apply_rekey_config(
        session, provider_id="minimax", old_account_id="default", new_account_id="alice@example.com"
    )

    assert result.credential_tags_moved == 1
    tag = session.exec(select(CredentialTag)).one()
    assert tag.account_id == "alice@example.com"


def test_apply_moves_webhook_and_drops_a_duplicate():
    session = _session()
    _config(session, "default")
    session.add(
        WebhookConfig(
            provider_id="minimax",
            account_id="default",
            threshold_pct=90.0,
            url="https://discord.example/unique",
            channel="discord",
        )
    )
    session.add(
        WebhookConfig(
            provider_id="minimax",
            account_id="default",
            threshold_pct=80.0,
            url="https://discord.example/shared",
            channel="discord",
        )
    )
    session.add(
        WebhookConfig(
            provider_id="minimax",
            account_id="alice@example.com",
            threshold_pct=80.0,
            url="https://discord.example/shared",  # already alerts for the target
            channel="discord",
        )
    )
    session.commit()

    result, _hooks = apply_rekey_config(
        session, provider_id="minimax", old_account_id="default", new_account_id="alice@example.com"
    )

    assert result.webhook_configs_moved == 1
    assert result.webhook_configs_dropped_duplicate == 1
    rows = list(session.exec(select(WebhookConfig)))
    assert len(rows) == 2  # the moved unique one + the pre-existing target one
    assert all(r.account_id == "alice@example.com" for r in rows)


async def _run_hook(hook):
    await hook()


def test_the_returned_hook_moves_the_token_cache_entry():
    session = _session()
    _config(session, "default")

    _result, hooks = apply_rekey_config(
        session, provider_id="minimax", old_account_id="default", new_account_id="alice@example.com"
    )

    fake_cache = AsyncMock()
    fake_cache.get_with_metadata.return_value = (
        {"api_key": "x"},
        {"account_label": "alice@example.com"},
    )
    with (
        patch("app.services.token_cache.token_cache", fake_cache),
        patch("app.services.collector_manager.manager") as fake_manager,
    ):
        fake_manager._sync_collectors = AsyncMock()
        import asyncio

        asyncio.run(_run_hook(hooks[0]))

    fake_cache.store.assert_awaited_once()
    store_kwargs = fake_cache.store.call_args.kwargs
    assert store_kwargs["account_id"] == "alice@example.com"
    fake_cache.remove.assert_awaited_once_with("minimax", "default")
    fake_manager._sync_collectors.assert_awaited_once_with(force=True)


def test_archive_hook_removes_source_cache_without_overwriting_target():
    session = _session()
    _config(session, "default")
    _config(session, "alice@example.com")

    _result, hooks = apply_rekey_config(
        session,
        provider_id="minimax",
        old_account_id="default",
        new_account_id="alice@example.com",
        on_collision="archive_default",
    )
    fake_cache = AsyncMock()
    fake_cache.get_with_metadata.return_value = ({"api_key": "old"}, {})  # pragma: allowlist secret
    with (
        patch("app.services.token_cache.token_cache", fake_cache),
        patch("app.services.collector_manager.manager") as fake_manager,
    ):
        fake_manager._sync_collectors = AsyncMock()
        import asyncio

        asyncio.run(_run_hook(hooks[0]))

    fake_cache.store.assert_not_awaited()
    fake_cache.remove.assert_awaited_once_with("minimax", "default")


def test_the_returned_hook_tolerates_no_cached_tokens():
    session = _session()
    _config(session, "default")

    _result, hooks = apply_rekey_config(
        session, provider_id="minimax", old_account_id="default", new_account_id="alice@example.com"
    )

    fake_cache = AsyncMock()
    fake_cache.get_with_metadata.return_value = None
    with (
        patch("app.services.token_cache.token_cache", fake_cache),
        patch("app.services.collector_manager.manager") as fake_manager,
    ):
        fake_manager._sync_collectors = AsyncMock()
        import asyncio

        asyncio.run(_run_hook(hooks[0]))

    fake_cache.store.assert_not_awaited()
    fake_cache.remove.assert_not_awaited()
