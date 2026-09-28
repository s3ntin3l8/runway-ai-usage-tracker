"""Fires Discord/Slack webhooks when a credential's Token Health status
turns bad (expired or invalid) and re-arms once it recovers.

Token Health (`app.services.token_health`) is computed fresh on every call
and never persisted; its ``invalid`` status comes from the in-memory
``auth_failures`` registry, which *any* successful collect clears. A single
healthy observation is therefore not proof the credential is actually
fixed — see ``_rearm_window_seconds`` / ``WebhookCredentialAlert.healthy_since``.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.db import ProviderConfig, SystemConfig, WebhookConfig, WebhookCredentialAlert
from app.services.account_identity import resolve_account_id
from app.services.token_health import (
    _build_accounts_by_provider,
    _underlying_account,
    is_flagged,
    token_health_service,
)
from app.services.webhooks import (
    _credential_discord_payload,
    _credential_slack_payload,
    _post_payload,
    _scope_matches,
)

logger = logging.getLogger(__name__)

# A single healthy Token Health observation must not re-arm an alert (see
# module docstring) — hold healthy for at least two poll cycles before
# clearing. BackgroundPoller's own default interval, used when no
# system_config override is set.
_DEFAULT_POLL_INTERVAL_SECONDS = 900
# Absolute floor regardless of how short the configured poll interval is.
_REARM_FLOOR_SECONDS = 1800

# The server is guaranteed single-process (see app/main.py's `uvicorn.run`
# comment), so every overlapping `poll_now()` call — a scheduled tick racing
# a `POST /force-collect` — runs on the same event loop. This lock fully
# serializes `check_credential_alerts` cycles, closing the read-then-write
# race on the dedup row described in `_commit_step`'s docstring.
_check_lock = asyncio.Lock()


def _rearm_window_seconds(session: Session) -> int:
    """At least two poll cycles, so one good tick can't re-arm by itself.

    Mirrors `BackgroundPoller._compute_effective_interval`'s global-default
    lookup — a per-provider `poll_interval_seconds` override only shortens
    that provider's own re-fetch cadence, not the cycle `poll_now()` (and
    thus this check) runs on, so it isn't part of this estimate.
    """
    sys_cfg = session.exec(select(SystemConfig)).first()
    interval = (
        sys_cfg.default_poll_interval_seconds
        if sys_cfg and sys_cfg.default_poll_interval_seconds
        else _DEFAULT_POLL_INTERVAL_SECONDS
    )
    return max(_REARM_FLOOR_SECONDS, 2 * interval)


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; UTCDateTime's coercion only runs on
    Pydantic validation, not on ORM-hydrated reads (see app/models/_datetime.py)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _worst_status(a: str | None, b: str) -> str:
    if a is None:
        return b
    return "invalid" if "invalid" in (a, b) else "expired"


def _commit_step(session: Session, context: str) -> None:
    """Commit one webhook's dedup-row change immediately, rather than batching
    the whole cycle into one commit.

    `_check_lock` now serializes overlapping `check_credential_alerts` cycles
    (force-collect racing a scheduled tick), so the same-process race that
    used to let two cycles insert the same `(webhook_id, provider_id,
    account_id)` dedup row can no longer happen — the `IntegrityError` catch
    below is defense-in-depth, not the primary guard. Per-step commits are
    still worth keeping regardless: they keep a losing write's blast radius
    to *this* row rather than the whole cycle, and they keep any single write
    from staying open across the next iteration's `httpx` await (SQLAlchemy
    autoflush would otherwise upgrade the connection to a held write lock for
    the duration of that request).
    """
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        logger.debug(f"Credential alert dedup race, skipping this tick: {context}")


def _is_alert_bad(row: dict[str, Any], accounts_by_provider: dict[str, set[str]]) -> bool:
    """A row is bad enough to alert on: rejected, or expired with no way to
    recover on its own.

    A rollable (refresh_token-bearing) expired row is normally a benign
    OAuth rollover in progress: `TokenAutoRefresher` re-rolls it before it
    goes stale. But `TokenAutoRefresher.refresh_due` only logs a refresh
    failure — it never flags `auth_failures` — and `_apply_invalid` only
    promotes `valid`/`unknown` rows, never an already-`expired` one. So a
    permanently dead credential (a revoked refresh_token) would otherwise
    sit at `expired` forever without ever alerting. Fall back to `is_flagged`
    directly: a live collection attempt against this account failing with
    401/403 sets the same `auth_failures` flag `_apply_invalid` reads,
    regardless of what it did to this row's status.
    """
    if row["status"] == "invalid":
        return True
    if row["status"] != "expired" or row.get("redundant"):
        return False
    rollable = "refresh_token" in row.get("token_types", [])
    if not rollable:
        return True
    return is_flagged(row, accounts_by_provider)


async def check_credential_alerts(session: Session) -> None:
    """Evaluate Token Health and fire/re-arm credential-health webhooks.

    Holds `_check_lock` for the entire body — the dedup-row read (`alert =
    session.exec(...)`) and the webhook POST + row write it gates must be
    atomic with respect to any other overlapping cycle, or two concurrent
    cycles can both observe `alert is None` and both deliver.
    """
    async with _check_lock:
        configs = session.exec(
            select(WebhookConfig).where(
                WebhookConfig.active == True,  # noqa: E712
                WebhookConfig.credential_alerts == True,  # noqa: E712
            )
        ).all()
        if not configs:
            return  # nothing opted in — skip the Token Health scan entirely

        scope_labels: dict[tuple[str, str | None], str | None] = {
            (r.provider_id, r.account_id): r.account_label
            for r in session.exec(select(ProviderConfig)).all()
        }

        rows = await token_health_service.get_health()
        if not rows:
            return
        accounts_by_provider = _build_accounts_by_provider(rows)

        # (provider, resolved_account_id) -> classification state built up
        # across every Token Health row that maps to that identity.
        keys: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            provider = row["provider"]
            underlying = _underlying_account(row["account_id"])
            # Synthetic rows the service builds itself (`server`, in
            # particular) carry no account_label of their own — fall back to
            # the same provider_configs label `scope_labels` uses, so a
            # `default`-scoped webhook whose account has an email label still
            # matches a server-discovered credential for that account, the
            # same way it already matches cards for it (see
            # `_scope_matches`'s docstring).
            label = row.get("account_label") or scope_labels.get((provider, underlying))
            resolved = resolve_account_id(provider, underlying, label)
            state = keys.setdefault(
                (provider, resolved),
                {"bad": False, "healthy": False, "status": None, "detail": None},
            )
            if _is_alert_bad(row, accounts_by_provider):
                state["bad"] = True
                state["status"] = _worst_status(state["status"], row["status"])
                if state["detail"] is None or row["status"] == "invalid":
                    state["detail"] = row
            # Deliberately broader than token_health's own redundancy math,
            # which excludes "_assumed" (config/env, no real expiry) rows as
            # evidence — here, any non-bad valid/expiring sibling is enough
            # to hold off an alert, since the goal is "don't page while
            # collection still works."
            if row["status"] in ("valid", "expiring"):
                state["healthy"] = True

        if not keys:
            return

        now = datetime.now(UTC)
        rearm_cutoff = now - timedelta(seconds=_rearm_window_seconds(session))

        async with httpx.AsyncClient(timeout=5.0) as client:
            for (provider, account_id), state in keys.items():
                # bad+healthy both true (a working credential alongside a
                # stale one) and neither true (only unknown/redundant/
                # rollable rows) are both left alone: a healthy sibling means
                # collection still works for this account, so a stale/
                # rejected credential beside it isn't blocking anything and
                # shouldn't page.
                if state["bad"] and not state["healthy"]:
                    classification = "bad"
                elif state["healthy"] and not state["bad"]:
                    classification = "healthy"
                else:
                    continue

                for config in configs:
                    scope_label = scope_labels.get((config.provider_id, config.account_id))
                    if not _scope_matches(config, provider, account_id, scope_label):
                        continue

                    alert = session.exec(
                        select(WebhookCredentialAlert).where(
                            WebhookCredentialAlert.webhook_id == config.id,
                            WebhookCredentialAlert.provider_id == provider,
                            WebhookCredentialAlert.account_id == account_id,
                        )
                    ).first()

                    context = f"webhook {config.id} ({provider}/{account_id})"

                    if classification == "healthy":
                        if alert is None:
                            continue  # no active alert to re-arm
                        if alert.healthy_since is None:
                            alert.healthy_since = now
                            session.add(alert)
                            _commit_step(session, context)
                        elif _as_utc(alert.healthy_since) <= rearm_cutoff:
                            session.delete(alert)
                            _commit_step(session, context)
                        continue

                    # classification == "bad"
                    if alert is not None:
                        changed = False
                        if alert.healthy_since is not None:
                            alert.healthy_since = None
                            changed = True
                        if state["status"] == "invalid" and alert.status != "invalid":
                            alert.status = "invalid"
                            changed = True
                        if changed:
                            session.add(alert)
                            _commit_step(session, context)
                        continue  # already alerted for this bad episode

                    detail = state["detail"] or {}
                    payload_kwargs = {
                        "provider_id": provider,
                        "account_label": detail.get("account_label"),
                        "account_id": account_id,
                        "status": state["status"],
                        "source_name": detail.get("source_name"),
                    }
                    payload = (
                        _credential_discord_payload(**payload_kwargs)
                        if config.channel == "discord"
                        else _credential_slack_payload(**payload_kwargs)
                    )
                    try:
                        await _post_payload(client, config, payload)
                    except Exception as e:
                        logger.error(f"Credential alert delivery failed for {context}: {e}")
                        continue

                    session.add(
                        WebhookCredentialAlert(
                            webhook_id=config.id,
                            provider_id=provider,
                            account_id=account_id,
                            status=state["status"],
                            fired_at=now,
                        )
                    )
                    _commit_step(session, context)
                    logger.info(f"Credential alert fired: {context} ({state['status']})")
