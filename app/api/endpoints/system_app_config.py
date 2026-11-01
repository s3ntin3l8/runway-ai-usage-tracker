import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.models.db import (
    SystemConfig,
)
from app.services import audit_log
from app.services.project_label import WORKTREE_CONTAINERS, clean_worktree_patterns
from app.services.worktree_patterns import (
    invalidate_worktree_patterns,
    parse_stored_patterns,
)

router = APIRouter()


class _AppConfigUpdate(BaseModel):
    browser_preference: str | None = None
    default_poll_interval_seconds: int | None = None  # 0 = clear override
    # IANA tz name (e.g. "Europe/Berlin"); "" or null = clear override (use TZ env / browser detect)
    user_timezone: str | None = None
    # Sidecar update channel: stable (default), beta, or edge; "" = stable.
    sidecar_update_channel: str | None = None
    # Fleet-wide opt-in: when true, sidecars self-install available updates.
    sidecar_auto_update: bool | None = None
    # Fleet-wide default: keep-alive on for every sidecar without its own override.
    sidecar_keep_alive_default: bool | None = None
    # Extra worktree directory patterns for project labels (added to the built-ins);
    # [] clears them, null leaves them as they are.
    worktree_patterns: list[str] | None = Field(default=None, max_length=200)


class _DashboardLayout(BaseModel):
    provider_order: list[str] = Field(default_factory=list)
    card_orders: dict[str, list[str]] = Field(default_factory=dict)


@router.get("/app-config", dependencies=[Depends(require_admin_key)])
@limiter.limit("30/minute")
async def get_app_config(request: Request, session: Session = Depends(get_session)) -> dict:
    """Return global application configuration."""
    cfg = session.exec(select(SystemConfig)).first()
    return {
        "browser_preference": (cfg.browser_preference if cfg else None)
        or settings.BROWSER_PREFERENCE,
        "default_poll_interval_seconds": cfg.default_poll_interval_seconds if cfg else None,
        "user_timezone": cfg.user_timezone if cfg else None,
        "sidecar_update_channel": (cfg.sidecar_update_channel if cfg else None) or "stable",
        "sidecar_auto_update": bool(cfg.sidecar_auto_update) if cfg else False,
        "sidecar_keep_alive_default": bool(cfg.sidecar_keep_alive_default) if cfg else False,
        "worktree_patterns": list(
            parse_stored_patterns(cfg.worktree_patterns_json if cfg else None)
        ),
        "worktree_builtin_patterns": list(WORKTREE_CONTAINERS),
        "env_timezone": settings.env_timezone,
    }


@router.put("/app-config")
@limiter.limit("10/minute")
async def upsert_app_config(
    request: Request,
    body: _AppConfigUpdate,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> dict:
    """Update global application configuration."""
    cfg = session.exec(select(SystemConfig)).first()
    if cfg is None:
        cfg = SystemConfig()
        session.add(cfg)
    if body.browser_preference is not None:
        cfg.browser_preference = body.browser_preference or None
    if body.default_poll_interval_seconds is not None:
        cfg.default_poll_interval_seconds = (
            body.default_poll_interval_seconds if body.default_poll_interval_seconds > 0 else None
        )
    if body.user_timezone is not None:
        if body.user_timezone == "":
            cfg.user_timezone = None
        else:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(body.user_timezone)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid IANA timezone: {body.user_timezone!r}",
                ) from e
            cfg.user_timezone = body.user_timezone
        # resolve_user_tz() and every period-boundary-dependent response
        # (/fleet, /global-stats, /top-*, /forecast) cache their output — a
        # tz change must take effect immediately, not wait out the TTL.
        from app.core.cache import cache_clear

        cache_clear()
    if body.sidecar_update_channel is not None:
        channel = body.sidecar_update_channel.strip().lower()
        if channel in ("", "stable"):
            cfg.sidecar_update_channel = None
        elif channel == "edge":
            cfg.sidecar_update_channel = "edge"
        elif channel == "beta":
            cfg.sidecar_update_channel = "beta"
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid update channel: {body.sidecar_update_channel!r} (expected 'stable', 'beta', or 'edge')",
            )
    if body.sidecar_auto_update is not None:
        cfg.sidecar_auto_update = bool(body.sidecar_auto_update)
    if body.sidecar_keep_alive_default is not None:
        cfg.sidecar_keep_alive_default = bool(body.sidecar_keep_alive_default)
    patterns_changed = False
    if body.worktree_patterns is not None:
        try:
            patterns = clean_worktree_patterns(body.worktree_patterns)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid worktree pattern: {e}") from e
        cfg.worktree_patterns_json = json.dumps(patterns) if patterns else None
        patterns_changed = True
    session.commit()
    if patterns_changed:
        # Ingest labels events through the cached patterns; drop them so the
        # next event (and the next relabel scan) sees the new list.
        invalidate_worktree_patterns()

    # Wake poller so the new interval applies on the next tick rather than
    # waiting out the current sleep.
    from app.services.poller import poller

    poller.wake()
    return {"status": "saved"}


@router.get("/dashboard-layout", dependencies=[Depends(require_admin_key)])
@limiter.limit("30/minute")
async def get_dashboard_layout(request: Request, session: Session = Depends(get_session)) -> dict:
    """Return the persisted dashboard layout. Empty default if unset."""
    cfg = session.exec(select(SystemConfig)).first()
    raw = cfg.dashboard_layout_json if cfg else None
    if not raw:
        return {"provider_order": [], "card_orders": {}}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"provider_order": [], "card_orders": {}}
    return {
        "provider_order": parsed.get("provider_order", []) or [],
        "card_orders": parsed.get("card_orders", {}) or {},
    }


@router.put("/dashboard-layout", dependencies=[Depends(require_admin_key)])
@limiter.limit("30/minute")
async def put_dashboard_layout(
    request: Request,
    body: _DashboardLayout,
    session: Session = Depends(get_session),
) -> dict:
    """Store a new dashboard layout for an authenticated administrator."""
    cfg = session.exec(select(SystemConfig)).first()
    if cfg is None:
        cfg = SystemConfig()
        session.add(cfg)
    cfg.dashboard_layout_json = json.dumps(body.model_dump())
    session.commit()
    audit_log.record(session, request, action="dashboard.layout_update", target_id="dashboard")
    return {"status": "saved"}


__all__ = [
    "_AppConfigUpdate",
    "_DashboardLayout",
    "get_app_config",
    "get_dashboard_layout",
    "put_dashboard_layout",
    "upsert_app_config",
]
