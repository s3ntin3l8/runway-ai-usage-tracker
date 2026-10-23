"""Authenticated source lifecycle; remote commands can only narrow local consent."""

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlmodel import Session, select

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key, validate_ingest_auth
from app.models.db import (
    CredentialSource,
    SidecarDevice,
    SidecarSourceContribution,
    SidecarSourceState,
)
from app.models.source_controls import FleetSourceResponse, SourceCommandRequest, SourceSyncRequest
from app.services import audit_log, sidecar_devices, source_controls
from scripts.sidecar_pkg.source_catalog import control_catalog as catalog

router = APIRouter()


@router.post("/source-sync")
@limiter.limit("60/minute")
async def sync_sources(
    request: Request,
    x_signature: str | None = Header(None),
    x_timestamp: str | None = Header(None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    raw = await validate_ingest_auth(
        request, x_signature, x_timestamp, session=session, purpose="source-sync"
    )
    device_id = getattr(request.state, "sidecar_device_id", None)
    if device_id is None:
        raise HTTPException(403, "Source controls require enrolled device authentication")
    try:
        body = SourceSyncRequest.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(422, "Invalid source report") from None
    if len({row.source_id for row in body.sources}) != len(body.sources):
        raise HTTPException(422, "Duplicate source report")
    accepted, withdrawals = [], []
    try:
        for report in body.sources:
            descriptor = catalog().get(report.source_id)
            if descriptor is None or descriptor.provider != report.provider_id:
                continue
            row = source_controls.apply_report(session, device_id, report.model_dump())
            if row.revision == descriptor.revision:
                accepted.append(row.source_id)
            if not row.local_read:
                withdrawals.append({"source_id": row.source_id, "generation": row.generation})
        for ack in body.acknowledgments:
            source_controls.acknowledge(session, device_id, ack.id, ack.status)
        source_controls.prune_commands(session, device_id)
        session.commit()
    except ValueError:
        session.rollback()
        raise HTTPException(409, "Source report or acknowledgment rejected") from None
    return {
        "protocol": 1,
        "accepted": accepted,
        "withdrawals": withdrawals,
        "commands": source_controls.pending_commands(session, device_id),
        "controls": source_controls.current_controls(session, device_id),
    }


def _device(session: Session, sidecar_id: str) -> SidecarDevice:
    if not sidecar_devices.reserved_registry_id(sidecar_id):
        raise HTTPException(409, "Source controls require device enrollment")
    device = session.get(SidecarDevice, sidecar_id.removeprefix("device-"))
    if device is None or device.revoked:
        raise HTTPException(404, "Device unavailable")
    return device


@router.get(
    "/sidecars/{sidecar_id}/sources",
    dependencies=[Depends(require_admin_key)],
    response_model=FleetSourceResponse,
)
async def get_sources(sidecar_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    device = _device(session, sidecar_id)
    rows = session.exec(
        select(SidecarSourceState).where(SidecarSourceState.device_id == device.device_id)
    ).all()
    now = datetime.now(UTC)
    contributions = session.exec(
        select(SidecarSourceContribution).where(
            SidecarSourceContribution.device_id == device.device_id
        )
    ).all()
    health_by_id = {
        entry.source_id: entry
        for entry in session.exec(
            select(CredentialSource).where(CredentialSource.sidecar_id == sidecar_id)
        ).all()
    }
    result = []
    for row in rows:
        descriptor = catalog().get(row.source_id)
        report = json.loads(row.report_json)
        credential_ids = [
            entry.credential_source_id
            for entry in contributions
            if entry.payload_encrypted and entry.scraper_id == row.source_id
        ]
        health = [health_by_id[key] for key in credential_ids if key in health_by_id]
        successes = [entry.last_success_at for entry in health if entry.last_success_at is not None]
        collection_health = (
            "withdrawn"
            if row.generation <= row.revoked_through
            else "paused"
            if row.remote_paused
            else "healthy"
            if any(
                entry.health == "healthy" and entry.last_success_at is not None for entry in health
            )
            else "action_required"
            if any(entry.health == "auth_failed" for entry in health)
            else "untried"
        )
        result.append(
            {
                **report,
                "label": descriptor.label if descriptor else "Unsupported scraper",
                "renewal_capable": descriptor.renewal if descriptor else False,
                "collection_health": collection_health,
                "last_collection_at": max(successes).isoformat() if successes else None,
                "remote_paused": row.remote_paused,
                "revoked": row.generation <= row.revoked_through,
                "report_at": row.last_report_at.isoformat(),
                "report_stale": (now - row.last_report_at).total_seconds() > 1800,
                "announcement": bool(
                    descriptor
                    and row.dismissed_revision != descriptor.revision
                    and (row.revision != descriptor.revision or report.get("new") is True)
                ),
            }
        )
    return {
        "protocol": 1,
        "sources": result,
        "commands": source_controls.pending_commands(session, device.device_id),
    }


@router.post(
    "/sidecars/{sidecar_id}/sources/{source_id}/commands", dependencies=[Depends(require_admin_key)]
)
@limiter.limit("30/minute")
async def command_source(
    request: Request,
    sidecar_id: str,
    source_id: str,
    body: SourceCommandRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    source_controls.reserve_source_writes(session)
    device = _device(session, sidecar_id)
    try:
        command = source_controls.queue_command(session, device.device_id, source_id, body.action)
        session.commit()
    except ValueError:
        session.rollback()
        raise HTTPException(409, "Command requires an existing source and local consent") from None
    audit_log.record(
        session,
        request,
        action="sidecar.source." + body.action,
        target_id=sidecar_id,
        payload={"source_id": source_id, "command_id": command.id},
    )
    return {"id": command.id, "status": "pending"}
