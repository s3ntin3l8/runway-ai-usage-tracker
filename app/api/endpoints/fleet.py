import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import Session, col, func, select

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
    CredentialSource,
    CredentialTag,
    LatestUsage,
    PendingUsageEvent,
    ProviderConfig,
    SidecarRegistry,
    SystemConfig,
    UsageEvent,
)
from app.models.schemas import IngestRequest, UsageEventPush
from app.services import audit_log, pairing
from app.services.account_identity import (
    FINGERPRINTED_ORIGIN_PROVIDERS,
    account_config_provider_id,
    canonical_account_id,
    credential_fingerprint,
    keyed_credential_origin,
    normalize_sidecar_id,
    resolve_account_id,
)
from app.services.accumulator import (
    prune_stale_latest_usage,
    reconcile_latest_usage_snapshot,
    upsert_latest_usage,
)
from app.services.credential_tags import (
    CredentialTagRepo,
    PendingCredentialTagRepo,
    live_sidecar_ids,
)
from app.services.credential_token import issue_credential_token
from app.services.event_ingestor import EventIngestor
from app.services.fleet_registry import fleet_registry
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)
router = APIRouter()

# Credential fields a sidecar token card may carry into the server's token cache.
# Anything else in ``metadata`` is dropped, so every key the sidecar's embedded
# registry can map a secret to must be listed here — a missing key silently
# disables the collector that reads it (e.g. kimi's ``session_cookie`` or
# opencode's ``console_session``). ``tests/unit/test_ingest_credential_keys.py``
# keeps this in lockstep with ``scripts/sidecar.py``.
_INGEST_CREDENTIAL_KEYS = frozenset(
    {
        "oauth_token",
        "refresh_token",
        "api_key",
        "id_token",
        "expiry_date",
        "client_id",
        "xai_access",
        "xai_refresh",
        "cli_access_token",
        "cli_expires_at",
        "session_cookie",
        "console_session",
    }
)
# Cookie-family keys that carry browser-session secrets (``cookie_<name>`` for
# named cookies, plus the two bundled-cookie fields above).
_COOKIE_BUNDLE_KEYS = frozenset({"session_cookie", "console_session"})


def _can_reconcile_row(provider_id: str, row_account_id: str, target_account_id: str) -> bool:
    """May ingest move/delete a source row filed under ``row_account_id`` now that the
    source resolves to ``target_account_id``?

    A source belongs to exactly one account, so a row elsewhere is stale — but how much
    ingest may touch differs by provider. Anthropic reconciles every other account (its
    sources used to be filed under token-derived placeholder identities). Everyone else
    only retires the ``default`` placeholder the manifest may have created before the
    real identity was known; a row an operator moved to another account is not ours to
    touch here.
    """
    if row_account_id == target_account_id:
        return False
    return provider_id == "anthropic" or row_account_id == "default"


def _is_cookie_key(key: str) -> bool:
    return key.startswith("cookie_") or key in _COOKIE_BUNDLE_KEYS


# HMAC-only sidecar routes: POST /ingest, POST /credentials/manifest, and
# GET /config. Keep docs/forward-auth.md's exact bypass route list in sync;
# other fleet routes remain protected by Authentik.


class SidecarUpdateRequest(BaseModel):
    custom_name: str | None = None
    tags: list[str] | None = None


class PendingEventAssignment(BaseModel):
    event_ids: list[int]
    account_id: str


class PendingEventBatchAssignment(BaseModel):
    assignments: list[PendingEventAssignment]


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
                credential_origin = (
                    card.metadata.get("credential_origin") if card.metadata else None
                )
                identity_pending = bool(
                    card.metadata and card.metadata.get("identity_pending") is True
                ) or bool(credential_origin and payload.sidecar_id and not acc_id)
                verified_tag = None
                if identity_pending and credential_origin and payload.sidecar_id:
                    # A verified identity can race one more heartbeat with
                    # stale pending metadata. Trust only the durable tag for
                    # this exact origin and reporting machine.
                    verified_tag = CredentialTagRepo.get(
                        session,
                        provider_id=provider_id,
                        credential_origin=credential_origin,
                        sidecar_id=payload.sidecar_id,
                    )
                    if verified_tag is not None and verified_tag.set_by != "identity_claim":
                        acc_id = verified_tag.account_id
                        identity_pending = False
                if provider_id == "anthropic" and credential_origin and payload.sidecar_id:
                    # Claude Code commonly stores its email separately from its
                    # OAuth token. A sidecar can claim that email, but only an
                    # exact configured account (or an operator tag) may receive
                    # the token. Otherwise keep it source-pinned for assignment.
                    tagged = verified_tag
                    if tagged is None:
                        tagged = CredentialTagRepo.get(
                            session,
                            provider_id=provider_id,
                            credential_origin=credential_origin,
                            sidecar_id=payload.sidecar_id,
                        )
                    if tagged is not None and tagged.set_by != "identity_claim":
                        acc_id = tagged.account_id
                        identity_pending = False
                    elif card.unit == "oauth":
                        claimed_id = canonical_account_id(acc_id)
                        matched_account = (
                            session.exec(
                                select(ProviderConfig.id).where(
                                    ProviderConfig.provider_id == provider_id,
                                    ProviderConfig.account_id == claimed_id,
                                    ProviderConfig.enabled == True,  # noqa: E712
                                )
                            ).first()
                            is not None
                            if claimed_id != "default"
                            else False
                        )
                        if not matched_account:
                            acc_label = claimed_id if claimed_id != "default" else None
                            acc_id = None
                            identity_pending = True
                # Keep unidentified bundles in the isolated verifier slot. Older
                # sidecars omitted identity_pending and supplied no account id;
                # token_cache would otherwise hash the token and strand it away
                # from the verifier's default candidates.
                if identity_pending:
                    # Give every pending token bundle an explicit account slot.
                    acc_id = "default"
                if card.metadata:
                    for key, val in card.metadata.items():
                        # Store tokens but skip the provider/account identifiers
                        if key not in (
                            "provider_id",
                            "account_id",
                            "account_label",
                            "credential_origin",
                        ) and (key in _INGEST_CREDENTIAL_KEYS or _is_cookie_key(key)):
                            provider_tokens[key] = val

                # Older sidecars combined browser cookies and CLI OAuth in
                # one card, then assigned the CLI account to the whole card.
                # That identity does not prove the cookie owner. Keep the
                # independently identified CLI family and discard the cookie
                # fields from this legacy mixed payload.
                if any(_is_cookie_key(key) for key in provider_tokens) and any(
                    key in provider_tokens
                    for key in ("oauth_token", "refresh_token", "id_token", "api_key")
                ):
                    provider_tokens = {
                        key: value
                        for key, value in provider_tokens.items()
                        if not _is_cookie_key(key)
                    }

                if provider_tokens:
                    tokens_to_store.append(
                        (
                            provider_id,
                            provider_tokens,
                            acc_id,
                            acc_label,
                            credential_origin,
                            identity_pending,
                        )
                    )
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
    for p_id, p_tokens, a_id, a_name, origin, identity_pending in tokens_to_store:
        sidecar_id = payload.sidecar_id or "local"
        from app.services.credential_sources import (
            describe_origin,
            sidecar_source_id,
            touch_source,
        )

        source_type, source_label = describe_origin(origin)
        source_id = sidecar_source_id(sidecar_id, origin)
        # Every account this source is currently filed under (needed to know whether
        # the resolved account already has its row).
        prior_sources = list(
            session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == p_id,
                    CredentialSource.source_id == source_id,
                )
            ).all()
        )
        # Claude OAuth sources from older sidecars may still be keyed by the
        # token-derived placeholder identity; use a stable host+origin key while
        # identity is pending so token rotations do not create orphan entries.
        cache_account_id = source_id if p_id == "anthropic" and identity_pending else a_id
        if p_id == "anthropic" and not identity_pending:
            # A pending source is cache-keyed by its stable source_id. Retire
            # that placeholder even if its durable CredentialSource row was
            # lost; otherwise its OAuth bundle survives beside the resolved one.
            await token_cache.remove_source(p_id, source_id, source_id, retire_matching_oauth=True)
        actual_acc_id = await token_cache.store(
            p_id,
            p_tokens,
            cache_account_id,
            a_name,
            source=payload.sidecar_id,
            source_id=source_id,
            source_metadata={
                "source_type": source_type,
                "source_label": source_label,
                "credential_origin": origin,
                "sidecar_id": payload.sidecar_id,
                "identity_pending": identity_pending,
                "identity_hint": a_name if identity_pending else None,
            },
        )
        if not isinstance(actual_acc_id, str):
            actual_acc_id = a_id or "default"
        # Anthropic reconciles every other account this source was filed under
        # (token-derived placeholder identities). Other providers only retire the
        # ``default`` placeholder the manifest may have created before ingest
        # resolved the real identity — a source belongs to exactly one account, but
        # an operator-moved row elsewhere is not ours to touch here.
        replaceable = [
            row for row in prior_sources if _can_reconcile_row(p_id, row.account_id, actual_acc_id)
        ]
        old_accounts = {row.account_id for row in replaceable}
        target_exists = any(row.account_id == actual_acc_id for row in prior_sources)
        transferable = (
            max(replaceable, key=lambda row: row.last_seen, default=None)
            if not target_exists
            else None
        )
        for row in replaceable:
            if row is transferable:
                row.account_id = actual_acc_id
                session.add(row)
            else:
                session.delete(row)
        session.flush()
        touch_source(
            session,
            provider_id=p_id,
            account_id=actual_acc_id,
            source_id=source_id,
            source_type=source_type,
            source_label=source_label,
            credential_origin=origin,
            sidecar_id=payload.sidecar_id,
        )
        for old_account in old_accounts:
            await token_cache.remove_source(
                p_id, old_account, source_id, retire_matching_oauth=True
            )
        tokens_received_count += len(p_tokens)
        logger.info(
            f"Received {len(p_tokens)} tokens for {p_id} account {actual_acc_id} from {payload.provider}"
        )
    if tokens_to_store:
        session.commit()

    # Store local data cards directly into LatestUsage (unified with server-scraped cards)
    if local_cards or payload.completed_providers:
        # Track (provider_id, canonical_account_id) → set of
        # (window_type, variant, model_id) for the prune step.
        batch_keys: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
        for card in local_cards:
            card_dict = card.model_dump(exclude_none=True)
            upsert_latest_usage(
                session,
                card_dict,
                sidecar_id_override=card.sidecar_id or payload.sidecar_id or "local",
                source_id=f"sidecar:{payload.sidecar_id or 'local'}:{card.provider_id}",
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
        if payload.completed_providers is not None:
            for provider_id in payload.completed_providers:
                source_id = f"sidecar:{payload.sidecar_id or 'local'}:{provider_id}"
                accounts = {account_id for pid, account_id in batch_keys if pid == provider_id}
                from app.models.db import LatestUsageContribution

                accounts.update(
                    row.account_id
                    for row in session.exec(
                        select(LatestUsageContribution).where(
                            LatestUsageContribution.provider_id == provider_id,
                            LatestUsageContribution.source_id == source_id,
                        )
                    ).all()
                )
                for account_id in accounts:
                    reconcile_latest_usage_snapshot(
                        session,
                        provider_id=provider_id,
                        account_id=account_id,
                        source_id=source_id,
                        reported_keys=batch_keys.get((provider_id, account_id), set()),
                    )
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

        if tokens_to_store:
            affected_providers = dict.fromkeys(p_id for p_id, *_rest in tokens_to_store)
            for provider_id in affected_providers:
                await manager.reconcile_token_cache_from_durable_tags(provider_id=provider_id)
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
        # check ("stable" | "beta" | "edge"). The dashboard owns this setting.
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


def _fingerprinted_credential_hints(
    session: Session, providers: list[str]
) -> dict[str, dict[str, str]]:
    """``{provider_id: {provider:<pid>#<fingerprint>: account_id}}`` for
    providers whose credential is a bare key (#347; widened from opencode
    to its OpenCode-file siblings in #349).

    The sidecar suffixes such a credential's origin with a fingerprint of
    the value it found on disk so two hosts (or one host after a key
    rotation) can never share an origin. When the same key has been pasted
    into ``provider_configs``, the server can recognise it here and answer
    with the account that paste belongs to — without ever seeing the
    sidecar's filesystem layout, and without the sidecar ever seeing a key
    it didn't already hold. Only the 12-hex fingerprint crosses the wire.

    The hint key is built from the *provider* descriptor rather than the
    file descriptor precisely because the server does not know the
    sidecar's path: it can only ever construct ``provider:<pid>#<fp>``.

    Unlike the auto-hint this ships unconditionally — a fingerprint is not
    a guess. A sidecar that lacks the key cannot produce a matching
    fingerprint, so there is nothing to scope: the match is the scoping.
    """
    keyed = FINGERPRINTED_ORIGIN_PROVIDERS.intersection(providers)
    if not keyed:
        return {}

    rows = list(
        session.exec(
            select(ProviderConfig)
            .where(
                col(ProviderConfig.provider_id).in_(sorted(keyed)),
                ProviderConfig.enabled == True,  # noqa: E712 — SQLModel needs the ==
            )
            .order_by(col(ProviderConfig.provider_id), col(ProviderConfig.account_id))
        ).all()
    )
    candidates: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        try:
            api_key = row.api_key
        except Exception:  # pragma: no cover — undecryptable stored key
            logger.debug("fingerprint hint: cannot decrypt %s key", row.provider_id, exc_info=True)
            continue
        fingerprint = credential_fingerprint(api_key)
        if not fingerprint:
            continue
        hint_key = keyed_credential_origin(
            CredentialTagRepo.provider_origin(row.provider_id), fingerprint
        )
        candidates.setdefault(row.provider_id, {}).setdefault(hint_key, set()).add(row.account_id)
    # The same key stored under multiple accounts is ambiguous; keep it for
    # manual assignment rather than making ordered rows an accidental policy.
    out = {
        provider_id: {origin: next(iter(ids)) for origin, ids in origins.items() if len(ids) == 1}
        for provider_id, origins in candidates.items()
    }
    if not out:
        return {}
    return out


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

    Merge order, most authoritative first:

    1. operator ``credential_tags`` rows — explicit, always win;
    2. :func:`_fingerprinted_credential_hints` — a definitive match
       between the sidecar's discovered key and a key the operator
       pasted into ``provider_configs`` (#347 T1);
    3. the single-account auto-hint — implicit, kept as the last
       resort for a fresh provider like MiniMax whose quota gauge
       lives at the operator's chosen account but whose upstream has
       no per-user identity, so the sidecar's local discovery returns
       nothing useful. The hint unblocks the events on the very next
       cycle so the quota gauge and the sidecar events merge into one
       Fleet entry.

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
    # A fingerprint match outranks the auto-hint but never an operator tag:
    # the tag is a deliberate per-origin choice, the fingerprint a derived
    # one. Unlike tags, the fingerprint hint is not scoped to a sidecar —
    # it doesn't need to be: only a sidecar that already holds the key can
    # build a matching fingerprint, so the match is the scoping.
    for pid, by_origin in _fingerprinted_credential_hints(session, providers).items():
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
# only for providers whose collection completed, and returns the resolved
# hints the sidecar will consume on its next /fleet/config. Closes the
# silent-listener loop without touching identity-local discovery paths.


class CredentialHealthObservation(BaseModel):
    """Non-secret health metadata for one sidecar credential source."""

    provider_id: str = Field(min_length=1)
    credential_origin: str = Field(min_length=1)
    token_types: list[str] = Field(default_factory=list)
    expires_at: float | None = None


class CredentialManifestRequest(BaseModel):
    """Body shape for POST /fleet/credentials/manifest."""

    sidecar_id: str
    entries: list[dict[str, str]] = []  # [{provider_id, credential_origin}, ...]
    # A provider is listed only when its collection completed successfully.
    # Older sidecars omit this field; their partial manifests are upsert-only.
    completed_providers: list[str] | None = None
    # Safe source metadata only: never include credential values.
    observations: list[CredentialHealthObservation] = Field(default_factory=list, max_length=256)


@router.post("/credentials/manifest")
@limiter.limit("60/minute")
async def post_credential_manifest(
    request: Request,
    x_signature: str = Header(None, alias="X-Signature"),
    x_timestamp: str = Header(None, alias="X-Timestamp"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Sidecar-issued manifest of credential origins it found locally.

    The body lists origins from this cycle and names providers whose
    collection completed. The server upserts each into
    ``pending_credential_tags`` and prunes missing origins only for those
    completed providers. That keeps partial failures from hiding unresolved
    credentials while letting a completed empty scan clear stale entries.
    The response includes resolved origin → account_id hints for the next
    ``/fleet/config`` round-trip.

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

    from app.services.credential_sources import (
        describe_origin,
        resolve_source_account,
        sidecar_source_id,
        touch_source,
    )

    # Each manifest is authoritative: the latest observed token names and
    # expiry replace the prior health metadata for that source.
    for observation in payload.observations:
        observation_provider = observation.provider_id
        observation_origin = observation.credential_origin
        source_type, source_label = describe_origin(observation_origin)
        try:
            expires_at = (
                datetime.fromtimestamp(observation.expires_at, tz=UTC)
                if observation.expires_at is not None
                else None
            )
        except (TypeError, ValueError, OverflowError, OSError):
            expires_at = None
        token_types = observation.token_types
        tag = CredentialTagRepo.get(
            session,
            provider_id=observation_provider,
            credential_origin=observation_origin,
            sidecar_id=payload.sidecar_id,
        )
        observed_source_id = sidecar_source_id(payload.sidecar_id, observation_origin)
        # No operator tag does not mean "unidentified": the sidecar may have resolved
        # the identity locally (id_token email, gh login), in which case ingest already
        # registered this source under the real account. Reuse that row rather than
        # filing a second one under the ``default`` placeholder, which would show as a
        # phantom "Pending identity" credential next to the real one.
        observed_account = (
            tag.account_id
            if tag
            else resolve_source_account(session, observation_provider, observed_source_id)
            or "default"
        )
        touch_source(
            session,
            provider_id=observation_provider,
            account_id=observed_account,
            source_id=observed_source_id,
            source_type=source_type,
            source_label=source_label,
            credential_origin=observation_origin,
            sidecar_id=payload.sidecar_id,
            credential_expires_at=expires_at,
            token_types=token_types,
        )
    if payload.observations:
        session.flush()

    keep_by_provider: dict[str, set[str]] = {}
    entries_received = 0
    for entry in payload.entries:
        provider_id = entry.get("provider_id")
        origin = entry.get("credential_origin")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        if not isinstance(origin, str) or not origin:
            continue
        entries_received += 1
        existing_tag = CredentialTagRepo.get(
            session,
            provider_id=provider_id,
            credential_origin=origin,
            sidecar_id=payload.sidecar_id,
        )
        if existing_tag is not None and existing_tag.set_by != "identity_claim":
            # A prior operator assignment or source-verified identity already
            # resolves this origin. Do not re-open it as pending each heartbeat.
            PendingCredentialTagRepo.delete(
                session,
                sidecar_id=payload.sidecar_id,
                provider_id=provider_id,
                credential_origin=origin,
            )
            continue
        claimed_id = entry.get("account_id")
        if provider_id == "anthropic" and isinstance(claimed_id, str):
            matched = session.exec(
                select(ProviderConfig.id).where(
                    ProviderConfig.provider_id == provider_id,
                    ProviderConfig.account_id == canonical_account_id(claimed_id),
                    ProviderConfig.enabled == True,  # noqa: E712
                )
            ).first()
            if matched is not None:
                CredentialTagRepo.set_tag(
                    session,
                    provider_id=provider_id,
                    credential_origin=origin,
                    account_id=canonical_account_id(claimed_id),
                    sidecar_id=payload.sidecar_id,
                    set_by="identity_claim",
                )
                PendingCredentialTagRepo.delete(
                    session,
                    sidecar_id=payload.sidecar_id,
                    provider_id=provider_id,
                    credential_origin=origin,
                )
                continue
        if existing_tag is not None and existing_tag.set_by == "identity_claim":
            # Identity-claim rows are a visible record of automatic matching,
            # not durable operator assignments. Drop a stale claim so the
            # origin can return to the pending queue after account changes.
            CredentialTagRepo.delete_tag_in_scope(
                session,
                provider_id=provider_id,
                credential_origin=origin,
                sidecar_id=payload.sidecar_id,
            )
        keep_by_provider.setdefault(provider_id, set()).add(origin)
        pending = PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=payload.sidecar_id,
            provider_id=provider_id,
            credential_origin=origin,
        )
        pending.claimed_account_id = (
            canonical_account_id(claimed_id)
            if provider_id == "anthropic" and isinstance(claimed_id, str) and "@" in claimed_id
            else None
        )
        session.add(pending)

    # Retain currently reported origins and prune origins no longer present.
    existing_rows = PendingCredentialTagRepo.list_all(session, sidecar_id=payload.sidecar_id)
    candidate_provider_ids = {r.provider_id for r in existing_rows} | set(keep_by_provider)
    # Saved mappings must be returned even after their pending-origin row was
    # cleared. Include explicitly mapped providers so the sidecar can refresh
    # a stale/empty hint cache on this manifest round-trip (including
    # canonical providers used by OpenCode and Hermes events).
    candidate_provider_ids.update(
        pid
        for pid in session.exec(
            select(CredentialTag.provider_id)
            .where(
                or_(
                    col(CredentialTag.sidecar_id) == payload.sidecar_id,
                    col(CredentialTag.sidecar_id).is_(None),
                )
            )
            .distinct()
        ).all()
        if isinstance(pid, str) and pid
    )
    candidate_pids = sorted(candidate_provider_ids)

    # The sidecar reports which providers completed so a missing provider
    # cannot make an incomplete collection cycle look like an empty snapshot.
    # Older sidecars omit completed_providers and remain upsert-only.
    # delete_stale considers every row under this sidecar, so seed its keep
    # map with current origins for incomplete providers before replacing the
    # entries for providers that completed this cycle.
    prune_keep: dict[str, set[str]] = {}
    for row in existing_rows:
        if (
            payload.completed_providers is None
            or row.provider_id not in payload.completed_providers
        ):
            prune_keep.setdefault(row.provider_id, set()).add(row.credential_origin)
    if payload.completed_providers is not None:
        prune_keep.update(
            {
                provider_id: keep_by_provider.get(provider_id, set())
                for provider_id in payload.completed_providers
                if provider_id
            }
        )
    removed = (
        PendingCredentialTagRepo.delete_stale(
            session,
            sidecar_id=payload.sidecar_id,
            keep_origins_by_provider=prune_keep,
        )
        if prune_keep
        else 0
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
    """Operator resolves a pending credential origin to a known account.

    The target must be a configured account or a discovered identity with
    live credential or usage evidence for the same provider. The endpoint persists a
    :class:`CredentialTag` (scoped per ``scope`` — #319), clears the
    matching pending row(s), and writes an audit-log row. A
    deployment-wide scope clears every sidecar's pending row for the
    origin; a sidecar scope clears only that sidecar's.
    """
    from app.services.account_identity import canonical_account_id

    target_account_id = canonical_account_id(body.account_id)
    sidecar_id = normalize_sidecar_id(body.sidecar_id) if body.sidecar_id else ""
    if body.scope == "sidecar" and not sidecar_id:
        raise HTTPException(
            status_code=422,
            detail="sidecar_id is required for a machine-scoped tag (scope='sidecar').",
        )

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == body.provider_id,
            ProviderConfig.account_id == target_account_id,
        )
    ).first()
    discovered = False
    if row is None:
        # Discovered accounts intentionally have no ProviderConfig row. They
        # are valid assignment targets when there is live credential or usage
        # evidence for that exact provider/account pair.
        from app.models.db import LatestUsage

        discovered = (
            any(
                pid == body.provider_id and aid == target_account_id
                for pid, aid, _label in await token_cache.get_all_active_accounts()
            )
            or session.exec(
                select(LatestUsage.id).where(
                    LatestUsage.provider_id == body.provider_id,
                    LatestUsage.account_id == target_account_id,
                )
            ).first()
            is not None
        )
    if row is None and not discovered:
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
        account_id=target_account_id,
        sidecar_id=sidecar_id if body.scope == "sidecar" else None,
        set_by=getattr(request.state.auth, "actor", "operator")
        if hasattr(request.state, "auth")
        else "operator",
    )
    from app.models.db import CredentialSource

    source_stmt = select(CredentialSource).where(
        CredentialSource.provider_id == body.provider_id,
        CredentialSource.credential_origin == body.credential_origin,
    )
    if body.scope == "sidecar":
        source_stmt = source_stmt.where(CredentialSource.sidecar_id == sidecar_id)
    source_rows = list(session.exec(source_stmt).all())
    cache_moves: list[tuple[str, str, bool, int]] = []
    for source_row in source_rows:
        if source_row.account_id == target_account_id:
            continue
        previous_account_id = source_row.account_id
        target_source = session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == body.provider_id,
                CredentialSource.account_id == target_account_id,
                CredentialSource.source_id == source_row.source_id,
            )
        ).first()
        if target_source is None:
            source_row.account_id = target_account_id
            session.add(source_row)
            preference_row = source_row
        else:
            target_source.source_type = source_row.source_type
            target_source.source_label = source_row.source_label
            target_source.credential_origin = source_row.credential_origin
            target_source.sidecar_id = source_row.sidecar_id
            target_source.last_seen = source_row.last_seen
            target_source.health = source_row.health
            session.add(target_source)
            session.delete(source_row)
            preference_row = target_source
        cache_moves.append(
            (
                previous_account_id,
                source_row.source_id,
                preference_row.enabled,
                preference_row.priority,
            )
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

    from app.services.collector_manager import manager

    for previous_account_id, source_id, enabled, priority in cache_moves:
        await token_cache.move_source(
            body.provider_id, previous_account_id, target_account_id, source_id
        )
        old_preferences = manager._credential_source_preferences.get(
            (body.provider_id, previous_account_id)
        )
        if old_preferences is not None:
            old_preferences.pop(source_id, None)
        manager._credential_source_preferences.setdefault(
            (body.provider_id, target_account_id), {}
        )[source_id] = (enabled, priority)

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

    items = []
    for row in visible_rows:
        preview, observed_at, preview_stale = PendingCredentialTagRepo.read_quota_preview(row)
        items.append(
            {
                "sidecar_id": row.sidecar_id,
                "provider_id": row.provider_id,
                "credential_origin": row.credential_origin,
                "claimed_account_id": row.claimed_account_id,
                "first_seen": row.first_seen.isoformat() if row.first_seen else None,
                "last_seen": row.last_seen.isoformat() if row.last_seen else None,
                "quota_preview": preview,
                "quota_preview_observed_at": observed_at,
                "quota_preview_stale": preview_stale,
            }
        )

    return {
        "items": items,
        "counts_by_sidecar": counts_by_sidecar,
    }


@router.get("/events/pending")
async def list_pending_usage_events(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    rows = list(
        session.exec(
            select(PendingUsageEvent)
            .order_by(cast(Any, PendingUsageEvent.ts).desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    items = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        items.append(
            {
                "id": row.id,
                "provider_id": row.provider_id,
                "event_id": row.event_id,
                "sidecar_id": row.sidecar_id,
                "ts": row.ts.isoformat(),
                "reason": row.reason,
                "model_id": payload.get("model_id"),
                "session_id": payload.get("session_id"),
            }
        )
    return {
        "items": items,
        "total": session.exec(select(func.count()).select_from(PendingUsageEvent)).one(),
        "offset": offset,
        "limit": limit,
    }


@router.get("/events/pending/sessions")
async def list_pending_usage_sessions(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    sidecar_id: str | None = Query(None),
    provider_id: str | None = Query(None),
    search: str | None = Query(None, max_length=200),
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Group pending usage by provider, sidecar, and session for review."""
    rows = list(
        session.exec(
            select(PendingUsageEvent).order_by(cast(Any, PendingUsageEvent.ts).desc())
        ).all()
    )
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            # Missing session IDs cannot safely identify related events.
            key = (row.provider_id, row.sidecar_id, f"event:{row.id}")
            visible_session_id = None
        else:
            key = (row.provider_id, row.sidecar_id, f"session:{session_id}")
            visible_session_id = session_id

        group = groups.get(key)
        if group is None:
            group = {
                "provider_id": row.provider_id,
                "sidecar_id": row.sidecar_id,
                "session_id": visible_session_id,
                "event_ids": [],
                "event_count": 0,
                "first_ts": row.ts,
                "last_ts": row.ts,
                "model_ids": set(),
            }
            groups[key] = group
        group["event_ids"].append(row.id)
        group["event_count"] += 1
        group["first_ts"] = min(group["first_ts"], row.ts)
        group["last_ts"] = max(group["last_ts"], row.ts)
        model_id = payload.get("model_id")
        if isinstance(model_id, str) and model_id:
            group["model_ids"].add(model_id)

    all_groups = list(groups.values())
    sidecars = sorted({group["sidecar_id"] for group in all_groups})
    providers = sorted({group["provider_id"] for group in all_groups})
    needle = search.strip().casefold() if search else ""
    filtered = [
        group
        for group in all_groups
        if (sidecar_id is None or group["sidecar_id"] == sidecar_id)
        and (provider_id is None or group["provider_id"] == provider_id)
        and (
            not needle
            or needle in (group["session_id"] or "").casefold()
            or any(needle in model_id.casefold() for model_id in group["model_ids"])
        )
    ]
    ordered = sorted(filtered, key=lambda group: group["last_ts"], reverse=True)
    page = ordered[offset : offset + limit]
    items = [
        {
            **group,
            "first_ts": group["first_ts"].isoformat(),
            "last_ts": group["last_ts"].isoformat(),
            "model_ids": sorted(group["model_ids"]),
        }
        for group in page
    ]
    return {
        "items": items,
        "total_events": sum(group["event_count"] for group in all_groups),
        "matching_events": sum(group["event_count"] for group in ordered),
        "total_groups": len(ordered),
        "sidecars": sidecars,
        "providers": providers,
        "offset": offset,
        "limit": limit,
    }


async def _validate_pending_event_assignments(
    assignments: list[PendingEventAssignment], session: Session
) -> list[tuple[str, list[PendingUsageEvent]]]:
    event_ids = [event_id for assignment in assignments for event_id in assignment.event_ids]
    if not assignments or not event_ids or len(event_ids) > 10_000:
        raise HTTPException(status_code=422, detail="Select between 1 and 10000 events.")
    if any(not assignment.event_ids for assignment in assignments):
        raise HTTPException(status_code=422, detail="Each account assignment must include events.")
    if len(event_ids) != len(set(event_ids)):
        raise HTTPException(status_code=422, detail="An event can only appear once per batch.")

    # Discovered identities can be valid assignment targets even without a
    # provider_configs row (for example sidecar-managed Antigravity accounts).
    # Only accept those that are still known from the cache or latest_usage.
    active_account_keys = {
        (provider_id, resolve_account_id("", account_id, None))
        for provider_id, account_id, _label in await token_cache.get_all_active_accounts()
    }
    latest_account_keys = {
        (provider_id, resolve_account_id("", active_id, None))
        for provider_id, active_id in session.exec(
            select(LatestUsage.provider_id, LatestUsage.account_id).distinct()
        ).all()
        if active_id
    }
    providers_with_config = set(session.exec(select(ProviderConfig.provider_id).distinct()).all())
    rows = list(
        session.exec(
            select(PendingUsageEvent).where(cast(Any, PendingUsageEvent.id).in_(event_ids))
        ).all()
    )
    if len(rows) != len(event_ids):
        raise HTTPException(status_code=404, detail="One or more pending events were not found.")
    by_id = {row.id: row for row in rows}
    # Check mapping collisions before validating account configuration so a
    # malformed batch cannot partially populate the validated work list.
    future_tags: dict[tuple[str, str], str] = {}
    for assignment in assignments:
        account_id = resolve_account_id("", assignment.account_id, None)
        for event_id in assignment.event_ids:
            row = by_id[event_id]
            tag_key = (row.provider_id, row.sidecar_id)
            if tag_key in future_tags and future_tags[tag_key] != account_id:
                raise HTTPException(
                    status_code=422,
                    detail="A provider on one host can only be assigned to one account per batch.",
                )
            future_tags[tag_key] = account_id
    validated: list[tuple[str, list[PendingUsageEvent]]] = []
    assignable_accounts: dict[tuple[str, str], bool] = {}
    for assignment in assignments:
        account_id = resolve_account_id("", assignment.account_id, None)
        assignment_rows = [by_id[event_id] for event_id in assignment.event_ids]
        for row in assignment_rows:
            config_provider_id = account_config_provider_id(row.provider_id)
            account_key = (config_provider_id, account_id)
            if account_key not in assignable_accounts:
                configured = session.exec(
                    select(ProviderConfig).where(
                        ProviderConfig.provider_id == config_provider_id,
                        ProviderConfig.account_id == account_id,
                    )
                ).first()
                if configured is not None:
                    assignable_accounts[account_key] = (
                        configured.enabled and not configured.archived
                    )
                else:
                    assignable_accounts[account_key] = account_key in active_account_keys or (
                        config_provider_id not in providers_with_config
                        and account_key in latest_account_keys
                    )
            assignable = assignable_accounts[account_key]
            if not assignable:
                raise HTTPException(
                    status_code=404,
                    detail=f"No active account {account_id!r} known for {row.provider_id!r}.",
                )
        validated.append((account_id, assignment_rows))
    return validated


def _promote_pending_event_rows(
    request: Request,
    session: Session,
    rows: list[PendingUsageEvent],
    account_id: str,
    *,
    clear_pending: bool = True,
    record_audit: bool = True,
) -> None:
    # Ingest is idempotent. Keep pending rows until every promotion succeeds;
    # a retry after a partial failure safely deduplicates already-promoted rows.
    pushes_by_sidecar: dict[str, list[UsageEventPush]] = {}
    event_key_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        event_key = (row.provider_id, row.event_id)
        event_key_counts[event_key] = event_key_counts.get(event_key, 0) + 1
    ingestor = EventIngestor(session)
    for row in rows:
        payload = UsageEventPush.model_validate_json(row.payload_json).model_copy(
            update={"account_id": account_id, "account_source": "tag"}
        )
        # OpenCode doesn't identify which credential origin handled a
        # message. Persist an explicit provider-level tag scoped to the
        # source sidecar so later messages use the operator's resolution.
        CredentialTagRepo.set_tag(
            session,
            provider_id=row.provider_id,
            credential_origin=CredentialTagRepo.provider_origin(row.provider_id),
            account_id=account_id,
            sidecar_id=row.sidecar_id,
            set_by="operator",
        )
        existing = session.exec(
            select(UsageEvent).where(
                UsageEvent.provider_id == row.provider_id,
                UsageEvent.event_id == row.event_id,
            )
        ).first()
        # A newly resolved push from another sidecar may have arrived while
        # this pending copy was waiting. Reuse its sidecar identity so the
        # explicit operator assignment can safely move that event and its
        # rollups instead of being mistaken for a competing host replay.
        promotion_sidecar = existing.sidecar_id if existing is not None else row.sidecar_id
        if event_key_counts[(row.provider_id, row.event_id)] > 1:
            # Keep duplicate replays sequential: a later copy must observe
            # the first promoted row and reuse its sidecar identity.
            ingestor.ingest([payload], sidecar_id=promotion_sidecar)
        else:
            pushes_by_sidecar.setdefault(promotion_sidecar, []).append(payload)
    for sidecar_id, pushes in pushes_by_sidecar.items():
        ingestor.ingest(pushes, sidecar_id=sidecar_id)
    if clear_pending:
        for row in rows:
            session.delete(row)
        session.commit()
    if record_audit:
        audit_log.record(
            session,
            request,
            action="usage.pending_events_assigned",
            target_id=f"{rows[0].provider_id if rows else ''}/{account_id}",
            payload={"event_count": len(rows)},
        )


@router.post("/events/pending/assign")
async def assign_pending_usage_events(
    request: Request,
    body: PendingEventAssignment,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    if not body.event_ids or len(body.event_ids) > 1000:
        raise HTTPException(status_code=422, detail="Select between 1 and 1000 events.")
    validated = await _validate_pending_event_assignments([body], session)
    account_id, rows = validated[0]
    _promote_pending_event_rows(request, session, rows, account_id)
    return {"assigned": len(rows), "provider_id": rows[0].provider_id if rows else None}


@router.post("/events/pending/assign-batch")
async def assign_pending_usage_events_batch(
    request: Request,
    body: PendingEventBatchAssignment,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    validated = await _validate_pending_event_assignments(body.assignments, session)
    for account_id, rows in validated:
        _promote_pending_event_rows(
            request, session, rows, account_id, clear_pending=False, record_audit=False
        )
    for _account_id, rows in validated:
        for row in rows:
            session.delete(row)
    session.commit()
    assigned = sum(len(rows) for _account_id, rows in validated)
    providers = sorted({row.provider_id for _account_id, rows in validated for row in rows})
    mapping_targets = sorted(
        {
            (row.provider_id, row.sidecar_id, account_id)
            for account_id, rows in validated
            for row in rows
        }
    )
    audit_log.record(
        session,
        request,
        action="usage.pending_events_batch_assigned",
        target_id=",".join(providers),
        payload={"event_count": assigned, "assignment_count": len(validated)},
    )
    return {
        "assigned": assigned,
        "providers": providers,
        "mappings": [
            {"provider_id": provider_id, "sidecar_id": sidecar_id, "account_id": account_id}
            for provider_id, sidecar_id, account_id in mapping_targets
        ],
    }


def _get_active_identities(_session: Session) -> dict[str, str]:
    """Deprecated compatibility field: never infer a host identity from usage."""
    return {}


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
    cfg = session.exec(select(SystemConfig)).first()
    update_channel = (cfg.sidecar_update_channel if cfg else None) or "stable"
    return {"sidecars": [fleet_registry.to_dict(row, update_channel) for row in rows]}


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
    cfg = session.exec(select(SystemConfig)).first()
    update_channel = (cfg.sidecar_update_channel if cfg else None) or "stable"
    return fleet_registry.to_dict(row, update_channel)


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
    cfg = session.exec(select(SystemConfig)).first()
    update_channel = (cfg.sidecar_update_channel if cfg else None) or "stable"
    return fleet_registry.to_dict(row, update_channel)


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
