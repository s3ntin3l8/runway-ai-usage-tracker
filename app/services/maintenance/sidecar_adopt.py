"""Fold a legacy (shared-key) sidecar into the enrolled device that replaced it.

Re-pairing a machine enrolls a new ``device-<hex>`` identity; the shared-key
entry it reported under until then (``desktop``) goes silent but keeps its
history, tags and settings. This moves that per-machine state onto the device
and retires the legacy row, so one machine is one card again.

A hostname is only a label, so nothing here runs on its own: the Fleet UI
offers the merge (``adoptable_legacy``) and an operator confirms it.

What moves is what is keyed by ``sidecar_id``: ``usage_events`` (authoritative),
``pending_usage_events``, machine-scoped credential tags, and the registry's
own settings. The per-sidecar rollups and closed windows are derived, so they
are rebuilt from the moved events, never patched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, text
from sqlmodel import Session, col, select

from app.models.db import (
    CredentialTag,
    PendingUsageEvent,
    SidecarRegistry,
    UsageEvent,
)
from app.services import sidecar_devices
from app.services.account_identity import normalize_sidecar_id
from app.services.maintenance._chunked_sql import chunked_update
from app.services.maintenance.rollups import rebuild_rollups_for_pairs
from app.services.maintenance.windows import rebuild_windows_overlapping

# A legacy sidecar must have stopped before its replacement showed up; a short
# grace covers the sidecar's final check-in racing the pairing handshake.
_HANDOVER_GRACE = timedelta(minutes=10)

# Registry columns that carry an operator's choice for this machine. Copied onto
# the device only where the device has none of its own.
_SETTING_FIELDS = (
    "custom_name",
    "tags_json",
    "keep_alive_desired",
    "keep_alive_desired_providers",
    "auto_update_desired",
    "update_channel_desired",
    "log_level_desired",
    "login_dirs_desired",
)


def _os_family(os_platform: str | None) -> str:
    return (os_platform or "").split("/", 1)[0].lower()


def _same_machine(legacy: SidecarRegistry, device: SidecarRegistry) -> bool:
    legacy_name = normalize_sidecar_id(legacy.hostname or legacy.sidecar_id)
    device_name = normalize_sidecar_id(device.hostname or "")
    family = _os_family(device.os_platform)
    return (
        bool(legacy_name)
        and legacy_name == device_name
        and bool(family)
        and family == _os_family(legacy.os_platform)
        # Still reporting after the device appeared: a second install, not its predecessor.
        and legacy.last_seen <= device.first_seen + _HANDOVER_GRACE
    )


def adoptable_legacy(rows: list[SidecarRegistry]) -> dict[str, list[dict[str, Any]]]:
    """Map each enrolled device's registry id to the legacy rows it likely replaced.

    Works on rows the caller already loaded, so listing the fleet costs no extra query."""
    legacy_rows = [r for r in rows if not sidecar_devices.reserved_registry_id(r.sidecar_id)]
    result: dict[str, list[dict[str, Any]]] = {}
    for device in rows:
        if not sidecar_devices.reserved_registry_id(device.sidecar_id):
            continue
        matches = [
            {
                "sidecar_id": legacy.sidecar_id,
                "last_seen": legacy.last_seen.isoformat(),
                "ingest_count": legacy.ingest_count,
            }
            for legacy in legacy_rows
            if _same_machine(legacy, device)
        ]
        if matches:
            result[device.sidecar_id] = matches
    return result


@dataclass
class AdoptPlan:
    events: int = 0
    pending_events: int = 0
    credential_tags: int = 0
    settings: list[str] = field(default_factory=list)
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    pairs: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class AdoptResult:
    events: int = 0
    pending_events: int = 0
    credential_tags: int = 0
    settings: list[str] = field(default_factory=list)
    rollup_pairs_rebuilt: int = 0
    windows_rebuilt: int = 0


def _candidates(session: Session, device_id: str) -> dict[str, SidecarRegistry]:
    rows = list(session.exec(select(SidecarRegistry)).all())
    return {
        legacy["sidecar_id"]: next(r for r in rows if r.sidecar_id == legacy["sidecar_id"])
        for legacy in adoptable_legacy(rows).get(device_id, [])
    }


def _check(
    session: Session, legacy_id: str, device_id: str
) -> tuple[SidecarRegistry, SidecarRegistry]:
    if not sidecar_devices.reserved_registry_id(device_id):
        raise ValueError("Only an enrolled device can adopt a legacy sidecar")
    device = session.get(SidecarRegistry, device_id)
    if device is None:
        raise ValueError(f"Sidecar '{device_id}' not found")
    legacy = _candidates(session, device_id).get(legacy_id)
    if legacy is None:
        raise ValueError(f"'{legacy_id}' is not a legacy sidecar this device can adopt")
    return legacy, device


def _settings_to_copy(legacy: SidecarRegistry, device: SidecarRegistry) -> list[str]:
    def unset(value: Any) -> bool:
        return value in (None, "", "{}", "[]")

    return [
        name
        for name in _SETTING_FIELDS
        if not unset(getattr(legacy, name)) and unset(getattr(device, name))
    ]


def plan_adopt_sidecar(session: Session, *, legacy_id: str, device_id: str) -> AdoptPlan:
    """Read-only preview of what an adoption would move."""
    legacy, device = _check(session, legacy_id, device_id)
    plan = AdoptPlan(settings=_settings_to_copy(legacy, device))
    plan.events, plan.ts_min, plan.ts_max = session.execute(
        select(func.count(), func.min(UsageEvent.ts), func.max(UsageEvent.ts)).where(
            UsageEvent.sidecar_id == legacy_id
        )
    ).one()
    plan.pairs = {
        (provider, account)
        for provider, account in session.execute(
            select(UsageEvent.provider_id, UsageEvent.account_id)
            .where(UsageEvent.sidecar_id == legacy_id)
            .distinct()
        )
    }
    plan.pending_events = session.execute(
        select(func.count()).where(PendingUsageEvent.sidecar_id == legacy_id)
    ).scalar_one()
    plan.credential_tags = len(_tags_to_move(session, legacy_id, device_id))
    return plan


def _tags_to_move(session: Session, legacy_id: str, device_id: str) -> list[CredentialTag]:
    held = {
        (tag.provider_id, tag.credential_origin)
        for tag in session.exec(select(CredentialTag).where(CredentialTag.sidecar_id == device_id))
    }
    return [
        tag
        for tag in session.exec(select(CredentialTag).where(CredentialTag.sidecar_id == legacy_id))
        if (tag.provider_id, tag.credential_origin) not in held
    ]


def apply_adopt_sidecar(session: Session, *, legacy_id: str, device_id: str) -> AdoptResult:
    """Move the legacy sidecar's history and settings onto the device, then retire it.

    The event retag is chunked (``_chunked_sql``) so a long history never holds
    SQLite's writer lock in one transaction. Re-running after a partial failure
    resumes: the legacy row only goes last, and a second call finds nothing left
    to move."""
    legacy, device = _check(session, legacy_id, device_id)
    plan = plan_adopt_sidecar(session, legacy_id=legacy_id, device_id=device_id)

    chunked_update(
        session, UsageEvent, [col(UsageEvent.sidecar_id) == legacy_id], {"sidecar_id": device_id}
    )

    # Pending events are unique per (provider, event, sidecar): a copy the device already
    # reported wins, the legacy duplicate is dropped.
    session.execute(
        text(
            "DELETE FROM pending_usage_events WHERE sidecar_id = :legacy AND EXISTS ("
            "SELECT 1 FROM pending_usage_events d WHERE d.sidecar_id = :device "
            "AND d.provider_id = pending_usage_events.provider_id "
            "AND d.event_id = pending_usage_events.event_id)"
        ),
        {"legacy": legacy_id, "device": device_id},
    )
    chunked_update(
        session,
        PendingUsageEvent,
        [col(PendingUsageEvent.sidecar_id) == legacy_id],
        {"sidecar_id": device_id},
    )

    for tag in _tags_to_move(session, legacy_id, device_id):
        tag.sidecar_id = device_id
        session.add(tag)

    for name in plan.settings:
        setattr(device, name, getattr(legacy, name))
    session.add(device)
    session.commit()

    result = AdoptResult(
        events=plan.events,
        pending_events=plan.pending_events,
        credential_tags=plan.credential_tags,
        settings=plan.settings,
    )
    if plan.pairs and plan.ts_min is not None and plan.ts_max is not None:
        # Both are derived from events, which now carry the device id.
        rebuild_rollups_for_pairs(session, plan.pairs)
        session.commit()
        result.rollup_pairs_rebuilt = len(plan.pairs)
        for provider_id in sorted({provider for provider, _ in plan.pairs}):
            result.windows_rebuilt += rebuild_windows_overlapping(
                session,
                provider_id=provider_id,
                account_ids=sorted({a for p, a in plan.pairs if p == provider_id}),
                ts_min=plan.ts_min,
                ts_max=plan.ts_max,
            )

    # Last: the legacy row's own tags, pending origins and discovered credentials go with it.
    from app.services.fleet_registry import fleet_registry

    fleet_registry.delete_sidecar(legacy_id, session)
    return result
