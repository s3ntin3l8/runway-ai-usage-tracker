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
    UsageWindow,
)
from app.services.account_identity import FINGERPRINT_LEN
from app.services.fleet_registry import fleet_registry
from app.services.maintenance.sidecar_adopt import (
    SidecarNotFoundError,
    adoptable_legacy,
    apply_adopt_sidecar,
    load_enrollment_times,
    plan_adopt_sidecar,
    replaced_by,
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
    # p1 is already on the device, so only the duplicate-free count would move (none here).
    assert (preview.events, preview.pending_events, preview.credential_tags) == (2, 0, 1)
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


def test_preview_counts_only_the_pending_events_that_would_move():
    session = _session()
    _pair(session)
    for event_id, sidecar_id in (("p1", "desktop"), ("p2", "desktop"), ("p1", DEVICE)):
        session.add(
            PendingUsageEvent(
                provider_id="anthropic",
                event_id=event_id,
                sidecar_id=sidecar_id,
                ts=T0,
                payload_json="{}",
            )
        )
    session.commit()

    assert plan_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE).pending_events == 1


def test_unknown_sidecars_are_not_found_rather_than_refused():
    session = _session()
    _pair(session)

    with pytest.raises(SidecarNotFoundError):
        plan_adopt_sidecar(session, legacy_id="nobody", device_id=DEVICE)
    with pytest.raises(SidecarNotFoundError):
        plan_adopt_sidecar(session, legacy_id="desktop", device_id="device-" + "f" * 32)


def test_retry_after_a_failed_rebuild_still_repairs_the_derived_rows(monkeypatch):
    session = _session()
    _pair(session)
    _event(session, "e1", "desktop")
    session.add(
        UsageWindow(
            provider_id="anthropic",
            account_id="me@example.com",
            window_type="session",
            window_start=datetime(2026, 9, 30, 23, tzinfo=UTC),
            window_end=datetime(2026, 10, 1, 4, tzinfo=UTC),
            sidecar_id="desktop",
            msgs=1,
        )
    )
    session.commit()

    from app.services.maintenance import sidecar_adopt

    real = sidecar_adopt.rebuild_windows_overlapping

    def boom(*args, **kwargs):
        raise RuntimeError("rebuild interrupted")

    monkeypatch.setattr(sidecar_adopt, "rebuild_windows_overlapping", boom)
    with pytest.raises(RuntimeError):
        apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)
    # The events moved and were committed; the legacy row must still be there to retry.
    assert session.get(SidecarRegistry, "desktop") is not None
    assert {e.sidecar_id for e in session.exec(select(UsageEvent))} == {DEVICE}

    monkeypatch.setattr(sidecar_adopt, "rebuild_windows_overlapping", real)
    result = apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert result.events == 0  # nothing left to move, yet the derived rows were found
    assert result.windows_rebuilt >= 1
    assert session.get(SidecarRegistry, "desktop") is None
    assert [w.sidecar_id for w in session.exec(select(UsageWindow))] != ["desktop"]
    assert not any(w.sidecar_id == "desktop" for w in session.exec(select(UsageWindow)))


def test_rollups_are_rebuilt_even_when_only_rollup_rows_carry_the_legacy_id():
    session = _session()
    _pair(session)
    session.add(
        UsagePeriodRollup(
            provider_id="anthropic",
            account_id="me@example.com",
            period_type="lifetime",
            period_key="all",
            model_id="",
            sidecar_id="desktop",
            msgs=4,
        )
    )
    session.commit()

    result = apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert result.rollup_pairs_rebuilt == 1
    assert not any(r.sidecar_id == "desktop" for r in session.exec(select(UsagePeriodRollup)))


def _counters(session, sidecar_id):
    row = session.get(SidecarRegistry, sidecar_id)
    session.refresh(row)
    return row.ingest_count, row.error_count, row.first_seen


def test_adoption_carries_the_push_and_error_counts_and_the_earlier_first_seen():
    session = _session()
    _pair(session, ingest_count=18689, error_count=3)
    device = session.get(SidecarRegistry, DEVICE)
    device.ingest_count, device.error_count = 207, 1
    session.add(device)
    session.commit()
    legacy_first_seen = session.get(SidecarRegistry, "desktop").first_seen

    preview = plan_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)
    assert preview.pushes_after == 18689 + 207

    result = apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert (result.pushes_carried, result.errors_carried) == (18689, 3)
    pushes, errors, first_seen = _counters(session, DEVICE)
    assert (pushes, errors) == (18689 + 207, 3 + 1)
    assert first_seen == legacy_first_seen  # the machine is as old as its history


def test_a_failed_adoption_does_not_carry_the_counters_twice_on_retry(monkeypatch):
    session = _session()
    _pair(session, ingest_count=100, error_count=2)
    device = session.get(SidecarRegistry, DEVICE)
    device.ingest_count, device.error_count = 10, 0
    session.add(device)
    session.commit()

    from app.services import fleet_registry as registry

    real = registry.fleet_registry.delete_sidecar

    def boom(*args, **kwargs):
        raise RuntimeError("delete interrupted")

    monkeypatch.setattr(registry.fleet_registry, "delete_sidecar", boom)
    with pytest.raises(RuntimeError):
        apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)
    # Nothing was carried, and the legacy row is still there to retry.
    assert _counters(session, DEVICE)[:2] == (10, 0)
    assert session.get(SidecarRegistry, "desktop") is not None

    monkeypatch.setattr(registry.fleet_registry, "delete_sidecar", real)
    apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert _counters(session, DEVICE)[:2] == (110, 2)  # carried exactly once


def test_the_preview_names_both_sides():
    session = _session()
    _pair(session, ingest_count=500, custom_name="Office PC")

    preview = plan_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert preview.legacy["sidecar_id"] == "desktop"
    assert preview.legacy["name"] == "Office PC"
    assert preview.legacy["ingest_count"] == 500
    assert preview.device["sidecar_id"] == DEVICE
    assert preview.device["name"] == "desktop"  # the device's hostname, not its id


def test_replaced_by_is_the_inverse_of_adoptable_legacy():
    session = _session()
    _pair(session)
    rows = list(session.exec(select(SidecarRegistry)).all())

    forward = adoptable_legacy(rows)
    inverse = replaced_by(rows)

    assert [c["sidecar_id"] for c in forward[DEVICE]] == ["desktop"]
    assert [d["sidecar_id"] for d in inverse["desktop"]] == [DEVICE]
    assert DEVICE not in inverse and "desktop" not in forward


def test_merging_one_old_entry_keeps_the_other_old_entry_offered():
    """The merge moves the device's first_seen back to the old entry's. The "same machine" check
    must not depend on it, or every *other* old entry of the machine drops out of the offer."""
    session = _session()
    _pair(session)
    _registry(session, "docker-desktop", hostname="desktop")  # a second old entry, same machine
    session.add(
        SidecarDevice(
            device_id=DEVICE.removeprefix("device-"),
            hostname="desktop",
            platform="Windows",
            secret_encrypted="x",
            created_at=T0 + timedelta(minutes=1),
        )
    )
    session.commit()
    enrolled = load_enrollment_times(session)
    rows = list(session.exec(select(SidecarRegistry)).all())
    assert sorted(c["sidecar_id"] for c in adoptable_legacy(rows, enrolled)[DEVICE]) == [
        "desktop",
        "docker-desktop",
    ]

    apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert session.get(SidecarRegistry, DEVICE).first_seen < T0  # it did move back
    enrolled = load_enrollment_times(session)
    rows = list(session.exec(select(SidecarRegistry)).all())
    assert [c["sidecar_id"] for c in adoptable_legacy(rows, enrolled)[DEVICE]] == ["docker-desktop"]
    assert [d["sidecar_id"] for d in replaced_by(rows, enrolled)["docker-desktop"]] == [DEVICE]
    # And a second merge is still accepted rather than answering "not a candidate".
    assert (
        plan_adopt_sidecar(session, legacy_id="docker-desktop", device_id=DEVICE).legacy[
            "sidecar_id"
        ]
        == "docker-desktop"
    )


def test_enrollment_times_can_be_read_for_just_the_devices_in_hand():
    session = _session()
    other = "c" * 32
    for device_id, minutes in (("a" * 32, 1), (other, 5)):
        session.add(
            SidecarDevice(
                device_id=device_id,
                hostname="h",
                platform="Linux",
                secret_encrypted="x",
                created_at=T0 + timedelta(minutes=minutes),
            )
        )
    session.commit()

    assert set(load_enrollment_times(session)) == {DEVICE, "device-" + other}
    assert set(load_enrollment_times(session, [DEVICE])) == {DEVICE}
    # A legacy id has no device record, and no ids means nothing to read.
    assert load_enrollment_times(session, ["desktop"]) == {}
    assert load_enrollment_times(session, []) == {}


def test_a_device_without_an_enrollment_record_falls_back_to_its_first_seen():
    session = _session()
    _pair(session)  # no SidecarDevice row at all
    rows = list(session.exec(select(SidecarRegistry)).all())

    found = adoptable_legacy(rows, load_enrollment_times(session, [DEVICE]))

    assert [c["sidecar_id"] for c in found[DEVICE]] == ["desktop"]


def test_a_missing_enrollment_record_is_logged_once_at_debug(caplog):
    session = _session()
    session.add(
        SidecarDevice(
            device_id="a" * 32, hostname="h", platform="Linux", secret_encrypted="x", created_at=T0
        )
    )
    session.commit()
    gone = "device-" + "d" * 32

    with caplog.at_level("DEBUG", logger="app.services.maintenance.sidecar_adopt"):
        load_enrollment_times(session, [DEVICE, gone])

    messages = [r.getMessage() for r in caplog.records if "enrollment record" in r.getMessage()]
    assert len(messages) == 1 and gone in messages[0] and DEVICE not in messages[0]


def test_nothing_is_logged_when_every_requested_device_has_a_record(caplog):
    session = _session()
    session.add(
        SidecarDevice(
            device_id="a" * 32, hostname="h", platform="Linux", secret_encrypted="x", created_at=T0
        )
    )
    session.commit()

    with caplog.at_level("DEBUG", logger="app.services.maintenance.sidecar_adopt"):
        load_enrollment_times(session, [DEVICE, "desktop"])  # a legacy id is not a device

    assert not [r for r in caplog.records if "enrollment record" in r.getMessage()]


def _source(session, sidecar_id, origin, *, account="me@example.com", provider="anthropic", **kw):
    from app.models.db import CredentialSource

    row = CredentialSource(
        provider_id=provider,
        account_id=account,
        source_id=f"{sidecar_id}:{origin}",
        source_type="sidecar",
        source_label=origin,
        credential_origin=origin,
        sidecar_id=sidecar_id,
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def _prefs(session, sidecar_id):
    from app.models.db import CredentialSource

    return {
        row.credential_origin: (row.enabled, row.priority)
        for row in session.exec(
            select(CredentialSource).where(CredentialSource.sidecar_id == sidecar_id)
        )
    }


def test_a_disabled_source_and_a_custom_priority_survive_the_merge():
    session = _session()
    _pair(session)
    _source(session, "desktop", "cli", enabled=False, priority=0)
    _source(session, "desktop", "browser", enabled=True, priority=5)
    _source(session, DEVICE, "cli")  # rediscovered with defaults
    _source(session, DEVICE, "browser")

    preview = plan_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)
    result = apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert preview.source_prefs == result.source_prefs == 2
    assert _prefs(session, DEVICE) == {"cli": (False, 0), "browser": (True, 5)}


def test_unmatched_and_ambiguous_sources_are_left_alone():
    session = _session()
    _pair(session)
    _source(session, "desktop", "gone", enabled=False)  # the device never found it
    _source(session, "desktop", "cli", enabled=False, account="a@example.com")
    _source(session, DEVICE, "cli", account="b@example.com")  # same origin, different account
    # Two device rows for one (provider, account, origin) key: guessing would be wrong.
    _source(session, "desktop", "dup", enabled=False)
    _source(session, DEVICE, "dup")
    twin = _source(session, DEVICE, "dup2")
    twin.credential_origin = "dup"
    session.add(twin)
    session.commit()

    result = apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert result.source_prefs == 0
    assert _prefs(session, DEVICE).get("gone") is None  # not created
    assert all(enabled for enabled, _ in _prefs(session, DEVICE).values())


def test_a_retry_after_a_failed_delete_applies_the_source_preferences_once(monkeypatch):
    session = _session()
    _pair(session)
    _source(session, "desktop", "cli", enabled=False, priority=3)
    _source(session, DEVICE, "cli")

    from app.services import fleet_registry as registry

    real = registry.fleet_registry.delete_sidecar

    def boom(*args, **kwargs):
        raise RuntimeError("delete interrupted")

    monkeypatch.setattr(registry.fleet_registry, "delete_sidecar", boom)
    with pytest.raises(RuntimeError):
        apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)
    # The copy rode the failed transaction, so nothing changed yet.
    assert _prefs(session, DEVICE) == {"cli": (True, 0)}

    monkeypatch.setattr(registry.fleet_registry, "delete_sidecar", real)
    result = apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert result.source_prefs == 1
    assert _prefs(session, DEVICE) == {"cli": (False, 3)}


XAI_A = f"env:XAI_API_KEY#{'a' * FINGERPRINT_LEN}"
XAI_B = f"env:XAI_API_KEY#{'b' * FINGERPRINT_LEN}"


def test_a_pre_fingerprint_origin_matches_the_devices_fingerprinted_one():
    session = _session()
    _pair(session)
    fp = "c" * FINGERPRINT_LEN
    _source(session, "desktop", "env:DEEPSEEK_API_KEY", provider="deepseek", enabled=False)
    _source(session, DEVICE, f"env:DEEPSEEK_API_KEY#{fp}", provider="deepseek")

    result = apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert result.source_prefs == 1
    assert _prefs(session, DEVICE) == {f"env:DEEPSEEK_API_KEY#{fp}": (False, 0)}


def test_two_device_fingerprints_for_one_origin_are_not_guessed_between():
    session = _session()
    _pair(session)
    _source(session, "desktop", "env:XAI_API_KEY", provider="xai", enabled=False)
    _source(session, DEVICE, XAI_A, provider="xai")
    _source(session, DEVICE, XAI_B, provider="xai")

    assert apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE).source_prefs == 0


def test_the_newest_of_several_old_rows_for_one_origin_carries_the_choice():
    session = _session()
    _pair(session)
    # A rotated key leaves stale rows behind; only the newest holds the current choice.
    _source(
        session,
        "desktop",
        XAI_A,
        provider="xai",
        enabled=True,
        last_seen=T0 - timedelta(days=9),
    )
    _source(
        session,
        "desktop",
        XAI_B,
        provider="xai",
        enabled=False,
        priority=4,
        last_seen=T0,
    )
    _source(session, DEVICE, XAI_B, provider="xai")

    result = apply_adopt_sidecar(session, legacy_id="desktop", device_id=DEVICE)

    assert result.source_prefs == 1
    assert _prefs(session, DEVICE) == {XAI_B: (False, 4)}
