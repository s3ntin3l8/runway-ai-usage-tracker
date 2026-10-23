"""Durable source lifecycle tests with synthetic encrypted credentials only."""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.encryption import EncryptionService
from app.models.db import SidecarSourceContribution, SidecarSourceState
from app.services import sidecar_devices, source_controls


@pytest.fixture
def session(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    encryption = EncryptionService(Fernet.generate_key().decode())
    monkeypatch.setattr(sidecar_devices, "encryption_service", encryption)
    monkeypatch.setattr(source_controls, "encryption_service", encryption)
    with Session(engine) as session:
        yield session
    engine.dispose()


def definition(*, read=True, generation=1):
    return {
        "source_id": "anthropic.claude-code",
        "provider_id": "anthropic",
        "label": "Claude Code",
        "revision": source_controls.catalog()["anthropic.claude-code"].revision,
        "generation": generation,
        "read": read,
        "state": "Limited",
        "renewal_requested": False,
        "renewal_supported": False,
        "new": False,
    }


def seed(
    session,
    *,
    generation=1,
    origin="keychain:Claude Code-credentials",
    account="synthetic@example.test",
):
    device, _ = sidecar_devices.enroll(session, "synthetic")
    source_controls.apply_report(session, device.device_id, definition(generation=generation))
    contribution = source_controls.save_contribution(
        session,
        device.device_id,
        definition(generation=generation),
        provider_id="anthropic",
        origin=origin,
        account_id=account,
        tokens={"oauth_token": "synthetic-access", "oauth_read_only": "1"},
        metadata={},
    )
    session.commit()
    return device, contribution


def test_off_withdraws_only_matching_contribution_and_reenable_needs_new_generation(session):
    first, contribution = seed(session)
    other, other_contribution = seed(session)
    source_controls.apply_report(session, first.device_id, definition(read=False, generation=2))
    session.commit()
    assert session.get(SidecarSourceContribution, contribution.id).payload_encrypted is None
    assert (
        session.get(SidecarSourceContribution, other_contribution.id).payload_encrypted is not None
    )
    assert not source_controls.allowed(session, first.device_id, "anthropic.claude-code", 1)
    source_controls.apply_report(session, first.device_id, definition(read=True, generation=1))
    assert not source_controls.allowed(session, first.device_id, "anthropic.claude-code", 1)
    source_controls.apply_report(session, first.device_id, definition(read=True, generation=3))
    assert source_controls.allowed(session, first.device_id, "anthropic.claude-code", 3)


def test_revocation_tombstone_rejects_late_contribution(session):
    device, contribution = seed(session)
    source_controls.queue_command(session, device.device_id, "anthropic.claude-code", "revoke")
    session.commit()
    assert session.get(SidecarSourceContribution, contribution.id).payload_encrypted is None
    with pytest.raises(ValueError, match="not authorized"):
        source_controls.save_contribution(
            session,
            device.device_id,
            definition(),
            provider_id="anthropic",
            origin="keychain:Claude Code-credentials",
            account_id="synthetic@example.test",
            tokens={"oauth_token": "late"},
            metadata={},
        )


def test_resume_cannot_grant_local_consent(session):
    device, _ = seed(session)
    source_controls.apply_report(session, device.device_id, definition(read=False, generation=2))
    with pytest.raises(ValueError, match="local consent"):
        source_controls.queue_command(session, device.device_id, "anthropic.claude-code", "resume")


def test_commands_are_durable_ordered_and_idempotent(session):
    device, _ = seed(session)
    first = source_controls.queue_command(
        session, device.device_id, "anthropic.claude-code", "pause"
    )
    second = source_controls.queue_command(
        session, device.device_id, "anthropic.claude-code", "request_setup"
    )
    session.commit()
    assert [row["id"] for row in source_controls.pending_commands(session, device.device_id)] == [
        first.id,
        second.id,
    ]
    source_controls.acknowledge(session, device.device_id, first.id, "applied")
    source_controls.acknowledge(session, device.device_id, first.id, "applied")
    assert [row["id"] for row in source_controls.pending_commands(session, device.device_id)] == [
        second.id
    ]
    other, _ = sidecar_devices.enroll(session, "other")
    with pytest.raises(ValueError, match="Unknown command"):
        source_controls.acknowledge(session, other.device_id, second.id, "applied")


def test_offline_snapshot_has_no_heartbeat_expiry_and_keeps_secrets_encrypted(session):
    device, contribution = seed(session)
    assert "synthetic-access" not in contribution.payload_encrypted
    device_id = device.device_id
    session.close()
    assert source_controls.snapshot(session)[1][0]["tokens"]["oauth_token"] == "synthetic-access"
    source_controls.apply_report(session, device_id, definition(read=False, generation=2))
    session.commit()
    assert source_controls.snapshot(session)[1] == []


def test_provider_mismatch_and_unknown_source_cannot_borrow_permission(session):
    device, _ = seed(session)
    with pytest.raises(ValueError, match="not authorized"):
        source_controls.save_contribution(
            session,
            device.device_id,
            definition(),
            provider_id="chatgpt",
            origin="keychain:Claude Code-credentials",
            account_id="synthetic@example.test",
            tokens={"oauth_token": "wrong-provider"},
            metadata={},
        )
    assert len(session.exec(select(SidecarSourceState)).all()) == 1


@pytest.mark.asyncio
async def test_cache_retains_offline_credentials_after_restart_and_observes_revocation(
    session, monkeypatch
):
    from app.services.token_cache import TokenCache

    device, _ = seed(session)
    monkeypatch.setattr(
        source_controls,
        "read_snapshot",
        lambda previous: (
            source_controls.snapshot(session)
            if source_controls.revision(session) != previous
            else None
        ),
    )
    first, restarted = TokenCache(ttl_seconds=0), TokenCache(ttl_seconds=0)
    assert (await first.get("anthropic"))["oauth_token"] == "synthetic-access"
    assert (await restarted.get("anthropic"))["oauth_token"] == "synthetic-access"
    source_controls.apply_report(session, device.device_id, definition(read=False, generation=2))
    session.commit()
    assert await first.get("anthropic") is None
    assert await restarted.get("anthropic") is None


@pytest.mark.asyncio
async def test_cache_withdrawal_preserves_identical_other_device_and_manual_fallback(
    session, monkeypatch
):
    from app.services.token_cache import TokenCache

    first, _ = seed(session)
    second, _ = seed(session)
    monkeypatch.setattr(
        source_controls,
        "read_snapshot",
        lambda previous: (
            source_controls.snapshot(session)
            if source_controls.revision(session) != previous
            else None
        ),
    )
    cache = TokenCache()
    await cache.store(
        "anthropic", {"api_key": "synthetic-manual"}, account_id="default", source="config"
    )
    candidates = await cache.get_source_candidates("anthropic", "synthetic@example.test")
    assert len(candidates) == 2
    source_controls.apply_report(session, first.device_id, definition(read=False, generation=2))
    session.commit()
    assert len(await cache.get_source_candidates("anthropic", "synthetic@example.test")) == 1
    source_controls.apply_report(session, second.device_id, definition(read=False, generation=2))
    session.commit()
    assert await cache.get_source_candidates("anthropic", "synthetic@example.test") == []
    assert await cache.get("anthropic") == {"api_key": "synthetic-manual"}


@pytest.mark.asyncio
async def test_cache_keeps_durable_bundles_separate_and_hides_pending_identity(
    session, monkeypatch
):
    from app.services.token_cache import TokenCache

    device, _ = seed(session)
    source_controls.save_contribution(
        session,
        device.device_id,
        None,
        provider_id="anthropic",
        origin="file:synthetic",
        account_id="pending-synthetic",
        tokens={"api_key": "synthetic-file"},
        metadata={"identity_pending": True},
    )
    session.commit()
    monkeypatch.setattr(
        source_controls,
        "read_snapshot",
        lambda previous: (
            source_controls.snapshot(session)
            if source_controls.revision(session) != previous
            else None
        ),
    )
    cache = TokenCache()
    assert await cache.get("anthropic", "synthetic@example.test") == {
        "oauth_token": "synthetic-access",
        "oauth_read_only": "1",
    }
    assert await cache.get("anthropic", "pending-synthetic") is None
    assert len(await cache.get_pending_sources("anthropic")) == 1
    assert [(p, a) for p, a, _ in await cache.get_all_active_accounts()] == [
        ("anthropic", "synthetic@example.test")
    ]


@pytest.mark.asyncio
async def test_identity_promotion_is_durable_and_never_leaks_into_legacy_cache(
    session, monkeypatch
):
    from app.core import db
    from app.services.token_cache import TokenCache

    device, _ = seed(session, account="pending-synthetic")
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(
        source_controls,
        "read_snapshot",
        lambda previous: (
            source_controls.snapshot(session)
            if source_controls.revision(session) != previous
            else None
        ),
    )
    cache = TokenCache()
    candidate = (await cache.get_source_candidates("anthropic", "pending-synthetic"))[0]
    assert await cache.move_source(
        "anthropic", "pending-synthetic", "synthetic@example.test", candidate["source_id"]
    )
    session.expire_all()
    assert source_controls.snapshot(session)[1][0]["account_id"] == "synthetic@example.test"
    assert cache._cache == {}
    source_controls.apply_report(session, device.device_id, definition(read=False, generation=2))
    session.commit()
    await cache.store(
        "anthropic",
        {"oauth_token": "late-callback"},
        "synthetic@example.test",
        source_id=candidate["source_id"],
    )
    assert await cache.get("anthropic") is None
    assert cache._cache == {}


def test_removing_device_rejects_future_auth_and_withdraws_all_its_contributions(session):
    device, _ = seed(session)
    other, _ = seed(session)
    source_controls.revoke_device(session, device.device_id)
    session.commit()
    assert sidecar_devices.authentication_key(session, device.device_id) is None
    assert [row["metadata"]["device_id"] for row in source_controls.snapshot(session)[1]] == [
        other.device_id
    ]


def test_stale_worker_cannot_overwrite_newer_consent_generation(session):
    device, _ = seed(session)
    device_id = device.device_id
    assert source_controls.state(session, device_id, "anthropic.claude-code").generation == 1
    session.commit()
    with Session(session.get_bind()) as other:
        source_controls.apply_report(other, device_id, definition(read=False, generation=2))
        other.commit()
    source_controls.apply_report(session, device_id, definition(read=True, generation=1))
    session.commit()
    row = source_controls.state(session, device_id, "anthropic.claude-code")
    assert row.generation == 2 and row.local_read is False
    assert row.revoked_through == 2


def test_stale_delivery_does_not_replace_a_fresher_bundle(session):
    import time

    device, _ = seed(session)
    common = {
        "provider_id": "anthropic",
        "origin": "keychain:Claude Code-credentials",
        "account_id": "synthetic@example.test",
        "metadata": {},
    }
    source_controls.save_contribution(
        session,
        device.device_id,
        definition(),
        tokens={"cli_access_token": "synthetic-fresh", "cli_expires_at": str(time.time() + 3600)},
        **common,
    )
    source_controls.save_contribution(
        session,
        device.device_id,
        definition(),
        tokens={"cli_access_token": "synthetic-stale", "cli_expires_at": str(time.time() - 60)},
        **common,
    )
    session.commit()
    assert (
        source_controls.snapshot(session)[1][0]["tokens"]["cli_access_token"] == "synthetic-fresh"
    )
