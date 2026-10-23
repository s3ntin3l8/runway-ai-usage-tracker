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


def test_old_generation_updates_contact_time_without_restoring_consent(session):
    from datetime import UTC, datetime

    device, _ = seed(session, generation=3)
    source_controls.apply_report(session, device.device_id, definition(read=False, generation=4))
    row = source_controls.state(session, device.device_id, "anthropic.claude-code")
    row.last_report_at = datetime(2000, 1, 1, tzinfo=UTC)
    session.add(row)
    session.commit()
    row = source_controls.apply_report(session, device.device_id, definition(generation=1))
    assert row.last_report_at.year > 2000
    assert row.generation == 4 and not row.local_read


def test_write_lock_reserves_writer_on_configured_engine(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.core.db import configure_sqlite_engine

    engine = create_engine(
        "sqlite:///" + str(tmp_path / "locking.db"), connect_args={"timeout": 0.05}
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as first, Session(engine) as second:
            source_controls._write_lock(first)
            with pytest.raises(OperationalError, match="locked"):
                second.execute(
                    text("UPDATE sidecar_source_revision SET revision = revision WHERE id = 1")
                )
            second.rollback()
            first.commit()
            second.execute(
                text("UPDATE sidecar_source_revision SET revision = revision WHERE id = 1")
            )
    finally:
        engine.dispose()


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
        lambda previous, *_: (
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
        lambda previous, *_: (
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
        lambda previous, *_: (
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
        lambda previous, *_: (
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


def test_browser_origin_cannot_hide_native_dependency_by_changing_provider(session):
    device, _ = seed(session)
    with pytest.raises(ValueError, match="not authorized"):
        source_controls.contribution_proof(
            session, device.device_id, "github", "cookie:anthropic/Chrome/Default/session", None
        )
    assert (
        source_controls.source_for_origin("anthropic", "cookie:anthropic/Safari/Default/session")
        is None
    )
    assert (
        source_controls.source_for_origin("anthropic", "keychain:Claude Code-credentials-unrelated")
        == "anthropic.claude-code"
    )
    assert (
        source_controls.source_for_origin("anthropic", "keychain:Claude Code-credentialsEVIL")
        == "unknown"
    )


def test_pause_hides_but_preserves_the_known_bundle_until_authorized_resume(session):
    device, contribution = seed(session)
    paused = source_controls.queue_command(
        session, device.device_id, "anthropic.claude-code", "pause"
    )
    source_controls.acknowledge(session, device.device_id, paused.id, "applied")
    session.commit()
    assert source_controls.snapshot(session)[1] == []
    assert session.get(SidecarSourceContribution, contribution.id).payload_encrypted is not None
    resumed = source_controls.queue_command(
        session, device.device_id, "anthropic.claude-code", "resume"
    )
    session.commit()
    assert source_controls.snapshot(session)[1] == []
    source_controls.acknowledge(session, device.device_id, resumed.id, "applied")
    session.commit()
    assert source_controls.snapshot(session)[1][0]["tokens"]["oauth_token"] == "synthetic-access"


def test_retiring_an_origin_erases_its_durable_bundle_without_touching_other_sources(session):
    from app.models.db import CredentialSource

    device, contribution = seed(session)
    other, _ = seed(session)
    metadata = CredentialSource(
        provider_id="anthropic",
        account_id=contribution.account_id,
        source_id=contribution.credential_source_id,
        source_type="keychain",
        source_label="Claude Code",
        sidecar_id=sidecar_devices.registry_id(device.device_id),
    )
    session.add(metadata)
    session.commit()
    before = source_controls.revision(session)
    session.delete(metadata)
    session.commit()
    assert session.get(SidecarSourceContribution, contribution.id).payload_encrypted is None
    assert source_controls.revision(session) > before
    assert [row["metadata"]["device_id"] for row in source_controls.snapshot(session)[1]] == [
        other.device_id
    ]


def test_account_row_reassignment_keeps_the_surviving_origin_bundle(session):
    from app.models.db import CredentialSource

    device, contribution = seed(session)
    common = {
        "provider_id": "anthropic",
        "source_id": contribution.credential_source_id,
        "source_type": "keychain",
        "source_label": "Claude Code",
    }
    old = CredentialSource(account_id=contribution.account_id, **common)
    surviving = CredentialSource(account_id="reassigned@example.test", **common)
    session.add(old)
    session.add(surviving)
    session.commit()
    session.delete(old)
    session.commit()
    assert session.get(SidecarSourceContribution, contribution.id).payload_encrypted is not None


@pytest.mark.asyncio
async def test_retiring_durable_copy_preserves_identical_manual_oauth(session, monkeypatch):
    from app.services.token_cache import TokenCache

    device, contribution = seed(session)
    monkeypatch.setattr(
        source_controls,
        "read_snapshot",
        lambda previous, *_: (
            source_controls.snapshot(session)
            if source_controls.revision(session) != previous
            else None
        ),
    )
    cache = TokenCache()
    await cache.store(
        "anthropic",
        {"oauth_token": "synthetic-access"},
        account_id=contribution.account_id,
        source="config",
    )
    assert len(await cache.get_source_candidates("anthropic", contribution.account_id)) == 1
    await cache.remove_source(
        "anthropic",
        contribution.account_id,
        contribution.credential_source_id,
        retire_matching_oauth=True,
    )
    assert await cache.get("anthropic", contribution.account_id) == {
        "oauth_token": "synthetic-access"
    }


def test_acknowledged_commands_are_pruned_without_losing_latest_ordering(session):
    from datetime import UTC, datetime

    from app.models.db import SidecarSourceCommand

    device, _ = seed(session)
    first = source_controls.queue_command(
        session, device.device_id, "anthropic.claude-code", "pause"
    )
    first_id = first.id
    first.issued_at = datetime(2000, 1, 1, tzinfo=UTC)
    source_controls.acknowledge(session, device.device_id, first.id, "applied")
    second = source_controls.queue_command(
        session, device.device_id, "anthropic.claude-code", "revoke"
    )
    source_controls.acknowledge(session, device.device_id, second.id, "applied")
    session.commit()
    source_controls.prune_commands(session, device.device_id)
    session.commit()
    assert session.get(SidecarSourceCommand, first_id) is None
    assert session.get(SidecarSourceCommand, second.id) is not None
    # A crashed client may repeat an already processed ack after retention.
    source_controls.acknowledge(session, device.device_id, first_id, "applied")
    row = source_controls.state(session, device.device_id, "anthropic.claude-code")
    assert row.remote_paused and row.revoked_through == 1
    newer = source_controls.queue_command(
        session, device.device_id, "anthropic.claude-code", "request_setup"
    )
    assert newer.id > second.id


def test_snapshot_decrypts_only_changed_bundles_and_drops_revoked_cache(session, monkeypatch):
    device, _ = seed(session)
    other, _ = seed(session)
    cache = {}
    calls = []
    decrypt = source_controls.encryption_service.decrypt_json

    def count(value):
        calls.append(value)
        return decrypt(value)

    monkeypatch.setattr(source_controls.encryption_service, "decrypt_json", count)
    assert len(source_controls.snapshot(session, cache)[1]) == 2
    assert len(calls) == 2
    source_controls.queue_command(session, device.device_id, "anthropic.claude-code", "pause")
    session.commit()
    assert len(source_controls.snapshot(session, cache)[1]) == 1
    assert len(calls) == 2  # The unrelated device's ciphertext did not change.
    assert len(cache) == 1
    source_controls.queue_command(session, other.device_id, "anthropic.claude-code", "revoke")
    session.commit()
    assert source_controls.snapshot(session, cache)[1] == []
    assert cache == {}


def test_stale_wal_reader_cannot_restore_a_concurrently_revoked_bundle(tmp_path, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.core.db import configure_sqlite_engine

    engine = create_engine("sqlite:///" + str(tmp_path / "race.db"), connect_args={"timeout": 0.05})
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    encryption = EncryptionService(Fernet.generate_key().decode())
    monkeypatch.setattr(sidecar_devices, "encryption_service", encryption)
    monkeypatch.setattr(source_controls, "encryption_service", encryption)
    try:
        with Session(engine) as writer, Session(engine) as stale:
            device, _ = seed(writer)
            device_id = device.device_id
            writer.commit()
            stale.execute(text("SELECT revision FROM sidecar_source_revision")).all()
            source_controls.queue_command(writer, device_id, "anthropic.claude-code", "revoke")
            writer.commit()
            with pytest.raises(OperationalError, match="locked"):
                source_controls.save_contribution(
                    stale,
                    device_id,
                    definition(),
                    provider_id="anthropic",
                    origin="keychain:Claude Code-credentials",
                    account_id="synthetic@example.test",
                    tokens={"oauth_token": "late"},
                    metadata={},
                )
            stale.rollback()
            assert source_controls.snapshot(stale)[1] == []
    finally:
        engine.dispose()
