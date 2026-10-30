"""The shared single-flight gate and read-only source authority for the Claude cloud diagnostics.

The cloud probe, the chat fixture, the cloud verification services and the collector all
contend for one gate and all read one authority view of a cached source. Those pieces live
here, in public names, so one diagnostic never reaches into another's privates.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, select

from app.core.db import engine
from app.core.encryption import DecryptionError, encryption_service
from app.models.db import CredentialSource, ProviderConfig, SidecarDevice, SidecarSourceContribution
from app.services.source_controls import allowed
from scripts.sidecar_pkg.source_catalog import source_for_origin

# One gate for the whole Claude cloud diagnostic cluster: at most one diagnostic is in
# flight per process, and a caller that finds it held reports "busy" without touching
# the network, the database or the token cache. It is created once here, never per call,
# and stays a module attribute so a test can bind a fresh lock to its own event loop.
gate = asyncio.Lock()

# The sidecar id the collector ingests under. It is a server-side source, not a host: a sidecar that
# claims it is refused (`SERVER_SIDECAR_IDS` in `sidecar_devices.py`), so no device can merge into it.
CLOUD_SIDECAR_ID = "anthropic-cloud"


class ProbeError(Exception):
    """Only internal, fixed outcome categories cross this boundary."""


def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject a JSON object with a duplicate key instead of silently keeping one."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError("unsupported_contract")
        result[key] = value
    return result


def strict_constant(value: str) -> None:
    """Reject the non-standard JSON constants instead of decoding them."""
    raise ProbeError("unsupported_contract")


def strict_uuid(value: object) -> str | None:
    """The canonical form of a UUID, or None when the value is not one."""
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def authority_read(
    account: str, source_id: str, tokens: dict[str, str], candidate: dict[str, Any]
) -> tuple[str | None, tuple[Any, ...]]:
    """Only bounded, read-only SQLite work runs in a cancellable worker thread."""
    database = engine.url.database
    if not database:
        raise ProbeError("source_unavailable")
    uri = "file:" + quote(str(Path(database).resolve())) + "?mode=ro"
    read_engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(uri, uri=True, timeout=0.25),
        poolclass=NullPool,
    )
    try:
        return _check_authority(read_engine, account, source_id, tokens, candidate)
    finally:
        read_engine.dispose()


def _check_authority(
    read_engine: Any,
    account: str,
    source_id: str,
    tokens: dict[str, str],
    candidate: dict[str, Any],
) -> tuple[str | None, tuple[Any, ...]]:
    with Session(read_engine) as session:
        source = session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == "anthropic",
                CredentialSource.account_id == account,
                CredentialSource.source_id == source_id,
            )
        ).first()
        config = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "anthropic", ProviderConfig.account_id == account
            )
        ).first()
        if (
            source is None
            or not source.enabled
            or (config and (config.archived or not config.enabled))
        ):
            raise ProbeError("source_ineligible")
        origin = candidate.get("credential_origin") or source.credential_origin or ""
        if source.credential_origin and origin != source.credential_origin:
            raise ProbeError("source_ineligible")
        if origin.startswith("keychain:") and source_for_origin("anthropic", origin) is None:
            raise ProbeError("unsupported_source")
        scraper = source_for_origin("anthropic", origin)
        if scraper is not None and scraper != "anthropic.claude-code":
            raise ProbeError("unsupported_source")
        contributions = session.exec(
            select(SidecarSourceContribution).where(
                SidecarSourceContribution.provider_id == "anthropic",
                SidecarSourceContribution.credential_source_id == source_id,
            )
        ).all()
        if len(contributions) > 1:
            raise ProbeError("source_ineligible")
        contribution = contributions[0] if contributions else None
        durable = candidate.get("durable_source") is True
        authority: tuple[Any, ...] = (source.source_type, origin)
        if durable or contribution is not None or scraper is not None:
            device_id = candidate.get("device_id")
            generation = candidate.get("consent_generation")
            device = session.get(SidecarDevice, device_id) if isinstance(device_id, str) else None
            if (
                not durable
                or contribution is None
                or device is None
                or device.revoked
                or type(generation) is not int
                or contribution.device_id != device_id
                or contribution.account_id != account
                or contribution.origin != origin
                or contribution.generation != generation
                or contribution.scraper_id != scraper
                or contribution.payload_encrypted is None
                or (scraper is not None and not allowed(session, device_id, scraper, generation))
            ):
                raise ProbeError("source_ineligible")
            try:
                payload = encryption_service.decrypt_json(contribution.payload_encrypted)
            except DecryptionError:
                raise ProbeError("source_ineligible") from None
            if not isinstance(payload, dict) or payload.get("tokens") != tokens:
                raise ProbeError("source_changed")
            authority += (device_id, scraper, generation)
        code_origin = (
            scraper == "anthropic.claude-code"
            or origin == "env:CLAUDE_CODE_OAUTH_TOKEN"
            or (
                origin.startswith(("file:", "path:"))
                and origin.endswith(("/.credentials.json", "/oauth_creds.json"))
            )
        )
        # Egress marks borrowed Code credentials read-only too: this forbids refresh,
        # not GETs. Require Code provenance rather than treating it as Desktop identity.
        if not code_origin and (
            source.source_type != "config" or tokens.get("oauth_read_only") == "1"
        ):
            raise ProbeError("unsupported_source")
    return config.claude_organization_id if config else None, authority
