"""Sidecar pairing: the one-time code an admin redeems on the host."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.core.utils import scrub_log
from app.models._datetime import iso_utc
from app.services import audit_log, pairing
from app.services.account_identity import normalize_sidecar_id

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Sidecar pairing (runway-sidecar://pair deep links) — see app/services/pairing.py
# ---------------------------------------------------------------------------


class PairingCodeRequest(BaseModel):
    # The URL the admin's browser is using for the dashboard — the most
    # reliable "how do machines reach this server" signal behind proxies.
    # PUBLIC_URL, when set, always wins.
    server_url: str | None = None


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: str
    server_url: str
    deep_link: str


class PairRequest(BaseModel):
    code: str
    hostname: str | None = None
    device_protocol: Literal[1] | None = None
    platform: Literal["Darwin", "Linux", "Windows"] | None = None


class PairResponse(BaseModel):
    api_url: str
    api_key: str
    device_id: str | None = None
    device_key: str | None = None
    device_api_url: str | None = None
    source_protocol: int | None = None


def _ingest_disabled() -> bool:
    from app.core.config import settings as _settings

    return not _settings.INGEST_API_KEY or _settings.INGEST_API_KEY_IS_INSECURE_DEFAULT


@router.post("/pairing-codes", response_model=PairingCodeResponse)
@limiter.limit("10/minute")
async def create_pairing_code(
    request: Request,
    body: PairingCodeRequest | None = None,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> PairingCodeResponse:
    """Mint a one-time code (+ deep link) that lets a new sidecar configure itself."""
    from app.core.config import settings as _settings

    if _ingest_disabled():
        raise HTTPException(
            status_code=503,
            detail="Set a custom INGEST_API_KEY before pairing sidecars (ingest is disabled).",
        )
    candidates = [_settings.PUBLIC_URL, body.server_url if body else None, str(request.base_url)]
    server_url = next(
        (u for u in (pairing.valid_server_url(c or "") for c in candidates) if u), None
    )
    if not server_url:
        raise HTTPException(status_code=400, detail="Could not determine the server URL")
    code, expires_at = pairing.create_code(
        session,
        server_url=server_url,
        ttl_seconds=_settings.PAIRING_CODE_TTL_SECONDS,
        created_by=audit_log.resolve_actor(request),
    )
    # The code itself never goes into the audit trail.
    audit_log.record(
        session,
        request,
        action="sidecar.pairing_code.create",
        target_id=None,
        payload={"server_url": server_url, "expires_at": iso_utc(expires_at)},
    )
    return PairingCodeResponse(
        code=code,
        expires_at=iso_utc(expires_at) or "",
        server_url=server_url,
        deep_link=pairing.deep_link(server_url, code),
    )


@router.post("/pair", response_model=PairResponse, response_model_exclude_none=True)
@limiter.limit("10/minute")
async def redeem_pairing_code(
    request: Request,
    body: PairRequest,
    session: Session = Depends(get_session),
) -> PairResponse:
    """Exchange a one-time pairing code for the sidecar's ``api_url`` + ingest key.

    Unauthenticated by design (the code *is* the credential): single-use,
    short-lived, rate-limited per IP, and every outcome is audited.
    """
    from app.core.config import settings as _settings

    if _ingest_disabled():
        raise HTTPException(status_code=503, detail="Sidecar ingest is disabled on this server")
    hostname = normalize_sidecar_id(body.hostname) if body.hostname else None
    request.state.admin_actor = "pairing-code"
    from app.services import sidecar_devices

    device_fields: dict[str, Any] = {}
    try:
        row = pairing.redeem(session, body.code, hostname=hostname, commit=False)
        if body.device_protocol == 1 and sidecar_devices.encryption_service.is_enabled:
            device, secret = sidecar_devices.enroll(session, hostname, platform=body.platform)
            device_fields = {
                "device_id": device.device_id,
                "device_key": secret,
                "device_api_url": row.server_url,
                "source_protocol": 1,
            }
        session.commit()
    except pairing.PairingError:
        audit_log.record(session, request, action="sidecar.pair.rejected", target_id=hostname)
        # One vague answer for unknown / expired / reused codes (no oracle).
        raise HTTPException(status_code=400, detail="Invalid or expired pairing code") from None
    audit_log.record(
        session,
        request,
        action="sidecar.pair",
        target_id=hostname,
        payload={"server_url": row.server_url},
    )
    logger.info("Sidecar %s paired via one-time code", scrub_log(hostname or "?"))
    return PairResponse(api_url=row.server_url, api_key=_settings.INGEST_API_KEY, **device_fields)


__all__ = [
    "PairRequest",
    "PairResponse",
    "PairingCodeRequest",
    "PairingCodeResponse",
    "_ingest_disabled",
    "create_pairing_code",
    "redeem_pairing_code",
]
