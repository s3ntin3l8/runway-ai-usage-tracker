"""Sidecar registry, its settings, and the configuration a sidecar polls."""

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlmodel import Session, col, select

from app.api.endpoints.fleet_shared import _account_tag_hints_for_providers
from app.core.db import get_session
from app.core.login_dirs import LOGIN_DIR_KEYS, stored_login_dirs, validate_directories
from app.core.rate_limit import limiter
from app.core.security import is_loopback_bind, require_admin_key, verify_config_signature
from app.core.utils import scrub_log
from app.models.db import SidecarRegistry, SystemConfig
from app.services import audit_log, sidecar_devices
from app.services.account_identity import normalize_sidecar_id
from app.services.credential_token import issue_credential_token
from app.services.fleet_registry import (
    LOG_LEVELS,
    UPDATE_CHANNELS,
    effective_auto_update,
    effective_update_channel,
    fleet_registry,
)
from app.services.maintenance.sidecar_adopt import (
    AdoptPlan,
    AdoptResult,
    SidecarNotFoundError,
    adoptable_legacy,
    apply_adopt_sidecar,
    load_enrollment_times,
    plan_adopt_sidecar,
    replaced_by,
)
from app.services.refresh_policy import KEEP_ALIVE_PROVIDERS, parse_provider_flags

logger = logging.getLogger(__name__)

# HMAC-only sidecar route: GET /config. Keep docs/forward-auth.md's exact
# bypass route list in sync; the other fleet routes stay behind Authentik.

router = APIRouter()


class SidecarUpdateRequest(BaseModel):
    custom_name: str | None = None
    tags: list[str] | None = None


def _fleet_update_defaults(session: Session) -> tuple[str, bool]:
    """Fleet-wide (update channel, auto-update) that a sidecar follows without an override."""
    cfg = session.exec(select(SystemConfig)).first()
    return (
        (cfg.sidecar_update_channel if cfg else None) or "stable",
        bool((cfg.sidecar_auto_update if cfg else None) or False),
    )


def _source_sync_fields(
    entry: tuple[sidecar_devices.SourceSyncState, datetime | None] | None,
) -> dict[str, Any]:
    """The source-sync pair for a sidecar entry; both null for a legacy (unenrolled) sidecar,
    which has no source sync to miss."""
    state, last_at = entry or (None, None)
    return {"source_sync_state": state, "source_sync_last_at": last_at}


@router.get("/sidecars", dependencies=[Depends(require_admin_key)])
@limiter.limit("30/minute")
async def list_sidecars(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """List all registered sidecars."""
    rows = session.exec(
        select(SidecarRegistry).order_by(col(SidecarRegistry.last_seen).desc())
    ).all()
    channel, auto_update = _fleet_update_defaults(session)
    enrolled_at = load_enrollment_times(session, [row.sidecar_id for row in rows])
    adoptable = adoptable_legacy(list(rows), enrolled_at)
    replaced = replaced_by(list(rows), enrolled_at)
    sync_states = sidecar_devices.source_sync_states(session, [row.sidecar_id for row in rows])
    return {
        "sidecars": [
            {
                **fleet_registry.to_dict(row, channel, auto_update),
                "adoptable_legacy": adoptable.get(row.sidecar_id, []),
                "replaced_by": replaced.get(row.sidecar_id, []),
                **_source_sync_fields(sync_states.get(row.sidecar_id)),
            }
            for row in rows
        ]
    }


@router.get("/sidecars/{sidecar_id}", dependencies=[Depends(require_admin_key)])
@limiter.limit("30/minute")
async def get_sidecar(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Get a single sidecar by ID."""
    row = session.get(SidecarRegistry, sidecar_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    channel, auto_update = _fleet_update_defaults(session)
    sync_states = sidecar_devices.source_sync_states(session, [sidecar_id])
    return {
        **fleet_registry.to_dict(row, channel, auto_update),
        **_source_sync_fields(sync_states.get(sidecar_id)),
    }


class SidecarAdoptRequest(BaseModel):
    legacy_sidecar_id: str
    # Preview what would move; nothing is written.
    dry_run: bool = True


@router.post("/sidecars/{sidecar_id}/adopt")
@limiter.limit("10/minute")
def adopt_legacy_sidecar(
    request: Request,
    sidecar_id: str,
    body: SidecarAdoptRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Fold a legacy sidecar's history and settings into the enrolled device that replaced it.

    A plain ``def``: the retag and rebuild are blocking SQLite work, so FastAPI runs the
    handler in its threadpool instead of stalling every other request (and sidecar check-in)."""
    try:
        if body.dry_run:
            plan = plan_adopt_sidecar(
                session, legacy_id=body.legacy_sidecar_id, device_id=sidecar_id
            )
            return {"dry_run": True, **_adopt_summary(plan)}
        result = apply_adopt_sidecar(
            session, legacy_id=body.legacy_sidecar_id, device_id=sidecar_id
        )
    except SidecarNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    # The collector caches (enabled, priority) per source and reloads at most every 60 s; every
    # other writer of those values forces a resync, so a source just disabled here is not
    # collected for one more cycle.
    from app.services.collector_manager import manager

    manager._last_sync_time = 0.0
    audit_log.record(
        session,
        request,
        action="sidecar.adopt",
        target_id=sidecar_id,
        payload={"legacy_sidecar_id": body.legacy_sidecar_id, **_adopt_summary(result)},
    )
    return {"dry_run": False, **_adopt_summary(result)}


def _adopt_summary(value: AdoptPlan | AdoptResult) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "events": value.events,
        "pending_events": value.pending_events,
        "credential_tags": value.credential_tags,
        "source_prefs": value.source_prefs,
        "settings": value.settings,
    }
    if isinstance(value, AdoptResult):
        summary["rollup_pairs_rebuilt"] = value.rollup_pairs_rebuilt
        summary["windows_rebuilt"] = value.windows_rebuilt
        summary["pushes_carried"] = value.pushes_carried
        summary["errors_carried"] = value.errors_carried
    else:
        # The preview names both sides and what the device's push count becomes.
        summary["from"] = value.legacy
        summary["into"] = value.device
        summary["pushes_after"] = value.pushes_after
    return summary


@router.patch("/sidecars/{sidecar_id}")
@limiter.limit("30/minute")
async def update_sidecar(
    request: Request,
    sidecar_id: str,
    body: SidecarUpdateRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Update custom_name and/or tags for a sidecar."""
    row = fleet_registry.update_sidecar(sidecar_id, body.custom_name, body.tags, session)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    audit_log.record(
        session,
        request,
        action="sidecar.update",
        target_id=sidecar_id,
        payload={"custom_name": body.custom_name, "tags": body.tags},
    )
    channel, auto_update = _fleet_update_defaults(session)
    return fleet_registry.to_dict(row, channel, auto_update)


@router.delete("/sidecars/{sidecar_id}")
@limiter.limit("30/minute")
async def delete_sidecar(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Remove a sidecar from the registry."""
    deleted = fleet_registry.delete_sidecar(sidecar_id, session)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    audit_log.record(session, request, action="sidecar.delete", target_id=sidecar_id)
    return {"status": "deleted", "sidecar_id": sidecar_id}


def _set_sidecar_collection_enabled(
    sidecar_id: str, enabled: bool, session: Session
) -> SidecarRegistry:
    row = session.get(SidecarRegistry, sidecar_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    row.collection_enabled = enabled
    session.commit()
    session.refresh(row)
    logger.info(f"Sidecar '{scrub_log(sidecar_id)}' collection_enabled set to {enabled}")
    return row


class SidecarKeepAliveRequest(BaseModel):
    # true/false forces keep-alive on/off; null defers to the sidecar's own flag / the fleet default.
    enabled: bool | None
    # Without ``provider`` this sets the sidecar-level switch. With one (an id from
    # KEEP_ALIVE_PROVIDERS) it overrides just that login, on top of the sidecar-level setting;
    # null then clears that login's override so it follows the sidecar again.
    provider: str | None = None


class SidecarSettingsRequest(BaseModel):
    """Per-sidecar overrides: the fleet-wide update settings, and the log level.

    A field that is absent is left alone; an explicit ``null`` clears that override so the
    sidecar follows the fleet again."""

    auto_update: bool | None = None
    update_channel: str | None = None
    log_level: str | None = None
    claude_config_dirs: list[str] | None = None
    codex_home: list[str] | None = None

    @field_validator("claude_config_dirs", "codex_home", mode="before")
    @classmethod
    def directories_only(cls, value: Any) -> list[str] | None:
        return None if value is None else validate_directories(value)


@router.put("/sidecars/{sidecar_id}/settings")
@limiter.limit("10/minute")
async def set_sidecar_settings(
    request: Request,
    sidecar_id: str,
    body: SidecarSettingsRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Override the fleet's auto-update flag, update channel and/or the log level of one sidecar.

    Delivered on the sidecar's next check-in, in place of the fleet-wide values."""
    sent = body.model_fields_set
    if body.update_channel is not None and body.update_channel not in UPDATE_CHANNELS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown update channel {body.update_channel!r} "
            f"(expected one of {list(UPDATE_CHANNELS)})",
        )
    # Case-insensitive, but otherwise validated like the channel: "" is an error, not a clear.
    log_level = body.log_level.upper() if body.log_level is not None else None
    if log_level is not None and log_level not in LOG_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown log level {log_level!r} (expected one of {list(LOG_LEVELS)})",
        )
    row = session.get(SidecarRegistry, sidecar_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    old_dirs = stored_login_dirs(row.login_dirs_desired)
    new_dirs = dict(old_dirs)
    for key in LOGIN_DIR_KEYS:
        if key in sent:
            new_dirs[key] = getattr(body, key) or []
    row.login_dirs_desired = json.dumps(new_dirs)
    if "log_level" in sent:
        row.log_level_desired = log_level
    if "auto_update" in sent:
        row.auto_update_desired = body.auto_update
    if "update_channel" in sent:
        row.update_channel_desired = body.update_channel
    session.commit()
    audit_log.record(
        session,
        request,
        action="sidecar.settings",
        target_id=sidecar_id,
        payload={k: getattr(body, k) for k in sorted(sent)},
    )
    if new_dirs != old_dirs:
        fleet_registry.set_pending_trigger(sidecar_id)
    channel, fleet_auto = _fleet_update_defaults(session)
    return {
        "status": "ok",
        "sidecar_id": sidecar_id,
        "auto_update_desired": row.auto_update_desired,
        "update_channel_desired": row.update_channel_desired,
        "log_level_desired": row.log_level_desired,
        "login_dirs_desired": new_dirs,
        "effective_auto_update": effective_auto_update(row, fleet_auto),
        "effective_update_channel": effective_update_channel(row, channel),
    }


@router.put("/sidecars/{sidecar_id}/keep-alive")
@limiter.limit("10/minute")
async def set_sidecar_keep_alive(
    request: Request,
    sidecar_id: str,
    body: SidecarKeepAliveRequest,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Turn the sidecar's keep-alive (renewal of the logins in KEEP_ALIVE_PROVIDERS) on or off remotely.

    Delivered on the sidecar's next check-in; the sidecar applies it without a restart."""
    if body.provider is not None and body.provider not in KEEP_ALIVE_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown keep-alive provider {body.provider!r} "
            f"(expected one of {sorted(KEEP_ALIVE_PROVIDERS)})",
        )
    row = session.get(SidecarRegistry, sidecar_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    if body.provider is None:
        row.keep_alive_desired = body.enabled
    else:
        overrides = parse_provider_flags(row.keep_alive_desired_providers)
        if body.enabled is None:
            overrides.pop(body.provider, None)
        else:
            overrides[body.provider] = body.enabled
        row.keep_alive_desired_providers = json.dumps(overrides) if overrides else None
    session.commit()
    audit_log.record(
        session,
        request,
        action="sidecar.keep_alive",
        target_id=sidecar_id,
        payload={"enabled": body.enabled, **({"provider": body.provider} if body.provider else {})},
    )
    return {
        "status": "ok",
        "sidecar_id": sidecar_id,
        "keep_alive_desired": row.keep_alive_desired,
        "keep_alive_desired_providers": parse_provider_flags(row.keep_alive_desired_providers),
    }


@router.post("/sidecars/{sidecar_id}/pause")
@limiter.limit("10/minute")
async def pause_sidecar(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Pause collection on the named sidecar. The sidecar continues to check
    in but receives no poll instructions until resumed."""
    _set_sidecar_collection_enabled(sidecar_id, False, session)
    audit_log.record(session, request, action="sidecar.pause", target_id=sidecar_id)
    return {"status": "paused", "sidecar_id": sidecar_id, "collection_enabled": False}


@router.post("/sidecars/{sidecar_id}/resume")
@limiter.limit("10/minute")
async def resume_sidecar(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Resume collection on the named sidecar."""
    _set_sidecar_collection_enabled(sidecar_id, True, session)
    audit_log.record(session, request, action="sidecar.resume", target_id=sidecar_id)
    return {"status": "resumed", "sidecar_id": sidecar_id, "collection_enabled": True}


@router.post("/sidecars/{sidecar_id}/update")
@limiter.limit("10/minute")
async def update_sidecar_now(
    request: Request,
    sidecar_id: str,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Push a one-shot self-update to the named sidecar. It self-installs on its
    next heartbeat (independent of the auto-update toggle); a no-op on non-frozen
    or Docker sidecars."""
    row = fleet_registry.set_pending_update(sidecar_id, session)
    if not row:
        raise HTTPException(status_code=404, detail=f"Sidecar '{sidecar_id}' not found")
    audit_log.record(session, request, action="sidecar.update_now", target_id=sidecar_id)
    return {"status": "update_queued", "sidecar_id": sidecar_id}


@router.get("/config")
@limiter.limit("60/minute")
async def get_fleet_config(
    request: Request,
    sidecar_id: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Retrieve active collection configuration for sidecars.

    This endpoint does not require the admin key (as sidecars do not have it)
    but relies on rate limiting. Account ids, ``account_tag_hints`` and
    ``credential_token``s are only returned to callers that sign the request
    with the ingest key (``X-Timestamp`` / ``X-Signature``, see
    ``verify_config_signature``) or when the server is bound to loopback;
    anyone else gets only the enabled/strategies view.

    Optional ``?sidecar_id=<hostname>`` (#319) identifies the requesting
    sidecar so ``account_tag_hints`` can be scoped to it: machine-scoped
    credential tags only ship to their host, and in a multi-host
    deployment the single-account auto-hint only reaches sidecars that
    reported the credential. Old sidecar binaries omit the parameter —
    they receive deployment-wide tags only, and no auto-hints while 2+
    sidecars are live (the safe pre-#319 behavior).

    The response carries two parallel shapes for backward compatibility:

    - ``enabled`` + ``strategies`` (legacy top-level): OR-merged across accounts
      so today's single-sidecar / single-account consumer keeps working unchanged.
    - ``accounts`` (new): per-account ``{account_id, enabled, strategies,
      credential_token}`` so a future per-account sidecar (Issue #272) can
      iterate without guessing.

    Tokens are short-lived (TTL = ``CREDENTIAL_TOKEN_TTL_SECONDS``, default
    1 hour) and scoped to a single ``(provider_id, account_id)`` pair. The
    companion redeem endpoint is the next PR — this one ships the wire
    format and the issuer so a sidecar build can target a stable token
    format without playing catch-up.
    """
    from app.models.db import ProviderConfig

    # Normalize, and collapse an empty ``?sidecar_id=`` to ``None`` so the
    # "exactly one live sidecar" fallback for unidentified callers applies.
    sidecar_id = (normalize_sidecar_id(sidecar_id) if sidecar_id else "") or None
    # Decided up front (verification consumes the single-use signature):
    # untrusted callers get a redacted view, so nothing identity-bearing —
    # including credential tokens — is even computed for them.
    trusted = verify_config_signature(request, session=session) or (
        is_loopback_bind() and request.headers.get("X-Runway-Dev-Remote") != "1"
    )

    from app.services import sidecar_devices

    sidecar_id = sidecar_devices.authenticated_registry_id(request, sidecar_id)

    rows = session.exec(select(ProviderConfig)).all()

    # Late import — survives ``importlib.reload(app.core.config)`` from
    # ``tests/unit/test_config.py``'s reload test. ``from app.core.config
    # import settings`` at module top would bind the pre-reload settings
    # instance, so a dotted-path ``monkeypatch.setattr`` in the test
    # fixture would patch the live module while this endpoint reads the
    # stale bound name (PR #290 round-2 review, Hermes body suggestion
    # #1, issue #291).
    from app.core.config import settings as _settings

    config: dict[str, dict] = {"providers": {}}
    token_ttl = max(60, int(_settings.CREDENTIAL_TOKEN_TTL_SECONDS))
    # Tokens are only issued when ingest is configured — the redeem endpoint
    # requires INGEST_API_KEY to authenticate, so issuing tokens without it
    # would just produce tokens no one can redeem.
    can_issue_tokens = (
        trusted
        and bool(_settings.INGEST_API_KEY)
        and not _settings.INGEST_API_KEY_IS_INSECURE_DEFAULT
    )

    for row in rows:
        is_first_for_provider = row.provider_id not in config["providers"]
        provider_cfg = config["providers"].setdefault(
            row.provider_id,
            {
                "enabled": row.enabled,
                # Seed strategies from the first row even when that row is
                # disabled — preserves the original endpoint's behavior so a
                # single-disabled-row-with-strategies still surfaces those
                # strategies (multi-account hardening only changes the
                # post-first-row branch).
                "strategies": row.strategies,
                # Per-account breakdown (multi-account). One entry per row.
                # Existing sidecars ignore this; new per-account sidecars
                # iterate the list. See Issue #272.
                "accounts": [],
            },
        )
        # Issue a per-account credential token. Tokens are issued only for
        # accounts that actually have a credential field set (api_key,
        # session_cookie, oai_sc_cookie) — there's no point handing a
        # sidecar a token for an empty row.
        has_credential = bool(
            row.api_key_encrypted or row.session_cookie_encrypted or row.oai_sc_cookie_encrypted
        )
        account_entry: dict[str, Any] = {
            "account_id": row.account_id,
            "enabled": row.enabled,
            "strategies": row.strategies,
        }
        if can_issue_tokens and has_credential and row.enabled:
            try:
                account_entry["credential_token"] = issue_credential_token(
                    _settings.INGEST_API_KEY,
                    provider_id=row.provider_id,
                    account_id=row.account_id,
                    ttl_seconds=token_ttl,
                )
            except Exception as exc:  # noqa: BLE001 — token issuance must never fail /config
                # Defensive: if token issuance raises (e.g. INGEST_API_KEY races
                # with a config reload), don't break /fleet/config — just omit
                # the token. The sidecar's existing local-credential path keeps
                # working until the next heartbeat can retry.
                logger.warning(
                    "Failed to issue credential token for %s/%s: %s",
                    scrub_log(row.provider_id),
                    scrub_log(row.account_id),
                    exc,
                )
        provider_cfg["accounts"].append(account_entry)
        if is_first_for_provider:
            # The setdefault above already seeded `enabled` and `strategies`
            # from this row — nothing else to do for the first row.
            continue
        # OR-merge enabled across accounts: the sidecar collects the provider
        # if *any* account has it enabled. Existing single-account consumers
        # see identical behavior.
        if row.enabled:
            provider_cfg["enabled"] = True
        # Last-writer-wins for the legacy top-level `strategies` field —
        # subsequent rows only overwrite when enabled (matches the original
        # endpoint's behavior; the per-account `accounts` array above
        # exposes strategies without ambiguity).
        if row.strategies and row.enabled:
            provider_cfg["strategies"] = row.strategies

    from app.services.collector_manager import collector_manager

    # Ensure all registered providers have a default entry if not in DB
    for p_id in collector_manager.collector_registry:
        if p_id not in config["providers"]:
            config["providers"][p_id] = {
                "enabled": True,
                "strategies": None,
                "accounts": [],
            }

    # Tag hints for every provider the sidecar might poll — powers the
    # silent-listener fall-through path (see PR #288). Sidecar uses these
    # when local credential discovery doesn't surface an account_id; the
    # hint maps the credential's `origin_descriptor` to a known
    # provider_configs.account_id. Scoped to ?sidecar_id= when given (#319).
    account_tag_hints = _account_tag_hints_for_providers(
        session,
        list(config["providers"].keys()),
        sidecar_id=sidecar_id,
        include_parked=False,
    )

    if not trusted:
        # Unsigned caller on a network-reachable server: this endpoint is
        # unauthenticated, so strip everything that identifies accounts —
        # account ids / emails, operator tag hints and credential tokens.
        # Sidecars sign the request with the ingest key and get the full
        # view; enabled/strategies stay available for older binaries.
        for provider_cfg in config["providers"].values():
            provider_cfg["accounts"] = []
        account_tag_hints = {}

    return {
        "status": "ok",
        "config": config,
        "account_tag_hints": account_tag_hints,
    }


__all__ = [
    "SidecarAdoptRequest",
    "SidecarKeepAliveRequest",
    "SidecarSettingsRequest",
    "SidecarUpdateRequest",
    "_adopt_summary",
    "_fleet_update_defaults",
    "_set_sidecar_collection_enabled",
    "_source_sync_fields",
    "adopt_legacy_sidecar",
    "delete_sidecar",
    "get_fleet_config",
    "get_sidecar",
    "list_sidecars",
    "pause_sidecar",
    "resume_sidecar",
    "set_sidecar_keep_alive",
    "set_sidecar_settings",
    "update_sidecar",
    "update_sidecar_now",
]
