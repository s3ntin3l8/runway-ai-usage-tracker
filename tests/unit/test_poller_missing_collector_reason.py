"""Why a server contribution goes stale when its collector has no outcome (#1027)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import CredentialSource, SidecarDevice, SidecarRegistry
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


def _source(
    session: Session,
    sidecar_id: str | None,
    age: timedelta,
    *,
    enabled: bool = True,
    source_type: str = "cookie",
) -> None:
    session.add(
        CredentialSource(
            provider_id="anthropic_console",
            account_id="org",
            source_id=f"sidecar:{sidecar_id}",
            source_type=source_type if sidecar_id else "config",
            source_label="Chrome cookie",
            sidecar_id=sidecar_id,
            last_seen=NOW - age,
            enabled=enabled,
        )
    )
    session.commit()


def _host(session: Session, sidecar_id: str, os_platform: str, **fields) -> None:
    fields.setdefault("hostname", sidecar_id)
    session.add(SidecarRegistry(sidecar_id=sidecar_id, os_platform=os_platform, **fields))
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


def _device(session: Session, device_id: str, *, revoked: bool) -> None:
    session.add(
        SidecarDevice(
            device_id=device_id,
            hostname="mac",
            platform="Darwin",
            secret_encrypted="x",
            revoked=revoked,
        )
    )
    session.commit()


def test_paired_mac_is_not_told_to_pair_again(session):
    device_id = "a" * 32
    _host(session, "device-" + device_id, "Darwin/25.6.0")
    _device(session, device_id, revoked=False)
    _source(session, "device-" + device_id, timedelta(days=3))

    assert "paired" not in _missing_collector_reason(session, "anthropic_console", "org")


def test_unpaired_mac_with_a_revoked_device_is_told_to_pair_again(session):
    device_id = "b" * 32
    _host(session, "device-" + device_id, "Darwin/25.6.0")
    _device(session, device_id, revoked=True)
    _source(session, "device-" + device_id, timedelta(days=3))

    assert "paired" in _missing_collector_reason(session, "anthropic_console", "org")


def test_the_machine_is_named_by_its_hostname_or_custom_name(session):
    _host(session, "device-" + "c" * 32, "Linux/7.0", hostname="dev-01", custom_name="Dev box")
    _source(session, "device-" + "c" * 32, timedelta(days=3))

    assert "from Dev box stopped" in _missing_collector_reason(session, "anthropic_console", "org")


def test_the_local_fallback_and_disabled_sources_are_not_blamed(session):
    _source(session, "local", timedelta(days=3))
    _source(session, "dev-01", timedelta(days=3), enabled=False)

    assert (
        _missing_collector_reason(session, "anthropic_console", "org")
        == "its collector is no longer active"
    )


@pytest.mark.parametrize("kind", ["cookie", "keychain", "sidecar"])
def test_a_silent_keychain_governed_or_unknown_kind_on_a_mac_gets_the_hint(session, kind):
    _host(session, "macbook", "Darwin/25.6.0")
    _source(session, "macbook", timedelta(days=3), source_type=kind)

    reason = _missing_collector_reason(session, "anthropic_console", "org")

    assert reason.startswith("its credential from macbook stopped reporting; check this Mac")


@pytest.mark.parametrize("kind", ["env", "file"])
def test_a_silent_env_or_file_credential_on_a_mac_gets_no_keychain_hint(session, kind):
    _host(session, "macbook", "Darwin/25.6.0")
    _source(session, "macbook", timedelta(days=3), source_type=kind)

    assert (
        _missing_collector_reason(session, "anthropic_console", "org")
        == "its credential from macbook stopped reporting"
    )
