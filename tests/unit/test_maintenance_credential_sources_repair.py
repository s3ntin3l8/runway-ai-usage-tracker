"""Tests for app/services/maintenance/credential_sources_repair.py."""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import (
    CredentialSource,
    CredentialTag,
    LatestUsage,
    ProviderAccountLabel,
    ProviderConfig,
)
from app.services.maintenance.credential_sources_repair import (
    apply_orphan_sources,
    find_orphan_sources,
    plan_orphan_sources,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _source(
    session: Session,
    *,
    account_id: str,
    source_id: str,
    provider_id: str = "deepseek",
    source_type: str = "sidecar",
    source_label: str = "auth.json",
    sidecar_id: str | None = "host-a",
) -> CredentialSource:
    row = CredentialSource(
        provider_id=provider_id,
        account_id=account_id,
        source_id=source_id,
        source_type=source_type,
        source_label=source_label,
        sidecar_id=sidecar_id,
    )
    session.add(row)
    session.commit()
    return row


def _config(session: Session, account_id: str, *, provider_id: str = "deepseek") -> ProviderConfig:
    row = ProviderConfig(provider_id=provider_id, account_id=account_id)
    session.add(row)
    session.commit()
    return row


def _card(session: Session, account_id: str, *, provider_id: str = "deepseek") -> None:
    session.add(
        LatestUsage(
            provider_id=provider_id,
            account_id=account_id,
            window_type="daily",
            variant="",
            model_id="",
            card_json="{}",
        )
    )
    session.commit()


def test_config_row_without_its_config_is_an_orphan_and_is_deleted():
    session = _session()
    _source(
        session,
        account_id="bd6d58cf",
        source_id="config:deepseek:bd6d58cf",
        source_type="config",
        source_label="Manual configuration",
        sidecar_id=None,
    )

    plan = plan_orphan_sources(session, "deepseek")
    assert plan.total == 1
    assert plan.config_ghosts == 1
    assert plan.orphans[0].kind == "config_ghost"
    assert plan.orphans[0].label == "Manual configuration · bd6d58cf"
    assert plan.counts == {"config_ghosts": 1, "duplicates": 0, "sources_deleted": 1}

    result = apply_orphan_sources(session, "deepseek")
    assert result.total == 1
    assert session.exec(select(CredentialSource)).all() == []
    assert apply_orphan_sources(session, "deepseek").total == 0  # idempotent


def test_config_row_with_its_config_is_never_touched():
    session = _session()
    _config(session, "alice@example.com")
    _source(
        session,
        account_id="alice@example.com",
        source_id="config:deepseek:alice@example.com",
        source_type="config",
        source_label="Manual configuration",
        sidecar_id=None,
    )

    assert find_orphan_sources(session, "deepseek") == []
    assert session.exec(select(CredentialSource)).one().account_id == "alice@example.com"


def test_duplicate_is_deleted_only_under_the_account_without_evidence():
    """The shape that made Settings list the provider twice: one source id
    under the old hash-shaped account and under the real one."""
    session = _session()
    _config(session, "alice@example.com")
    _source(session, account_id="bd6d58cf", source_id="sidecar:abc")
    _source(session, account_id="alice@example.com", source_id="sidecar:abc")

    plan = plan_orphan_sources(session, "deepseek")
    assert plan.total == 1
    assert plan.duplicates == 1
    assert plan.orphans[0].account_id == "bd6d58cf"

    apply_orphan_sources(session, "deepseek")
    remaining = session.exec(select(CredentialSource)).all()
    assert [(r.account_id, r.source_id) for r in remaining] == [
        ("alice@example.com", "sidecar:abc")
    ]


def test_a_second_real_account_keeps_its_copy():
    """Two accounts an operator really used is their call, not a repair's."""
    session = _session()
    _config(session, "alice@example.com")
    _config(session, "bob@example.com")
    _source(session, account_id="alice@example.com", source_id="sidecar:abc")
    _source(session, account_id="bob@example.com", source_id="sidecar:abc")

    assert find_orphan_sources(session, "deepseek") == []


def test_a_group_where_no_copy_has_evidence_is_left_alone():
    """No config, card or events on either side: nothing says which copy is
    live, so neither is safe to call stranded."""
    session = _session()
    _source(session, account_id="bd6d58cf", source_id="sidecar:abc")
    _source(session, account_id="1692b86a", source_id="sidecar:abc")

    assert find_orphan_sources(session, "deepseek") == []


def test_a_card_is_enough_evidence_for_the_account():
    session = _session()
    _card(session, "alice@example.com")
    _source(session, account_id="bd6d58cf", source_id="sidecar:abc")
    _source(session, account_id="alice@example.com", source_id="sidecar:abc")

    plan = plan_orphan_sources(session, "deepseek")
    assert [o.account_id for o in plan.orphans] == ["bd6d58cf"]


def test_a_tag_is_enough_evidence_for_the_account():
    """A tag on a discovered account (config not created yet) is operator intent,
    not a stranded copy."""
    session = _session()
    _config(session, "alice@example.com")
    session.add(
        CredentialTag(
            provider_id="deepseek",
            credential_origin="path:/auth.json",
            account_id="bd6d58cf",
        )
    )
    _source(session, account_id="bd6d58cf", source_id="sidecar:abc")
    _source(session, account_id="alice@example.com", source_id="sidecar:abc")

    assert find_orphan_sources(session, "deepseek") == []


def test_an_account_label_is_enough_evidence_for_the_account():
    session = _session()
    _config(session, "alice@example.com")
    session.add(
        ProviderAccountLabel(
            provider_id="deepseek",
            account_id="bd6d58cf",
            account_label="Renamed by hand",
        )
    )
    _source(session, account_id="bd6d58cf", source_id="sidecar:abc")
    _source(session, account_id="alice@example.com", source_id="sidecar:abc")

    assert find_orphan_sources(session, "deepseek") == []


def test_a_single_row_on_an_account_without_evidence_is_not_reported():
    """A credential tagged to a new account, before its first collection, is
    not a leftover — only a claim contradicted by another copy is."""
    session = _session()
    _source(session, account_id="bob@example.com", source_id="sidecar:abc")

    assert find_orphan_sources(session, "deepseek") == []


def test_other_providers_rows_are_untouched():
    session = _session()
    _source(
        session,
        account_id="bd6d58cf",
        source_id="config:deepseek:bd6d58cf",
        provider_id="deepseek",
        source_type="config",
        sidecar_id=None,
    )
    _source(
        session,
        account_id="1692b86a",
        source_id="config:openrouter:1692b86a",
        provider_id="openrouter",
        source_type="config",
        sidecar_id=None,
    )

    apply_orphan_sources(session, "deepseek")

    remaining = session.exec(select(CredentialSource)).all()
    assert [(r.provider_id, r.source_id) for r in remaining] == [
        ("openrouter", "config:openrouter:1692b86a")
    ]


def test_plan_is_read_only():
    session = _session()
    _source(
        session,
        account_id="bd6d58cf",
        source_id="config:deepseek:bd6d58cf",
        source_type="config",
        sidecar_id=None,
    )

    plan_orphan_sources(session, "deepseek")

    assert len(session.exec(select(CredentialSource)).all()) == 1
