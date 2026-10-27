"""Tests for app/services/maintenance/sidecar_adopt.py and the hostname default at pairing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import (
    CredentialTag,
    PendingUsageEvent,
    SidecarDevice,
    SidecarRegistry,
    UsageEvent,
    UsagePeriodRollup,
)
from app.services.fleet_registry import fleet_registry
from app.services.maintenance.sidecar_adopt import (
    adoptable_legacy,
    apply_adopt_sidecar,
    plan_adopt_sidecar,
)

DEVICE = "device-" + "a" * 32
T0 = datetime(2026, 10, 27, 21, 0, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _registry(session: Session, sidecar_id: str, **overrides) -> SidecarRegistry:
    base = {
        "sidecar_id": sidecar_id,
        "hostname": "desktop",
        "os_platform": "Windows/11",
        "first_seen": T0 - timedelta(days=20),
        "last_seen": T0,
    }
    base.update(overrides)
    row = SidecarRegistry(**base)
    session.add(row)
    session.commit()
    return row


def _pair(session: Session, **legacy_overrides) -> None:
    _registry(session, "desktop", **legacy_overrides)
    _registry(
        session, DEVICE, first_seen=T0 + timedelta(minutes=1), last_seen=T0 + timedelta(hours=1)
    )


def _event(session: Session, event_id: str, sidecar_id: str, hour: int = 1) -> None:
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="me@example.com",
            sidecar_id=sidecar_id,
            event_id=event_id,
            ts=datetime(2026, 10, 1, hour, tzinfo=UTC),
            kind="message",
            model_id="claude",
            tokens_input=10,
            tokens_output=5,
            cost_usd=0.5,
        )
    )
    session.commit()


def test_candidate_matches_hostname_os_and_handover_timing():
    session = _session()
    _pair(session)

    found = adoptable_legacy(list(session.exec(select(SidecarRegistry)).all()))

    assert [c["sidecar_id"] for c in found[DEVICE]] == ["desktop"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"os_platform": "Darwin/25"},
        {"hostname": "laptop"},
        {"last_seen": T0 + timedelta(hours=2)},  # still reporting after the device appeared
    ],
)
def test_candidate_rejects_a_different_machine(overrides):
    session = _session()
    _pair(session, **overrides)

    assert adoptable_legacy(list(session.exec(select(SidecarRegistry)).all())) == {}


def test_plan_refuses_a_legacy_row_that_is_not_a_candidate():
    session = _session()
    _pair(session, hostname="laptop")

    with pytest.raises(ValueError, match="can adopt"):
        plan_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)


def test_plan_refuses_a_device_that_is_not_enrolled():
    session = _session()
    _registry(session, "desktop")
    _registry(session, "other")

    with pytest.raises(ValueError, match="enrolled device"):
        plan_adopt_sidecar(session, legacy_id="desktop", device_id="other")


def test_apply_moves_history_tags_and_settings_then_retires_the_legacy_row():
    session = _session()
    _pair(session, custom_name="Office PC", tags_json='["work"]', log_level_desired="DEBUG")
    device = session.get(SidecarRegistry, DEVICE)
    device.log_level_desired = "INFO"  # the device's own choice wins
    session.add(device)
    _event(session, "e1", "desktop", hour=1)
    _event(session, "e2", "desktop", hour=2)
    _event(session, "e3", DEVICE, hour=3)
    session.add(
        PendingUsageEvent(
            provider_id="anthropic", event_id="p1", sidecar_id="desktop", ts=T0, payload_json="{}"
        )
    )
    session.add(
        PendingUsageEvent(
            provider_id="anthropic", event_id="p1", sidecar_id=DEVICE, ts=T0, payload_json="{}"
        )
    )
    session.add(
        CredentialTag(
            provider_id="xai", credential_origin="path:a", account_id="me", sidecar_id="desktop"
        )
    )
    session.add(
        CredentialTag(
            provider_id="xai", credential_origin="path:a", account_id="me", sidecar_id=DEVICE
        )
    )
    session.add(
        CredentialTag(
            provider_id="chatgpt", credential_origin="path:b", account_id="me", sidecar_id="desktop"
        )
    )
    session.commit()

    preview = plan_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)
    assert (preview.events, preview.pending_events, preview.credential_tags) == (2, 1, 1)
    assert sorted(preview.settings) == ["custom_name", "tags_json"]

    result = apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert result.events == 2
    assert session.get(SidecarRegistry, "desktop") is None
    assert {e.sidecar_id for e in session.exec(select(UsageEvent))} == {DEVICE}
    assert [p.sidecar_id for p in session.exec(select(PendingUsageEvent))] == [DEVICE]
    tags = {(t.provider_id, t.sidecar_id) for t in session.exec(select(CredentialTag))}
    assert tags == {("xai", DEVICE), ("chatgpt", DEVICE)}
    device = session.get(SidecarRegistry, DEVICE)
    assert (device.custom_name, device.tags_json, device.log_level_desired) == (
        "Office PC",
        '["work"]',
        "INFO",
    )
    # Derived per-sidecar rollups were rebuilt from the moved events.
    per_sidecar = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "lifetime", UsagePeriodRollup.model_id == ""
        )
    ).all()
    assert {r.sidecar_id for r in per_sidecar} <= {"", DEVICE}
    assert sum(r.msgs for r in per_sidecar if r.sidecar_id == DEVICE) == 3


def test_new_device_row_takes_the_hostname_recorded_at_pairing():
    session = _session()
    session.add(
        SidecarDevice(
            device_id="a" * 32, hostname="desktop", platform="Windows", secret_encrypted="x"
        )
    )
    session.commit()

    row = fleet_registry.upsert_sidecar(DEVICE, "10.0.0.1", session, os_platform="Windows/11")

    assert row.hostname == "desktop"


def test_row_registered_with_its_id_as_hostname_heals_on_next_checkin():
    session = _session()
    session.add(
        SidecarDevice(
            device_id="a" * 32, hostname="desktop", platform="Windows", secret_encrypted="x"
        )
    )
    _registry(session, DEVICE, hostname=DEVICE)

    row = fleet_registry.upsert_sidecar(DEVICE, "10.0.0.1", session)

    assert row.hostname == "desktop"


def test_legacy_sidecar_keeps_the_id_it_reported_as_hostname():
    session = _session()

    row = fleet_registry.upsert_sidecar("macbook", "10.0.0.1", session)

    assert row.hostname == "macbook"
