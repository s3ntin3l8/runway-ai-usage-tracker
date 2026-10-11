"""Migration notices describe confirmed removals and share their transaction."""

import json

from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import CredentialSource, SystemConfig
from app.services.credential_file_migration import (
    dismiss_file_migration_notice,
    get_file_migration_notice,
)
from app.services.credential_sources import (
    prune_unmanaged_server_file_sources,
    register_server_source,
)


def _session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _legacy(session, provider="anthropic"):
    register_server_source(
        session, provider_id=provider, account_id="default", source_type="file", label="login.json"
    )
    session.commit()


def test_notice_only_after_confirmed_removal_and_persists_across_sessions():
    with _session() as session:
        assert get_file_migration_notice(session) is None
        assert prune_unmanaged_server_file_sources(session).removed_count == 0
        assert session.exec(select(SystemConfig)).first() is None
        _legacy(session)
        result = prune_unmanaged_server_file_sources(session)
        session.commit()
        assert result.provider_ids == ("anthropic",)
        with Session(session.get_bind()) as restarted:
            notice = get_file_migration_notice(restarted)
            assert notice is not None
            assert notice.removed_count == 1
            assert notice.providers[0].name == "Claude Pro"
            assert notice.providers[0].provider_id == "anthropic"
            timestamp = notice.recorded_at
            assert prune_unmanaged_server_file_sources(restarted).removed_count == 0
            restarted.commit()
            assert get_file_migration_notice(restarted).recorded_at == timestamp


def test_notice_and_removals_rollback_together():
    with _session() as session:
        _legacy(session)
        prune_unmanaged_server_file_sources(session)
        session.rollback()
        assert session.exec(select(CredentialSource)).first() is not None
        assert get_file_migration_notice(session) is None
        prune_unmanaged_server_file_sources(session)
        session.commit()
        assert get_file_migration_notice(session).removed_count == 1


def test_dismissal_is_installation_wide_and_survives_later_removals():
    with _session() as session:
        _legacy(session)
        prune_unmanaged_server_file_sources(session)
        session.commit()
        _legacy(session, "gemini")
        prune_unmanaged_server_file_sources(session)
        session.commit()
        notice = get_file_migration_notice(session)
        assert notice.removed_count == 2
        assert {p.provider_id for p in notice.providers} == {"gemini", "anthropic"}
        dismiss_file_migration_notice(session)
        session.commit()
        with Session(session.get_bind()) as restarted:
            assert get_file_migration_notice(restarted) is None
            dismiss_file_migration_notice(restarted)
            restarted.commit()
            before = json.loads(
                restarted.exec(select(SystemConfig)).first().credential_file_migration_json
            )
            _legacy(restarted, "github")
            prune_unmanaged_server_file_sources(restarted)
            restarted.commit()
            after = json.loads(
                restarted.exec(select(SystemConfig)).first().credential_file_migration_json
            )
            assert after["dismissed_at"] == before["dismissed_at"]
            assert after["removed_count"] == 3
            assert get_file_migration_notice(restarted) is None


def test_unknown_provider_and_corrupt_record_are_safe():
    with _session() as session:
        session.add(SystemConfig(credential_file_migration_json="broken"))
        session.commit()
        assert get_file_migration_notice(session) is None
        _legacy(session, "retired-provider")
        prune_unmanaged_server_file_sources(session)
        session.commit()
        assert get_file_migration_notice(session).providers[0].name == "retired-provider"
