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

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, text, update
from sqlmodel import Session, col, select

from app.models.db import (
    CredentialTag,
    PendingUsageEvent,
    SidecarDevice,
    SidecarRegistry,
    UsageEvent,
    UsagePeriodRollup,
    UsageWindow,
)
from app.services import sidecar_devices
from app.services.account_identity import normalize_sidecar_id
from app.services.maintenance._chunked_sql import chunked_update
from app.services.maintenance.rollups import rebuild_rollups_for_pairs
from app.services.maintenance.windows import rebuild_windows_overlapping

logger = logging.getLogger(__name__)

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


def load_enrollment_times(
    session: Session, registry_ids: Iterable[str] | None = None
) -> dict[str, datetime]:
    """Registry id -> the instant a device was enrolled (when its pairing code was redeemed).

    Pass the registry ids in hand to read only those devices (the fleet list and a merge need a
    handful of rows, not the whole table); ``None`` reads every device.

    The handover check below must compare against this, not the device row's ``first_seen``:
    ``first_seen`` is something a merge deliberately moves back to the old entry's, and the
    comparison would then stop offering every *other* old entry of the same machine."""
    statement = select(SidecarDevice)
    requested: list[str] = []
    if registry_ids is not None:
        requested = [
            registry_id.removeprefix("device-")
            for registry_id in registry_ids
            if sidecar_devices.reserved_registry_id(registry_id)
        ]
        statement = statement.where(col(SidecarDevice.device_id).in_(requested))
    found = {
        sidecar_devices.registry_id(device.device_id): device.created_at
        for device in session.exec(statement).all()
    }
    missing = sorted(set(map(sidecar_devices.registry_id, requested)) - set(found))
    if missing:
        # Not an error: such a device falls back to its own first_seen in the handover check,
        # which a merge can move. Say so once here rather than per pair inside the check.
        logger.debug(
            "No enrollment record for %s; using first_seen for the handover check", missing
        )
    return found


def _same_machine(
    legacy: SidecarRegistry, device: SidecarRegistry, enrolled_at: dict[str, datetime]
) -> bool:
    legacy_name = normalize_sidecar_id(legacy.hostname or legacy.sidecar_id)
    device_name = normalize_sidecar_id(device.hostname or "")
    family = _os_family(device.os_platform)
    # A device row with no SidecarDevice record (so no enrollment time) falls back to its own
    # first_seen, which is what the check used before enrollment times existed. Keep it: dropping
    # the fallback would make such a row match nothing. (A merge moves first_seen back, so for a
    # row that does have a record the enrollment time above must win.)
    enrolled = enrolled_at.get(device.sidecar_id, device.first_seen)
    return (
        bool(legacy_name)
        and legacy_name == device_name
        and bool(family)
        and family == _os_family(legacy.os_platform)
        # Still reporting after the device appeared: a second install, not its predecessor.
        and legacy.last_seen <= enrolled + _HANDOVER_GRACE
    )


def adoptable_legacy(
    rows: list[SidecarRegistry], enrolled_at: dict[str, datetime] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Map each enrolled device's registry id to the legacy rows it likely replaced.

    Works on rows the caller already loaded, so listing the fleet costs no extra query."""
    enrolled_at = enrolled_at or {}
    legacy_rows = [r for r in rows if not sidecar_devices.reserved_registry_id(r.sidecar_id)]
    result: dict[str, list[dict[str, Any]]] = {}
    for device in rows:
        if not sidecar_devices.reserved_registry_id(device.sidecar_id):
            continue
        matches = [
            _describe(legacy)
            for legacy in legacy_rows
            if _same_machine(legacy, device, enrolled_at)
        ]
        if matches:
            result[device.sidecar_id] = matches
    return result


def _describe(row: SidecarRegistry) -> dict[str, Any]:
    """The facts an operator needs to tell two entries for one machine apart."""
    return {
        "sidecar_id": row.sidecar_id,
        "name": row.custom_name or row.hostname or row.sidecar_id,
        "os_platform": row.os_platform,
        "first_seen": row.first_seen.isoformat(),
        "last_seen": row.last_seen.isoformat(),
        "ingest_count": row.ingest_count,
    }


def replaced_by(
    rows: list[SidecarRegistry], enrolled_at: dict[str, datetime] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """The inverse of ``adoptable_legacy``: map each legacy row to the enrolled device(s) that
    replaced it, so the old entry's card can offer the same merge."""
    enrolled_at = enrolled_at or {}
    devices = [r for r in rows if sidecar_devices.reserved_registry_id(r.sidecar_id)]
    result: dict[str, list[dict[str, Any]]] = {}
    for legacy in rows:
        if sidecar_devices.reserved_registry_id(legacy.sidecar_id):
            continue
        matches = [
            _describe(device) for device in devices if _same_machine(legacy, device, enrolled_at)
        ]
        if matches:
            result[legacy.sidecar_id] = matches
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
    # Both sides, so a preview can say which entry goes into which.
    legacy: dict[str, Any] = field(default_factory=dict)
    device: dict[str, Any] = field(default_factory=dict)
    pushes_after: int = 0


@dataclass
class AdoptResult:
    events: int = 0
    pending_events: int = 0
    credential_tags: int = 0
    settings: list[str] = field(default_factory=list)
    rollup_pairs_rebuilt: int = 0
    windows_rebuilt: int = 0
    pushes_carried: int = 0
    errors_carried: int = 0


class SidecarNotFoundError(LookupError):
    """Named so a stray ``KeyError``/``IndexError`` from inside a rebuild is never reported as 404."""


def _check(
    session: Session, legacy_id: str, device_id: str
) -> tuple[SidecarRegistry, SidecarRegistry]:
    """Raises ``SidecarNotFoundError`` for a sidecar that does not exist, ``ValueError`` for a refusal."""
    rows = {row.sidecar_id: row for row in session.exec(select(SidecarRegistry)).all()}
    device, legacy = rows.get(device_id), rows.get(legacy_id)
    if device is None:
        raise SidecarNotFoundError(f"Sidecar '{device_id}' not found")
    if legacy is None:
        raise SidecarNotFoundError(f"Sidecar '{legacy_id}' not found")
    if not sidecar_devices.reserved_registry_id(device_id):
        raise ValueError("Only an enrolled device can adopt a legacy sidecar")
    candidates = adoptable_legacy(
        list(rows.values()), load_enrollment_times(session, [device_id])
    ).get(device_id, [])
    if legacy_id not in {c["sidecar_id"] for c in candidates}:
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


def _plan(session: Session, legacy: SidecarRegistry, device: SidecarRegistry) -> AdoptPlan:
    legacy_id, device_id = legacy.sidecar_id, device.sidecar_id
    plan = AdoptPlan(
        settings=_settings_to_copy(legacy, device),
        legacy=_describe(legacy),
        device=_describe(device),
        pushes_after=legacy.ingest_count + device.ingest_count,
    )
    plan.events, plan.ts_min, plan.ts_max = session.execute(
        select(func.count(), func.min(UsageEvent.ts), func.max(UsageEvent.ts)).where(
            UsageEvent.sidecar_id == legacy_id
        )
    ).one()
    # Derived rows still tagged with the legacy id belong in the rebuild too. After the
    # events have moved they are all that is left to find, so a retry of an adoption that
    # failed part-way still repairs them instead of skipping them and deleting the legacy row.
    plan.pairs = {
        (provider, account)
        for provider, account in session.execute(
            select(UsageEvent.provider_id, UsageEvent.account_id)
            .where(UsageEvent.sidecar_id == legacy_id)
            .distinct()
        )
    }
    plan.pairs |= {
        (provider, account)
        for provider, account in session.execute(
            select(UsagePeriodRollup.provider_id, UsagePeriodRollup.account_id)
            .where(UsagePeriodRollup.sidecar_id == legacy_id)
            .distinct()
        )
    }
    for provider, account, start, end in session.execute(
        select(
            UsageWindow.provider_id,
            UsageWindow.account_id,
            func.min(UsageWindow.window_start),
            func.max(UsageWindow.window_end),
        )
        .where(UsageWindow.sidecar_id == legacy_id)
        .group_by(UsageWindow.provider_id, UsageWindow.account_id)
    ):
        plan.pairs.add((provider, account))
        plan.ts_min = min(filter(None, (plan.ts_min, start)))
        plan.ts_max = max(filter(None, (plan.ts_max, end)))
    # What would actually move: a copy the device already reported is dropped, not moved.
    plan.pending_events = session.execute(
        text(
            "SELECT COUNT(*) FROM pending_usage_events p WHERE p.sidecar_id = :legacy "
            "AND NOT EXISTS (SELECT 1 FROM pending_usage_events d WHERE d.sidecar_id = :device "
            "AND d.provider_id = p.provider_id AND d.event_id = p.event_id)"
        ),
        {"legacy": legacy_id, "device": device_id},
    ).scalar_one()
    plan.credential_tags = len(_tags_to_move(session, legacy_id, device_id))
    return plan


def plan_adopt_sidecar(session: Session, *, legacy_id: str, device_id: str) -> AdoptPlan:
    """Read-only preview of what an adoption would move."""
    legacy, device = _check(session, legacy_id, device_id)
    return _plan(session, legacy, device)


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
    resumes: the legacy row only goes last, and the rebuild set is read from the
    derived rows that still carry the legacy id, so it survives the events having
    already moved."""
    legacy, device = _check(session, legacy_id, device_id)
    plan = _plan(session, legacy, device)
    # Read now: the window rebuild below expunges every instance from the session, so these two
    # objects are detached by the time the counters are carried.
    legacy_pushes, legacy_errors = legacy.ingest_count, legacy.error_count
    legacy_first_seen, device_first_seen = legacy.first_seen, device.first_seen

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
    if plan.pairs:
        # Rollups are derived from events, which now carry the device id; rebuilding also
        # drops the rows still tagged with the legacy id. They need no time bounds.
        rebuild_rollups_for_pairs(session, plan.pairs)
        session.commit()
        result.rollup_pairs_rebuilt = len(plan.pairs)
    if plan.pairs and plan.ts_min is not None and plan.ts_max is not None:
        for provider_id in sorted({provider for provider, _ in plan.pairs}):
            result.windows_rebuilt += rebuild_windows_overlapping(
                session,
                provider_id=provider_id,
                account_ids=sorted({a for p, a in plan.pairs if p == provider_id}),
                ts_min=plan.ts_min,
                ts_max=plan.ts_max,
            )

    # Last, in one commit with the legacy row going away: carry its push and error counts and its
    # earlier first-seen onto the device, so the card does not read as a brand-new machine. An
    # atomic UPDATE (not read-modify-write) because the device keeps ingesting while this runs,
    # and in the same transaction as the delete so a retry after a failure cannot add them twice.
    result.pushes_carried, result.errors_carried = legacy_pushes, legacy_errors
    values: dict[str, Any] = {
        "ingest_count": col(SidecarRegistry.ingest_count) + legacy_pushes,
        "error_count": col(SidecarRegistry.error_count) + legacy_errors,
    }
    if legacy_first_seen < device_first_seen:
        values["first_seen"] = legacy_first_seen
    session.execute(
        update(SidecarRegistry).where(col(SidecarRegistry.sidecar_id) == device_id).values(**values)
    )

    # Its own tags, pending origins and discovered credentials go with the legacy row.
    from app.services.fleet_registry import fleet_registry

    try:
        fleet_registry.delete_sidecar(legacy_id, session)
    except Exception:
        # The carried counters are pending in this transaction; never let them outlive a delete
        # that did not happen, or a retry on the same session would add them a second time.
        session.rollback()
        raise
    return result
