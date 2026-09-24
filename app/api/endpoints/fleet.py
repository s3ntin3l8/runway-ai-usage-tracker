import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session, col, select

from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import (
    is_loopback_bind,
    require_admin_key,
    validate_ingest_auth,
    verify_config_signature,
)
from app.core.utils import scrub_log
from app.models._datetime import iso_utc
from app.models.db import (
    LatestUsage,
    ProviderConfig,
    SidecarRegistry,
    SystemConfig,
)
from app.models.schemas import IngestRequest
from app.services import audit_log, pairing
from app.services.account_identity import normalize_sidecar_id, resolve_account_id
from app.services.accumulator import prune_stale_latest_usage, upsert_latest_usage
from app.services.credential_tags import (
    CredentialTagRepo,
    PendingCredentialTagRepo,
    live_sidecar_ids,
)
from app.services.credential_token import issue_credential_token
from app.services.fleet_registry import fleet_registry
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)
router = APIRouter()


class SidecarUpdateRequest(BaseModel):
    custom_name: str | None = None
    tags: list[str] | None = None


@router.post("/ingest")
@limiter.limit("600/minute")
async def ingest_metrics(  # noqa: PLR0915 — known-debt: end-to-end ingest entrypoint, refactor tracked separately
    request: Request,
    x_signature: str = Header(None, alias="X-Signature"),
    x_timestamp: str = Header(None, alias="X-Timestamp"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """
    Ingest metrics from sidecar with HMAC-SHA256 signature verification.

    Headers required:
    - X-Signature: HMAC-SHA256(secret, timestamp + body)
    - X-Timestamp: Unix timestamp (within 5 minutes)

    Rate limit: 600 requests / minute per source IP. Sidecars batch up to
    1000 events per push at a 15-min cadence (spec §7.3), so even a fleet
    of 100 sidecars stays well under 5 req/min. The limit's there to keep
    a flooding attacker from saturating the HMAC + Pydantic parse path,
    not to throttle legitimate operators.
    """
    # Validate HMAC + window + body cap via the shared ingest-auth helper
    # (any change to the scheme is one place). #288+ deduplicates against
    # the same helper used by /fleet/credentials/manifest.
    body_bytes = await validate_ingest_auth(request, x_signature, x_timestamp)

    # 4. Parse request
    try:
        payload = IngestRequest.model_validate_json(body_bytes)
    except Exception as e:
        logger.error(f"Failed to parse ingest payload: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid payload: {str(e)}")

    # Normalize the originating sidecar id once, here at the chokepoint, so the
    # registry upsert, the per-card propagation, and every ingested event all key
    # off the same stable id. Collapses a host that flips between its FQDN and
    # `.local`/short name (e.g. macbook.local ⇄ Macbook.in.example.de) onto one
    # registry entry regardless of the sidecar binary version.
    if payload.sidecar_id:
        payload.sidecar_id = normalize_sidecar_id(payload.sidecar_id)

    tokens_to_store = []
    local_cards = []

    for card in payload.metrics:
        # Check if this is a token-only card (should NOT be displayed)
        is_token_only = card.remaining == "Token" and card.unit in ("oauth", "api_key", "cookie")

        if is_token_only:
            # Extract provider/account identifiers: prefer top-level fields, fall back to metadata
            provider_id = card.provider_id or (
                card.metadata.get("provider_id") if card.metadata else None
            )
            if provider_id:
                acc_id = card.account_id or (
                    card.metadata.get("account_id") if card.metadata else None
                )
                acc_label = card.account_label or (
                    card.metadata.get("account_label") if card.metadata else None
                )

                provider_tokens = {}
                if card.metadata:
                    for key, val in card.metadata.items():
                        # Store tokens but skip the provider/account identifiers
                        if key not in (
                            "provider_id",
                            "account_id",
                            "account_label",
                        ) and (
                            key
                            in (
                                "oauth_token",
                                "refresh_token",
                                "api_key",
                                "id_token",
                                "expiry_date",
                            )
                            or key.startswith("cookie_")
                        ):
                            provider_tokens[key] = val

                # Older sidecars combined browser cookies and CLI OAuth in
                # one card, then assigned the CLI account to the whole card.
                # That identity does not prove the cookie owner. Keep the
                # independently identified CLI family and discard the cookie
                # fields from this legacy mixed payload.
                if any(key.startswith("cookie_") for key in provider_tokens) and any(
                    key in provider_tokens
                    for key in ("oauth_token", "refresh_token", "id_token", "api_key")
                ):
                    provider_tokens = {
                        key: value
                        for key, value in provider_tokens.items()
                        if not key.startswith("cookie_")
                    }

                if provider_tokens:
                    tokens_to_store.append((provider_id, provider_tokens, acc_id, acc_label))
                    logger.debug(
                        f"Extracted {list(provider_tokens.keys())} for {provider_id} account {acc_id or 'auto'} from {payload.provider}"
                    )
            continue

        # Propagate sidecar_id from the request to each card (if not already set)
        if payload.sidecar_id and not card.sidecar_id:
            card.sidecar_id = payload.sidecar_id

        # Enforce sidecar-enrichment-only rule: quota (percent / pct_used) cards
        # must come from server-side API collectors, not sidecars.  The LSP
        # data_source is the one explicit exception (it legitimately emits
        # percentage-quota from local tooling state).
        is_sidecar_quota = (card.unit_type == "percent" or card.pct_used is not None) and (
            card.data_source or "local"
        ) != "lsp"
        if is_sidecar_quota:
            logger.warning(
                "Ingest: dropping sidecar quota card for %r "
                "(provider=%s, data_source=%s) — sidecar enrichment must not "
                "emit quota; use the server-side API collector.",
                card.service_name or "unknown",
                card.provider_id or payload.provider or "unknown",
                card.data_source or "local",
            )
            continue

        # Keep actual data cards
        local_cards.append(card)

    # Register/update sidecar in persistent fleet registry (non-fatal)
    if payload.sidecar_id:
        source_ip = request.client.host if request.client else "unknown"
        try:
            fleet_registry.upsert_sidecar(
                payload.sidecar_id,
                source_ip,
                session,
                sidecar_version=payload.sidecar_version,
                os_platform=payload.os_platform,
                self_update_capable=payload.self_update_capable,
                collection_errors=payload.collection_errors,
                last_log_lines=payload.last_log_lines or [],
                identity_sources=payload.identity_sources,
            )
        except Exception as _e:
            logger.warning(f"Fleet registry upsert failed for '{payload.sidecar_id}': {_e}")

    # Store tokens in cache for each identified account
    tokens_received_count = 0
    for p_id, p_tokens, a_id, a_name in tokens_to_store:
        actual_acc_id = await token_cache.store(
            p_id, p_tokens, a_id, a_name, source=payload.sidecar_id
        )
        tokens_received_count += len(p_tokens)
        logger.info(
            f"Received {len(p_tokens)} tokens for {p_id} account {actual_acc_id} from {payload.provider}"
        )

    # Store local data cards directly into LatestUsage (unified with server-scraped cards)
    if local_cards:
        # Track (provider_id, canonical_account_id) → set of
        # (window_type, variant, model_id) for the prune step.
        batch_keys: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
        for card in local_cards:
            card_dict = card.model_dump(exclude_none=True)
            upsert_latest_usage(
                session,
                card_dict,
                sidecar_id_override=card.sidecar_id or payload.sidecar_id or "local",
            )
            if card.provider_id and card.account_id:
                canonical_aid = resolve_account_id(
                    card.provider_id, card.account_id, card.account_label
                )
                batch_keys.setdefault((card.provider_id, canonical_aid), set()).add(
                    (
                        card.window_type or "",
                        card.variant or "default",
                        card.model_id or "",
                    )
                )
        pruned = prune_stale_latest_usage(session, batch_keys)
        session.commit()
        logger.info(
            f"Stored {len(local_cards)} local cards into LatestUsage from {payload.provider}"
            + (f" (pruned {pruned} ghost row(s))" if pruned else "")
        )

    # Wake the poller whenever the sidecar pushes anything actionable —
    # tokens or local cards. Without this, token-only payloads (the common
    # case) leave the poller asleep until its 15-min interval, so the
    # dashboard stays empty even though credentials are in the cache.
    if tokens_to_store or local_cards:
        from app.services.collector_manager import manager
        from app.services.poller import poller

        # Force the next collect_all to re-sync per-account collectors so
        # the freshly-pushed accounts get SmartCollectors immediately
        # instead of waiting the 60s sync throttle.
        manager._last_sync_time = 0.0
        poller.wake()

    # Process events for atomic usage tracking
    ingest_result = None
    events_error = False
    if payload.events:
        from app.services.event_ingestor import EventIngestor

        try:
            ingestor = EventIngestor(session)
            ingest_result = ingestor.ingest(payload.events, sidecar_id=payload.sidecar_id)
            logger.info(
                f"Ingested {ingest_result.events_inserted} events "
                f"({ingest_result.events_duplicate} dup, "
                f"{ingest_result.events_reattributed} re-attributed) "
                f"from {payload.sidecar_id or 'unknown'}"
            )
        except Exception as e:
            logger.error(f"Event ingestion failed: {e}", exc_info=True)
            ingest_result = None
            events_error = True

    # Determine which providers this sidecar should poll right now.
    # The server is the cadence authority — sidecars heartbeat frequently and
    # collect only what we tell them via poll_providers (per-provider intervals
    # gate this server-side via fleet_registry.get_due_providers).
    poll_providers: list[str] = []
    trigger: bool = False
    sys_cfg = session.exec(select(SystemConfig)).first()
    collection_enabled = True
    if payload.sidecar_id:
        # Honor per-sidecar pause: paused sidecars still check in but receive
        # no poll instructions, and their pending-trigger flag is preserved
        # so a resume can still deliver it.
        sc_row = session.get(SidecarRegistry, payload.sidecar_id)
        if sc_row is not None and not sc_row.collection_enabled:
            collection_enabled = False
        else:
            global_interval = (sys_cfg.default_poll_interval_seconds if sys_cfg else None) or 900

            enabled_provider_rows = session.exec(
                select(ProviderConfig).where(ProviderConfig.enabled)
            ).all()
            configured = {row.provider_id for row in enabled_provider_rows}

            # Passive providers (antigravity, opencode-free, …) have no
            # provider_configs row because they need no credentials — the
            # sidecar discovers them locally. Without this they'd never
            # appear in poll_providers and would only refresh on cold-start
            # or a user-triggered full refresh.
            passive_pids = (
                set(session.exec(select(LatestUsage.provider_id).distinct()).all()) - configured
            )

            provider_intervals = [
                (row.provider_id, row.poll_interval_seconds or global_interval)
                for row in enabled_provider_rows
            ] + [(pid, global_interval) for pid in sorted(passive_pids)]

            poll_providers, trigger = fleet_registry.get_due_providers(
                payload.sidecar_id, provider_intervals
            )
            if poll_providers:
                logger.info(f"Instructing sidecar '{payload.sidecar_id}' to poll: {poll_providers}")

    # One-shot admin "Update now" push: consumed once so the sidecar self-updates
    # on this heartbeat (independent of the auto-update toggle / pause state).
    update_now = (
        fleet_registry.consume_pending_update(payload.sidecar_id, session)
        if payload.sidecar_id
        else False
    )

    return {
        "status": "ok",
        "provider": payload.provider,
        "tokens_received": tokens_received_count,
        "metrics_stored": len(local_cards),
        "events_received": ingest_result.events_received if ingest_result else 0,
        "events_inserted": ingest_result.events_inserted if ingest_result else 0,
        "events_duplicate": ingest_result.events_duplicate if ingest_result else 0,
        "events_reattributed": ingest_result.events_reattributed if ingest_result else 0,
        # True when this batch's events were NOT stored — the sidecar keeps
        # its watermark and re-sends them next cycle instead of losing them.
        "events_error": events_error,
        "windows_closed": ingest_result.windows_closed if ingest_result else 0,
        "poll_providers": poll_providers,
        "trigger": trigger,
        "collection_enabled": collection_enabled,
        "identities": _get_active_identities(
            session
        ),  # For sidecar identity propagation (legacy single-value)
        "account_identities": _get_active_identity_lists(
            session
        ),  # Per-provider list of real account_ids (multi-account)
        "reset_anchors": _reset_anchors_for_sidecar(session),  # Phase 6
        # Update channel the sidecar should track for its "update available"
        # check ("stable" | "edge"). The dashboard owns this setting.
        "sidecar_update_channel": (sys_cfg.sidecar_update_channel if sys_cfg else None) or "stable",
        # Fleet-wide opt-in auto-update flag; a sidecar's explicit local config wins.
        "sidecar_auto_update": (sys_cfg.sidecar_auto_update if sys_cfg else None) or False,
        # One-shot: self-update immediately on this heartbeat (admin pushed it).
        "update_now": update_now,
        # Tag hints the sidecar consumes on its next collection cycle to
        # stamp cards it couldn't resolve via local discovery alone (silent
        # listener model — see PR #288). Scoped to the ingesting sidecar (#319).
        "account_tag_hints": _account_tag_hints_for_providers(
            session, list(poll_providers), sidecar_id=payload.sidecar_id or None
        )
        if poll_providers
        else {},
    }


def _account_tag_hints_for_providers(
    session: Session, providers: list[str], *, sidecar_id: str | None = None
) -> dict[str, dict[str, str]]:
    """Return ``{provider_id: {credential_origin: account_id, ...}}`` for the
    requested providers, scoped to the requesting sidecar (#319).

    Powers ``/fleet/config``, ``/fleet/ingest``, and the manifest
    response, so the sidecar can stamp cards it couldn't resolve
    locally.

    Thin wrapper over :meth:`CredentialTagRepo.list_pending_payload` +
    :meth:`CredentialTagRepo.auto_hints_for_single_account_providers`
    — the lookup keys live in the repo so future callers all read
    through the same SQL shape (PR #290 round-2 review Hermes body
    suggestion #6).

    Merges the operator-resolved ``credential_tags`` rows on top of the
    auto-hints for single-account providers — the auto-hint is the
    fallback for a fresh provider like MiniMax whose quota gauge lives
    at the operator's chosen account but whose upstream has no per-
    user identity, so the sidecar's local discovery returns nothing
    useful. The hint unblocks the events on the very next cycle so
    the quota gauge and the sidecar events merge into one Fleet
    entry. Operator tags (when present) always win, since they're
    explicit and the auto-hint is implicit.

    Requester identity: with ``sidecar_id`` given, both that sidecar's
    scoped tags and deployment-wide (NULL) tags ship (scoped winning).
    Without one (old sidecar binaries that don't send
    ``?sidecar_id=`` on ``/fleet/config``), the caller is treated as
    the deployment's only live sidecar when exactly one exists — so a
    single-host operator's newly created machine-scoped tags still
    reach an un-upgraded binary — and falls back to deployment-wide
    tags only when zero or 2+ sidecars are live.
    """
    effective_sidecar_id = sidecar_id
    if effective_sidecar_id is None:
        live = live_sidecar_ids(session)
        if len(live) == 1:
            effective_sidecar_id = live[0]
    resolved = CredentialTagRepo.list_pending_payload(
        session, providers=providers, sidecar_id=effective_sidecar_id
    )
    # Auto-hints key off the *original* requester identity: an
    # unidentified fetcher in a multi-host deployment gets no auto-hints
    # (safe default), while ≤1 live sidecar ships unconditionally.
    auto = CredentialTagRepo.auto_hints_for_single_account_providers(
        session, providers=providers, sidecar_id=sidecar_id
    )
    # Operator tags win over auto-hints — explicit operator choice is
    # never overridden by the implicit single-account heuristic.
    for pid, by_origin in auto.items():
        bucket = resolved.setdefault(pid, {})
        for origin, account_id in by_origin.items():
            bucket.setdefault(origin, account_id)
    return resolved


# ---------------------------------------------------------------------------
# Silent listener — /fleet/credentials/manifest (sidecar-issued)
# ---------------------------------------------------------------------------
#
# The sidecar reports the credential_origins it discovered locally. The server
# upserts each into pending_credential_tags for UI surfacing, prunes entries
# the sidecar didn't re-report, and returns the resolved hints the sidecar
# will consume on its next /fleet/config. Closes the silent-listener loop
# without touching the sidecar's identity-local discovery paths.


class CredentialManifestRequest(BaseModel):
    """Body shape for POST /fleet/credentials/manifest."""

    sidecar_id: str
    entries: list[dict[str, str]] = []  # [{provider_id, credential_origin}, ...]


@router.post("/credentials/manifest")
@limiter.limit("60/minute")
async def post_credential_manifest(
    request: Request,
    x_signature: str = Header(None, alias="X-Signature"),
    x_timestamp: str = Header(None, alias="X-Timestamp"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Sidecar-issued manifest of credential origins it found locally.

    The body lists every origin the sidecar currently has. The server
    upserts each into ``pending_credential_tags`` (these power the
    "Untagged credentials" surface in the fleet UI), prunes entries the
    sidecar didn't re-report this cycle (a credential disappeared from
    disk), and responds with the resolved hints — origin → account_id —
    the sidecar will consume on its next ``/fleet/config`` round-trip.

    Rate limit: 60/min — one call per sidecar per heartbeat (10-min default)
    is the steady state, so 60/min is ~6× headroom for retries.
    """
    body_bytes = await validate_ingest_auth(request, x_signature, x_timestamp)
    try:
        payload = CredentialManifestRequest.model_validate_json(body_bytes)
    except Exception as exc:
        logger.debug(f"manifest: invalid body: {exc}")
        raise HTTPException(status_code=400, detail=f"Invalid manifest: {exc}") from exc

    if not payload.sidecar_id:
        raise HTTPException(status_code=400, detail="sidecar_id is required")

    # Mirror /fleet/ingest: normalize the sidecar_id so a reporter that
    # sends an FQDN doesn't get a pending row keyed on a string no
    # card can surface in the per-sidecar badge query
    # (?sidecar_id=<registry id>). Defensive — today's sidecar normalizes
    # client-side, but a future reporter (custom integration, scripted
    # curl) might not (PR #290 round-2 review, Hermes suggestion #7).
    payload.sidecar_id = normalize_sidecar_id(payload.sidecar_id)

    keep_by_provider: dict[str, set[str]] = {}
    for entry in payload.entries:
        provider_id = entry.get("provider_id")
        origin = entry.get("credential_origin")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        if not isinstance(origin, str) or not origin:
            continue
        keep_by_provider.setdefault(provider_id, set()).add(origin)
        PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=payload.sidecar_id,
            provider_id=provider_id,
            credential_origin=origin,
        )

    # Count before stickiness folds synthetic auto-hint origins into the
    # keep-set — entries_received must reflect what the sidecar reported.
    entries_received = sum(len(v) for v in keep_by_provider.values())

    # Auto-hint stickiness (#319): the sidecar stops reporting a
    # synthetic ``provider:<pid>`` origin the moment the auto-hint
    # resolves it, and delete_stale would then prune the pending row —
    # which is exactly the signal multi-host auto-hint delivery keys
    # off, so the hint would oscillate off one manifest cycle after it
    # turned on. Retain those rows for as long as their auto-hint is
    # still active (they're hidden from the Untagged dialog via the
    # effective-hint filter on GET .../tags/pending). Candidates: pids
    # reported this cycle plus pids of rows still pending from before.
    existing_rows = PendingCredentialTagRepo.list_all(session, sidecar_id=payload.sidecar_id)
    candidate_pids = sorted({r.provider_id for r in existing_rows} | set(keep_by_provider))
    auto = CredentialTagRepo.auto_hints_for_single_account_providers(
        session, providers=candidate_pids, sidecar_id=payload.sidecar_id
    )
    for pid, by_origin in auto.items():
        keep_by_provider.setdefault(pid, set()).update(by_origin)

    removed = PendingCredentialTagRepo.delete_stale(
        session,
        sidecar_id=payload.sidecar_id,
        keep_origins_by_provider=keep_by_provider,
    )
    session.commit()

    resolved = _account_tag_hints_for_providers(
        session, candidate_pids, sidecar_id=payload.sidecar_id
    )

    return {
        "status": "ok",
        "sidecar_id": payload.sidecar_id,
        "entries_received": entries_received,
        "entries_pruned": removed,
        "resolved": resolved,
    }


# ---------------------------------------------------------------------------
# Silent listener — operator tag endpoints (admin-gated)
# ---------------------------------------------------------------------------


class CredentialTagRequest(BaseModel):
    """Body shape for POST /fleet/credentials/tags."""

    sidecar_id: str
    provider_id: str
    credential_origin: str
    # The chosen provider_configs row's account_id. The server looks up
    # the row to verify it exists before persisting the tag.
    account_id: str
    # "sidecar" (default): the tag applies only to ``sidecar_id`` —
    # "this machine" in the dialog. "deployment": the tag applies to
    # every sidecar (``credential_tags.sidecar_id = NULL``) — the
    # dialog's "All machines" scope for shared origins (NFS home dirs).
    scope: Literal["sidecar", "deployment"] = "sidecar"


@router.post("/credentials/tags")
async def post_credential_tag(
    request: Request,
    body: CredentialTagRequest,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Operator resolves a pending credential origin into a server-side
    ``account_id`` (matching a configured ``provider_configs`` row).

    Validates the chosen ``account_id`` corresponds to an existing
    ``provider_configs`` row for the same provider, persists a
    :class:`CredentialTag` (scoped per ``scope`` — #319), clears the
    matching pending row(s), and writes an audit-log row. A
    deployment-wide scope clears every sidecar's pending row for the
    origin; a sidecar scope clears only that sidecar's.
    """
    sidecar_id = normalize_sidecar_id(body.sidecar_id) if body.sidecar_id else ""
    if body.scope == "sidecar" and not sidecar_id:
        raise HTTPException(
            status_code=422,
            detail="sidecar_id is required for a machine-scoped tag (scope='sidecar').",
        )

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == body.provider_id,
            ProviderConfig.account_id == body.account_id,
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No provider_configs row for provider={body.provider_id!r} "
                f"account_id={body.account_id!r} — create one in Provider Settings "
                "first, then tag."
            ),
        )

    CredentialTagRepo.set_tag(
        session,
        provider_id=body.provider_id,
        credential_origin=body.credential_origin,
        account_id=body.account_id,
        sidecar_id=sidecar_id if body.scope == "sidecar" else None,
        set_by=getattr(request.state.auth, "actor", "operator")
        if hasattr(request.state, "auth")
        else "operator",
    )
    if body.scope == "deployment":
        # "All machines" must actually win on every machine: drop any
        # machine-scoped override for this origin, otherwise it keeps
        # resolving first (scoped > deployment) and stays invisible.
        CredentialTagRepo.delete_scoped_for_origin(
            session,
            provider_id=body.provider_id,
            credential_origin=body.credential_origin,
        )
        PendingCredentialTagRepo.delete_by_origin(
            session,
            provider_id=body.provider_id,
            credential_origin=body.credential_origin,
        )
    else:
        PendingCredentialTagRepo.delete(
            session,
            sidecar_id=sidecar_id,
            provider_id=body.provider_id,
            credential_origin=body.credential_origin,
        )
    # Commit the tag explicitly — ``audit_log.record`` swallows its own
    # errors, so relying on its commit could drop the tag while still
    # answering ``ok``.
    session.commit()

    audit_log.record(
        session,
        request,
        action="credential.tag_set",
        target_id=f"{body.provider_id}/{body.account_id}",
        payload={
            "credential_origin": body.credential_origin,
            "sidecar_id": sidecar_id or None,
            "scope": body.scope,
        },
    )

    return {"status": "ok"}


@router.get("/credentials/tags")
async def list_credential_tags(
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """List every resolved credential tag (both scopes).

    Once an origin is tagged it never shows up as pending again, so this
    is the only way for the operator to see — and correct — an existing
    mapping. ``sidecar_id`` is ``None`` for deployment-wide ("All
    machines") tags.
    """
    return {
        "items": [
            {
                "provider_id": t.provider_id,
                "credential_origin": t.credential_origin,
                "account_id": t.account_id,
                "sidecar_id": t.sidecar_id,
                "set_by": t.set_by,
                "set_at": t.set_at.isoformat() if t.set_at else None,
            }
            for t in CredentialTagRepo.list_all(session)
        ]
    }


@router.delete("/credentials/tags")
async def delete_credential_tag(
    request: Request,
    provider_id: str = Query(...),
    credential_origin: str = Query(...),
    sidecar_id: str | None = Query(default=None),
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Remove one resolved tag (exactly one scope).

    ``sidecar_id`` omitted = the deployment-wide ("All machines") row;
    otherwise that machine's row. The sidecar stops receiving the hint on
    its next ``/fleet/config`` refresh; if the origin still has no local
    identity it re-appears as pending on the following manifest, so the
    operator can re-tag it.
    """
    scoped_id = (normalize_sidecar_id(sidecar_id) if sidecar_id else "") or None
    if sidecar_id and scoped_id is None:
        # A sidecar_id that normalizes to nothing must not silently fall
        # through to deleting the deployment-wide ("All machines") row.
        raise HTTPException(status_code=422, detail=f"Invalid sidecar_id: {sidecar_id!r}")
    removed = CredentialTagRepo.delete_tag_in_scope(
        session,
        provider_id=provider_id,
        credential_origin=credential_origin,
        sidecar_id=scoped_id,
    )
    if not removed:
        raise HTTPException(status_code=404, detail="No such credential tag")
    session.commit()
    audit_log.record(
        session,
        request,
        action="credential.tag_delete",
        target_id=provider_id,
        payload={"credential_origin": credential_origin, "sidecar_id": scoped_id},
    )
    return {"status": "deleted"}


@router.get("/credentials/tags/pending")
async def list_pending_credential_tags(
    sidecar_id: str | None = None,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """List pending credential tags awaiting operator resolution.

    When ``sidecar_id`` is given, returns just that sidecar's pending set
    (used by the per-card badge on the fleet view). When omitted, returns
    all pending entries across sidecars (the top-of-page banner).

    Rows whose origin already has an effective hint (an explicit tag or
    an active single-account auto-hint) are hidden: #319 keeps
    auto-resolved synthetic ``provider:<pid>`` rows in the table so the
    auto-hint doesn't oscillate, but they're not "untagged" from the
    operator's perspective.
    """
    all_rows = PendingCredentialTagRepo.list_all(session)

    # Group by sidecar so hints resolve with that host's scope.
    by_sidecar: dict[str, list] = {}
    for r in all_rows:
        by_sidecar.setdefault(r.sidecar_id, []).append(r)

    visible_rows = []
    counts_by_sidecar: dict[str, int] = {}
    for sc_id, sc_rows in by_sidecar.items():
        pids = sorted({r.provider_id for r in sc_rows})
        hints = _account_tag_hints_for_providers(session, pids, sidecar_id=sc_id)
        for r in sc_rows:
            if r.credential_origin in hints.get(r.provider_id, {}):
                continue
            visible_rows.append(r)
            counts_by_sidecar[sc_id] = counts_by_sidecar.get(sc_id, 0) + 1

    if sidecar_id is not None:
        visible_rows = [r for r in visible_rows if r.sidecar_id == sidecar_id]

    return {
        "items": [
            {
                "sidecar_id": r.sidecar_id,
                "provider_id": r.provider_id,
                "credential_origin": r.credential_origin,
                "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
            for r in visible_rows
        ],
        "counts_by_sidecar": counts_by_sidecar,
    }


def _get_active_identities(session: Session) -> dict[str, str]:
    """Map provider_id to its single 'real' account_id seen in LatestUsage.

    Used by sidecars to discover their identity when local logs are anonymous.
    Kept as a single-value-per-provider dict for backward compat with existing
    sidecars. Multi-account consumers should read ``_get_active_identity_lists``
    (the new field) for the full per-provider list.
    """
    from app.services.credential_tags import live_sidecar_ids

    # Old sidecars prefer this value over their own local discovery, so it
    # must never name another host's account: ship it only when there is at
    # most one live sidecar AND the provider has exactly one real account.
    # Anything else resolves through the per-sidecar tag / auto-hint path.
    if len(live_sidecar_ids(session)) > 1:
        return {}
    return {
        pid: aids[0] for pid, aids in _get_active_identity_lists(session).items() if len(aids) == 1
    }


def _get_active_identity_lists(session: Session) -> dict[str, list[str]]:
    """Map provider_id to every distinct 'real' account_id seen in LatestUsage.

    New shape that ships alongside the legacy single-value ``identities`` so
    future per-account sidecars (Issue 1) can pick the right one. Empty list
    for a provider means "no real-account rows exist yet".
    """
    rows = _active_identity_rows(session)
    out: dict[str, list[str]] = {}
    for pid, aid in rows:
        # Preserve recency order (rows are already ordered by LatestUsage.updated_at desc).
        if aid not in out.setdefault(pid, []):
            out[pid].append(aid)
    return out


def _active_identity_rows(session: Session) -> list[tuple[str, str]]:
    """Distinct ``(provider_id, account_id)`` pairs from LatestUsage, real accounts only.

    Ordered by LatestUsage.updated_at descending so the most recently active
    identity surfaces first. Used by both the legacy single-value
    ``identities`` map and the new per-provider list.
    """
    from app.models.db import LatestUsage

    return list(
        session.exec(
            select(LatestUsage.provider_id, LatestUsage.account_id)
            .where(LatestUsage.account_id != "default")
            .where(col(LatestUsage.account_id).is_not(None))
            .order_by(col(LatestUsage.updated_at).desc())
        ).all()
    )


def _reset_anchors_for_sidecar(session: Session) -> dict[str, dict[str, str]]:
    """Per-provider authoritative reset_at by window_type, for sidecar use.

    Reads all LatestUsage rows with future reset_at and builds a dict of
    the latest reset_at per (provider_id, window_type) pair. Filters to
    only default variants (model_id="" and variant in ("", "default")).

    Returns:
        {
          "anthropic": {
            "session": "2026-05-08T18:00:00+00:00",
            "weekly":  "2026-05-12T18:00:00+00:00"
          },
          ...
        }
    """
    from datetime import UTC, datetime

    from app.models.db import LatestUsage

    # Push the default-variant / no-model-id filters into SQL so we don't
    # materialise per-model rows that we'd immediately discard.
    rows = session.exec(
        select(LatestUsage).where(
            LatestUsage.model_id == "",
            col(LatestUsage.variant).in_(["", "default"]),
        )
    ).all()
    now = datetime.now(UTC)
    anchors: dict[str, dict[str, str]] = {}

    for r in rows:
        # Defense-in-depth: filters above are already SQL-enforced.
        if r.model_id and r.model_id != "":
            continue
        if r.variant not in ("", "default"):
            continue

        # Parse card_json
        try:
            card = json.loads(r.card_json) if r.card_json else {}
        except json.JSONDecodeError:
            continue

        # Extract reset_at
        reset_at = card.get("reset_at")
        if not reset_at:
            continue

        # Parse datetime and check if it's in the future
        try:
            reset_dt = parse_iso8601_utc(reset_at)
        except ValueError:
            continue

        if reset_dt <= now:
            continue

        # Track the latest reset_at per (provider_id, window_type)
        prov_anchors = anchors.setdefault(r.provider_id, {})
        existing = prov_anchors.get(r.window_type)
        if existing is None or reset_at > existing:
            prov_anchors[r.window_type] = reset_at

    return anchors


@router.get("/sidecars")
@limiter.limit("30/minute")
async def list_sidecars(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """List all registered sidecars."""
    rows = session.exec(
        select(SidecarRegistry).order_by(col(SidecarRegistry.last_seen).desc())
    ).all()
    return {"sidecars": [fleet_registry.to_dict(row) for row in rows]}


@router.get("/sidecars/{sidecar_id}")
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
    return fleet_registry.to_dict(row)


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
    return fleet_registry.to_dict(row)


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
    trusted = verify_config_signature(request) or is_loopback_bind()

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
        session, list(config["providers"].keys()), sidecar_id=sidecar_id
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


# NOTE: /api/v1/fleet/credentials/redeem is intentionally not implemented
# in this PR. Adding the endpoint without a production caller would ship
# unused authenticated surface (HMAC body parsing, decryption, audit
# hooks) that nothing exercises — see code review on PR #283. The token
# module + the issuance in ``/fleet/config`` are the public surface for
# now; the redeem handler lands with the first real caller in the
# follow-up PR.


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


class PairResponse(BaseModel):
    api_url: str
    api_key: str


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


@router.post("/pair", response_model=PairResponse)
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
    try:
        row = pairing.redeem(session, body.code, hostname=hostname)
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
    return PairResponse(api_url=row.server_url, api_key=_settings.INGEST_API_KEY)
