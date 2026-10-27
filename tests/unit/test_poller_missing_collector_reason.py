"""Why a server contribution goes stale when its collector has no outcome (#1027)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import CredentialSource, SidecarRegistry
from app.services.poller import _missing_collector_reason

NOW = datetime.now(UTC)


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _source(session: Session, sidecar_id: str | None, age: timedelta) -> None:
    session.add(
        CredentialSource(
            provider_id="anthropic_console",
            account_id="org",
            source_id=f"sidecar:{sidecar_id}",
            source_type="cookie" if sidecar_id else "config",
            source_label="Chrome cookie",
            sidecar_id=sidecar_id,
            last_seen=NOW - age,
        )
    )
    session.commit()


def _host(session: Session, sidecar_id: str, os_platform: str) -> None:
    session.add(
        SidecarRegistry(sidecar_id=sidecar_id, hostname=sidecar_id, os_platform=os_platform)
    )
    session.commit()


def test_account_without_any_sidecar_credential_keeps_the_collector_wording(session):
    _source(session, None, timedelta(days=3))

    assert (
        _missing_collector_reason(session, "anthropic_console", "org")
        == "its collector is no longer active"
    )


def test_credential_still_being_reported_is_not_blamed(session):
    _source(session, "dev-01", timedelta(minutes=2))

    assert (
        _missing_collector_reason(session, "anthropic_console", "org")
        == "its collector is no longer active"
    )


def test_silent_sidecar_credential_names_the_sidecar(session):
    _host(session, "dev-01", "Linux/7.0")
    _source(session, "dev-01", timedelta(days=3))

    assert (
        _missing_collector_reason(session, "anthropic_console", "org")
        == "its credential from dev-01 stopped reporting"
    )


def test_silent_legacy_mac_gets_the_pairing_and_keychain_hint(session):
    _host(session, "macbook", "Darwin/25.6.0")
    _source(session, "macbook", timedelta(days=3))

    reason = _missing_collector_reason(session, "anthropic_console", "org")

    assert reason.startswith("its credential from macbook stopped reporting; check this Mac")
    assert " — " not in reason  # the failing prefix ends a reason at its separator


def test_paired_mac_is_not_told_to_pair_again(session):
    paired = "device-" + "a" * 32
    _host(session, paired, "Darwin/25.6.0")
    _source(session, paired, timedelta(days=3))

    assert "paired" not in _missing_collector_reason(session, "anthropic_console", "org")
