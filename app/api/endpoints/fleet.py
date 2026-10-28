import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import or_, text
from sqlmodel import Session, col, func, select

from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.login_dirs import LOGIN_DIR_KEYS, stored_login_dirs, validate_directories
from app.core.rate_limit import limiter
from app.core.security import (
    is_loopback_bind,
    require_admin_key,
    validate_ingest_auth,
    verify_config_signature,
)
from app.core.source_apps import HERMES, MINIMAX_CODE
from app.core.utils import scrub_log
from app.models._datetime import iso_utc
from app.models.db import (
    CredentialSource,
    CredentialTag,
    LatestUsage,
    PendingCredentialTag,
    PendingUsageEvent,
    ProviderConfig,
    SidecarRegistry,
    SystemConfig,
    UsageEvent,
)
from app.models.schemas import IngestRequest, UsageEventPush
from app.services import audit_log, pairing, sidecar_devices
from app.services.account_identity import (
    FINGERPRINTED_ORIGIN_PROVIDERS,
    account_config_provider_id,
    allowed_assignment_provider_ids,
    canonical_account_id,
    credential_fingerprint,
    jwt_stable_subject,
    keyed_credential_origin,
    normalize_sidecar_id,
    resolve_account_id,
)
from app.services.account_merges import resolve_merged_account
from app.services.accumulator import (
    prune_stale_latest_usage,
    reconcile_latest_usage_snapshot,
    upsert_latest_usage,
)
from app.services.blocked_sources import SOURCE_REASONS, is_source_blocked
from app.services.credential_tags import (
    CredentialTagRepo,
    ParkedMapping,
    PendingCredentialTagRepo,
    live_sidecar_ids,
)
from app.services.credential_token import issue_credential_token
from app.services.event_ingestor import EventIngestor, redirect_push
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
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)
# Sidecar extractors that collect usage *events* but have no provider row, card
# or credential (the events are attributed to the real providers they call).
# Scheduling-only: added to poll_providers so the extractor runs on every poll
# cycle, never listed as a provider. Kept in step with the sidecar overlay's
# `add_providers` by tests/unit/test_sidecar_only_event_providers.py.
#
# ``minimax_code`` is the MiniMax Code runtime ledger: a client app whose
# usage lives in a local SQLite ledger, not an account. It gets its own id so
# its collection does not depend on the host having a MiniMax *account* — a
# provider config row or a quota card (#928). Its events carry
# ``provider_id="minimax"`` and are attributed to the operator's MiniMax
# account, so it is a scheduling id only and never a provider here.
SIDECAR_ONLY_EVENT_PROVIDERS: frozenset[str] = frozenset({HERMES, MINIMAX_CODE})

router = APIRouter()


def _validation_summary(exc: Exception) -> str:
    """Field paths and error types only, never the offending values.

    A Pydantic error's default text echoes the rejected input, and on the ingest
    routes that input can be a credential. The text goes into both the 400
    response and the log, and the sidecar logs the response body it gets back.
    """
    if isinstance(exc, ValidationError):
        problems = [
            f"{'.'.join(str(part) for part in err['loc']) or '<body>'}: {err['type']}"
            for err in exc.errors(include_input=False, include_url=False, include_context=False)
        ]
        return "; ".join(problems[:10]) or "validation failed"
    return type(exc).__name__


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
        "oauth_account_uuid",
        "oauth_read_only",
        "xai_access",
        "xai_refresh",
        "cli_access_token",
        "cli_expires_at",
        # The installed agy CLI's version, reported next to the token. Not a
        # secret: the Code Assist API gates the quota pools it serves on the
        # User-Agent version, so the collector needs the real one (#687).
        "agy_version",
        "session_cookie",
        "console_session",
    }
)
# Cookie-family keys that carry browser-session secrets (``cookie_<name>`` for
# named cookies, plus the two bundled-cookie fields above).
_COOKIE_BUNDLE_KEYS = frozenset({"session_cookie", "console_session"})


def _can_reconcile_row(
    provider_id: str,
    row_account_id: str,
    target_account_id: str,
    phantom_account_ids: set[str] | None = None,
) -> bool:
    """May ingest move/delete a source row filed under ``row_account_id`` now that the
    source resolves to ``target_account_id``?

    A source belongs to exactly one account, so a row elsewhere is stale — but how much
    ingest may touch differs by provider. Anthropic reconciles every other account (its
    sources used to be filed under token-derived placeholder identities). Everyone else
    only retires the ``default`` placeholder the manifest may have created before the
    real identity was known, plus any *phantom* account — one with no configuration,
    quota card, usage or operator-set tag/label, i.e. the id an account was renamed away
    from (a credential-hash id later re-keyed to its email label, typically). A row an
    operator moved to another account is not ours to touch here;
    ``phantom_account_ids`` is computed by the caller, once per provider per request.
    """
    if row_account_id == target_account_id:
        return False
    if provider_id == "anthropic" or row_account_id == "default":
        return True
    return row_account_id in (phantom_account_ids or ())


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
    # Provider owning ``account_id`` when it isn't the event's own provider
    # (e.g. Gemini usage assigned to an Antigravity account).
    target_provider_id: str | None = None


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
    body_bytes = await validate_ingest_auth(request, x_signature, x_timestamp, session=session)

    # 4. Parse request
    try:
        payload = IngestRequest.model_validate_json(body_bytes)
    except Exception as e:
        detail = _validation_summary(e)
        logger.error("Failed to parse ingest payload: %s", detail)
        raise HTTPException(status_code=400, detail=f"Invalid payload: {detail}") from None

    # Normalize the originating sidecar id once, here at the chokepoint, so the
    # registry upsert, the per-card propagation, and every ingested event all key
    # off the same stable id. Collapses a host that flips between its FQDN and
    # `.local`/short name (e.g. macbook.local ⇄ Macbook.in.example.de) onto one
    # registry entry regardless of the sidecar binary version.
    if payload.sidecar_id:
        payload.sidecar_id = normalize_sidecar_id(payload.sidecar_id)

    from app.services import sidecar_devices, source_controls

    payload.sidecar_id = sidecar_devices.authenticated_registry_id(request, payload.sidecar_id)
    enrolled_device = getattr(request.state, "sidecar_device_id", None)
    source_proofs: dict[tuple[str, str], dict[str, Any] | None] = {}
    original_card_count = len(payload.metrics)
    accepted_cards = []
    for card in payload.metrics:
        if enrolled_device is not None:
            card.sidecar_id = payload.sidecar_id
            if card.remaining == "Token" and card.unit in ("oauth", "api_key", "cookie"):
                metadata = card.metadata or {}
                provider = card.provider_id or metadata.get("provider_id", "")
                origin = metadata.get("credential_origin", "")
                try:
                    source_proofs[(provider, origin)] = source_controls.contribution_proof(
                        session, enrolled_device, provider, origin, metadata.get("keychain_consent")
                    )
                except ValueError:
                    # A newer or withdrawn source must not discard unrelated usage.
                    # Do not touch tags, cache entries or source metadata for this card.
                    continue
        elif card.sidecar_id:
            sidecar_devices.authenticated_registry_id(request, card.sidecar_id)
        accepted_cards.append(card)
    payload.metrics = accepted_cards

    # A mapping onto an archived account is *parked*: it resolves the credential
    # but must never stamp the archived account with new usage. The predicate is
    # tag-aware — a redirect tag names an account of its *target* provider, so
    # its parked-ness is looked up there.
    parked = ParkedMapping(
        session,
        [
            pid
            for pid in (
                card.provider_id or (card.metadata.get("provider_id") if card.metadata else None)
                for card in payload.metrics
            )
            if isinstance(pid, str) and pid
        ],
    )

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
                # A merged-away account id was retired by the operator: the
                # account — and the credential that belonged to it — now
                # belongs to the merge's destination. A machine that still
                # claims the old id must not bring that account back.
                if acc_id:
                    acc_id = resolve_merged_account(session, provider_id, acc_id)
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
                tagged = None
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
                    if (
                        verified_tag is not None
                        and verified_tag.set_by != "identity_claim"
                        and not parked.is_parked(verified_tag)
                    ):
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
                    if (
                        tagged is not None
                        and tagged.set_by != "identity_claim"
                        and not parked.is_parked(tagged)
                    ):
                        # The tag may name an account that was merged away (the
                        # shared `default` identity keeps its tags through a
                        # merge): resolve it the same way as a claim.
                        acc_id = resolve_merged_account(session, provider_id, tagged.account_id)
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

                if provider_id == "kimi_coding" and provider_tokens.get("cli_access_token"):
                    from app.core.kimi_device import normalize_device_id

                    device_id = normalize_device_id((card.metadata or {}).get("cli_device_id"))
                    if device_id:
                        provider_tokens["cli_device_id"] = device_id

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
                    if (
                        provider_id == "anthropic"
                        and provider_tokens.get("oauth_read_only") == "1"
                        and credential_origin
                        and (
                            tagged is None
                            or tagged.set_by
                            in (None, "identity_claim", "identity_verification", "rotation")
                        )
                    ):
                        from app.services.credential_sources import sidecar_source_id

                        # Native slots survive login changes. A previous inferred tag
                        # cannot prove a newly arrived bearer (or re-resolve one still
                        # pending). Keep it isolated until profile+quota verification.
                        native_source_id = sidecar_source_id(
                            payload.sidecar_id or "local", credential_origin
                        )
                        still_pending = any(
                            candidate["source_id"] == native_source_id
                            for candidate in await token_cache.get_pending_sources(provider_id)
                        )
                        if still_pending or not await _bundle_holds(
                            provider_id, acc_id or "default", native_source_id, provider_tokens
                        ):
                            identity_pending = True
                            acc_id = "default"
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
                keep_alive=payload.keep_alive,
                keep_alive_providers=payload.keep_alive_providers,
                auto_update=payload.auto_update,
                update_channel=payload.update_channel,
                log_level=payload.log_level,
                collection_errors=payload.collection_errors,
                last_log_lines=payload.last_log_lines or [],
                identity_sources=payload.identity_sources,
                login_dirs_report=payload.login_dirs_report,
            )
        except Exception as _e:
            logger.warning(f"Fleet registry upsert failed for '{payload.sidecar_id}': {_e}")

    # Store tokens in cache for each identified account
    tokens_received_count = 0

    # Accounts this request's providers have credential rows for but no evidence
    # of — computed at most once per provider (see `_can_reconcile_row`), and
    # only when a source turns out to be filed under another account.
    phantom_cache: dict[str, set[str]] = {}

    def _phantoms_for(provider_id: str) -> set[str]:
        if provider_id not in phantom_cache:
            from app.services.credential_sources import phantom_accounts

            phantom_cache[provider_id] = phantom_accounts(session, provider_id=provider_id)
        return phantom_cache[provider_id]

    for p_id, p_tokens, a_id, a_name, origin, identity_pending in tokens_to_store:
        sidecar_id = payload.sidecar_id or "local"
        from app.services.credential_sources import (
            describe_origin,
            pending_cache_slot,
            reset_source_retry,
            retire_superseded_keyed_origins,
            retire_unkeyed_origin,
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
        cache_account_id = pending_cache_slot(p_id, source_id) if identity_pending else a_id
        # A retired account holds no credential: the source record stays (the
        # operator still sees the machine reporting it) but the secret is not
        # kept — not in the cache, not at rest. The same predicate the
        # inventory's marker, the refresher's rule and the parked-mapping check
        # use, so the four can never disagree. A credential whose identity is
        # still pending belongs to no account at all, so the rule does not
        # apply to it.
        retired_account = not identity_pending and parked.is_parked_account(
            p_id, cache_account_id or ""
        )
        if p_id in ("anthropic", "anthropic_console") and not identity_pending:
            # A pending source is cache-keyed by its stable source_id. Retire
            # that placeholder even if its durable CredentialSource row was
            # lost; otherwise its OAuth bundle survives beside the resolved one.
            await token_cache.remove_source(p_id, source_id, source_id, retire_matching_oauth=True)
        bundle_changed = (
            False
            if retired_account
            else bool(origin)
            and not await _bundle_holds(p_id, cache_account_id or "default", source_id, p_tokens)
        )
        secret_changed = bool(identity_pending and bundle_changed)
        if retired_account:
            # Keep the record, drop the secret: nothing is accepted for a
            # retired account, so there is nothing to retry either.
            actual_acc_id = cache_account_id or a_id or "default"
        elif enrolled_device is not None:
            actual_acc_id = cache_account_id or "default"
            if p_id == "anthropic" and (
                p_tokens.get("oauth_read_only") == "1"
                or bool(p_tokens.get("refresh_token"))
                or (origin or "").startswith(("file:", "keychain:"))
            ):
                p_tokens = {key: value for key, value in p_tokens.items() if key != "refresh_token"}
                p_tokens["oauth_read_only"] = "1"
            try:
                source_controls.save_contribution(
                    session,
                    enrolled_device,
                    source_proofs[(p_id, origin or "")],
                    provider_id=p_id,
                    origin=origin or "",
                    account_id=actual_acc_id,
                    tokens=p_tokens,
                    metadata={
                        "source_type": source_type,
                        "source_label": source_label,
                        "credential_origin": origin,
                        "sidecar_id": payload.sidecar_id,
                        "account_label": a_name,
                        "identity_pending": identity_pending,
                        "identity_hint": a_name if identity_pending else None,
                    },
                )
            except ValueError:
                session.rollback()
                raise HTTPException(409, "Source contribution not authorized") from None
        else:
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
        if bundle_changed:
            # A re-login (or a pasted new key) ends a rejected source's rest period: it is a
            # different credential now, so it is retried at once instead of at its old backoff.
            from app.services.collector_manager import manager as _collector_manager

            reset_source_retry(session, provider_id=p_id, source_id=source_id)
            _collector_manager.clear_source_retry(p_id, source_id)
        if secret_changed and origin:
            # A re-login (or first sighting): the identity verifier retries it next cycle
            # instead of waiting out the backoff earned by the previous secret.
            PendingCredentialTagRepo.reset_verification(
                session,
                sidecar_id=sidecar_id,
                provider_id=p_id,
                credential_origin=origin,
            )
        # Anthropic reconciles every other account this source was filed under
        # (token-derived placeholder identities). Other providers only retire the
        # ``default`` placeholder the manifest may have created before ingest
        # resolved the real identity, and rows on an account nothing collects for
        # any more — a source belongs to exactly one account, but an operator-moved
        # row on a live account is not ours to touch here.
        phantoms = (
            _phantoms_for(p_id)
            if any(row.account_id != actual_acc_id for row in prior_sources)
            else set()
        )
        replaceable = [
            row
            for row in prior_sources
            if _can_reconcile_row(p_id, row.account_id, actual_acc_id, phantoms)
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
        if origin:
            # A key-scoped origin supersedes the plain one the same credential used to
            # be reported under; drop that stale row (and its cached bundle).
            retired_rows = retire_unkeyed_origin(
                session, provider_id=p_id, sidecar_id=sidecar_id, origin=origin
            )
            # xAI re-keys on every refresh: earlier ``base#<old fp>`` rows are dead too (#523).
            retired_rows += retire_superseded_keyed_origins(
                session, provider_id=p_id, sidecar_id=sidecar_id, origin=origin
            )
            # Several stale rows can pile up there; the newest one carries the operator's
            # current choices, so it is the one the successor inherits from.
            retired_rows.sort(key=lambda row: row.last_seen, reverse=True)
            if retired_rows:
                from app.services.collector_manager import manager

                if not prior_sources:
                    # The keyed row is new this cycle: it inherits the operator's choices
                    # (enabled, failover priority) from the row it replaces instead of
                    # silently re-enabling a source they turned off. A machine reports one
                    # key per plain origin, so there is normally a single retired row; xAI can
                    # leave many (one per rotation), newest first. Prefer the one filed under
                    # the resolved account.
                    inherited = next(
                        (row for row in retired_rows if row.account_id == actual_acc_id),
                        retired_rows[0],
                    )
                    successor = session.exec(
                        select(CredentialSource).where(
                            CredentialSource.provider_id == p_id,
                            CredentialSource.source_id == source_id,
                            CredentialSource.account_id == actual_acc_id,
                        )
                    ).first()
                    if successor is not None:
                        successor.enabled = inherited.enabled
                        successor.priority = inherited.priority
                        session.add(successor)
                        manager.set_credential_source_preference(
                            p_id, actual_acc_id, source_id, inherited.enabled, inherited.priority
                        )
                for retired in retired_rows:
                    await token_cache.remove_source(p_id, retired.account_id, retired.source_id)
                    manager.drop_credential_source_preference(
                        p_id, retired.account_id, retired.source_id
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
    keep_alive_desired: bool | None = None
    keep_alive_desired_providers: dict[str, bool] = {}
    fleet_channel = (sys_cfg.sidecar_update_channel if sys_cfg else None) or "stable"
    fleet_auto_update = bool((sys_cfg.sidecar_auto_update if sys_cfg else None) or False)
    update_channel = fleet_channel
    auto_update = fleet_auto_update
    log_level_desired: str | None = None
    login_dirs = stored_login_dirs(None)
    if payload.sidecar_id:
        desired_row = session.get(SidecarRegistry, payload.sidecar_id)
        if desired_row is not None:
            update_channel = effective_update_channel(desired_row, fleet_channel)
            auto_update = effective_auto_update(desired_row, fleet_auto_update)
            log_level_desired = desired_row.log_level_desired
            login_dirs = stored_login_dirs(desired_row.login_dirs_desired)
        keep_alive_desired = desired_row.keep_alive_desired if desired_row else None
        keep_alive_desired_providers = parse_provider_flags(
            desired_row.keep_alive_desired_providers if desired_row else None
        )
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

            # Sidecar-only event providers (Hermes) are neither configured nor
            # in latest_usage — their events are filed under the real providers
            # they call — so without this their extractor runs only on a full
            # cycle. A scheduling id only: never a provider, card or UI row.
            provider_intervals = (
                [
                    (row.provider_id, row.poll_interval_seconds or global_interval)
                    for row in enabled_provider_rows
                ]
                + [(pid, global_interval) for pid in sorted(passive_pids)]
                + [(pid, global_interval) for pid in sorted(SIDECAR_ONLY_EVENT_PROVIDERS)]
            )

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
        "credentials_rejected": original_card_count - len(payload.metrics),
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
        # Operator's remote keep-alive setting; null leaves the sidecar's own flag in charge.
        "keep_alive_desired": keep_alive_desired,
        # Per-login overrides on top of it ({provider_id: bool}); only overridden logins appear.
        "keep_alive_desired_providers": keep_alive_desired_providers,
        "identities": _get_active_identities(
            session
        ),  # For sidecar identity propagation (legacy single-value)
        "reset_anchors": _reset_anchors_for_sidecar(session),  # Phase 6
        # Update channel the sidecar should track for its "update available"
        # check ("stable" | "beta" | "edge"). The dashboard owns this setting.
        # The sidecar's own override, else the fleet's.
        "sidecar_update_channel": update_channel,
        # Opt-in auto-update flag (this sidecar's override, else the fleet's); a sidecar's
        # explicit local config wins.
        "sidecar_auto_update": auto_update,
        # Operator's log-level override (raised from the dashboard while debugging); null leaves
        # the sidecar at its own configured level.
        "log_level": log_level_desired,
        "login_dirs": login_dirs,
        # Fleet-wide keep-alive default: on for sidecars without their own override. It only
        # ever turns keep-alive on (the sidecar ORs it with its local flag).
        "keep_alive_fleet_default": (sys_cfg.sidecar_keep_alive_default if sys_cfg else None)
        or False,
        # One-shot: self-update immediately on this heartbeat (admin pushed it).
        "update_now": update_now,
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
        # xAI's pasted bearer is a JWT that rotates; the sidecar keys xAI
        # origins on its stable claim (#523), so the hint must too.
        subject = jwt_stable_subject(api_key) if row.provider_id == "xai" else None
        fingerprint = credential_fingerprint(subject or api_key)
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


_VOLATILE_TOKEN_KEYS = {"expiry_date", "expires_at", "expires_in", "access_token"}


async def _bundle_holds(
    provider_id: str, cache_account_id: str, source_id: str, tokens: dict[str, str]
) -> bool:
    """True when the cached bundle already holds the secret being pushed.

    A re-login changes the secret; a heartbeat repeats it, and an access token that rotates
    under a constant refresh token (or a new expiry) is the same login, so only the stable
    part is compared: the refresh token when there is one, otherwise every pushed value
    except expiries.
    """
    stable = (
        {"refresh_token": tokens["refresh_token"]}
        if tokens.get("refresh_token")
        else {k: v for k, v in tokens.items() if k not in _VOLATILE_TOKEN_KEYS}
    )
    if not stable:
        # Nothing but an access token, with no refresh token behind it: that token is the
        # secret, so a different one is a different login.
        stable = {k: v for k, v in tokens.items() if k == "access_token"}
    # The bundle sits in the account slot the push resolves to, which ingest cannot always
    # know up front (a push without an account lands under its resolved identity): look in the
    # given slot first, then wherever else this source is filed.
    candidates = await token_cache.get_source_candidates(provider_id, cache_account_id)
    candidates = candidates + await token_cache.get_account_source_candidates(provider_id)
    for candidate in candidates:
        if candidate["source_id"] == source_id:
            held = candidate["tokens"]
            return all(held.get(key) == value for key, value in stable.items())
    return False


def _account_tag_hints_for_providers(
    session: Session,
    providers: list[str],
    *,
    sidecar_id: str | None = None,
    include_parked: bool = True,
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

    ``include_parked=False`` drops the mappings whose target account is
    archived (see :class:`ParkedMapping`). A parked mapping
    resolves the credential but must never reach a sidecar: shipping it
    would make the sidecar stamp the archived account with new usage
    and resurrect a pair the operator retired. Callers that decide
    whether an origin is *resolved* — ``visible_pending_rows`` — keep
    the default, because a parked mapping does resolve the origin and
    the operator must not be nagged about it again.

    The map is **complete per provider**: every origin the server
    resolves for a provider it is asked about is in it. A caller that
    hands it to a sidecar as "the answer for these providers" may
    therefore replace what it holds for them — the manifest response
    does exactly that, and adds an explicit empty bucket for a provider
    with no hints at all.
    """
    effective_sidecar_id = sidecar_id
    if effective_sidecar_id is None:
        live = live_sidecar_ids(session)
        if len(live) == 1:
            effective_sidecar_id = live[0]
    tags = CredentialTagRepo.list_effective_tags(
        session, providers=providers, sidecar_id=effective_sidecar_id
    )
    resolved: dict[str, dict[str, str]] = {}
    for (pid, origin), tag in tags.items():
        resolved.setdefault(pid, {})[origin] = tag.account_id
    # A fingerprint match outranks the auto-hint but never an operator tag:
    # the tag is a deliberate per-origin choice, the fingerprint a derived
    # one. Unlike tags, the fingerprint hint is not scoped to a sidecar —
    # it doesn't need to be: only a sidecar that already holds the key can
    # build a matching fingerprint, so the match is the scoping.
    for pid, by_origin in _fingerprinted_credential_hints(session, providers).items():
        bucket = resolved.setdefault(pid, {})
        for origin, account_id in by_origin.items():
            bucket.setdefault(origin, account_id)
    if not include_parked:
        # A parked mapping resolves the credential but must never reach a
        # sidecar: shipping it would make the sidecar stamp the archived
        # account with new usage and resurrect a pair the operator retired.
        # The check is tag-aware — a redirect tag names an account of its
        # *target* provider, so its parked-ness is looked up there.
        parked = ParkedMapping(session, providers)
        # A redirect tag names an account of its *target* provider — seed those
        # too, so the whole check costs two queries instead of one per
        # provider.
        parked.seed(
            {tag.target_provider_id for tag in tags.values() if tag.target_provider_id is not None}
        )
        for (pid, origin), tag in tags.items():
            if parked.is_parked(tag):
                tagged_origins = resolved.get(pid)
                if tagged_origins is not None:
                    tagged_origins.pop(origin, None)
                    if not tagged_origins:
                        resolved.pop(pid, None)
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
    keychain_consent: dict[str, Any] | None = None


class CredentialManifestRequest(BaseModel):
    """Body shape for POST /fleet/credentials/manifest."""

    sidecar_id: str
    entries: list[dict[str, Any]] = []  # [{provider_id, credential_origin}, ...]
    # A provider is listed only when its collection completed successfully.
    # Older sidecars omit this field; their partial manifests are upsert-only.
    completed_providers: list[str] | None = None
    # Safe source metadata only: never include credential values.
    observations: list[CredentialHealthObservation] = Field(default_factory=list, max_length=256)
    # The providers whose hints this sidecar currently holds. The server
    # answers with the complete hint set for each of them — an empty map
    # included — so a hint the server no longer holds is dropped on the same
    # cycle instead of surviving until the next /fleet/config.
    # Bounded like ``observations``: every id becomes one term in the query
    # below and one bucket in the response.
    hint_providers: list[str] = Field(default_factory=list, max_length=256)


def _entry_reason(
    seen: dict[tuple[str, str], str | None], key: tuple[str, str], entry: dict[str, str]
) -> str | None:
    """Why the sidecar reported this origin. One origin can appear twice in a payload (a
    withheld token and untagged events); the louder reason wins whatever the entry order."""
    reason = entry.get("reason")
    reason = reason if isinstance(reason, str) and reason else None
    if seen.get(key) == "token_withheld":
        reason = "token_withheld"
    seen[key] = reason
    return reason


def _source_field(entry: dict[str, str], key: str) -> str | None:
    """One bounded, non-secret field of a source-blocked manifest entry."""
    value = entry.get(key)
    if isinstance(value, str) and value:
        return value[:200]
    return None


# Event sources that are never an account: Hermes calls other providers'
# accounts, so a credential origin reported for it has nothing to tag. Old
# sidecars still report ``provider:hermes``; the server drops it. Hermes names
# an upstream it cannot map ``hermes-<name>``, which has no provider row either.
# Mirrored by the sidecar's ``_is_pass_through_provider`` (a parity test pins them).
PASS_THROUGH_PROVIDERS: frozenset[str] = frozenset({"hermes"})
PASS_THROUGH_PROVIDER_PREFIX = "hermes-"


def is_pass_through_provider(provider_id: str) -> bool:
    """True for an event source that is never an account an operator can tag."""
    return provider_id in PASS_THROUGH_PROVIDERS or provider_id.startswith(
        PASS_THROUGH_PROVIDER_PREFIX
    )


def _record_pending_entry(
    session: Session,
    payload: CredentialManifestRequest,
    entry: dict[str, str],
    provider_id: str,
    origin: str,
    keep_by_provider: dict[str, set[str]],
    reasons_in_payload: dict[tuple[str, str], str | None],
) -> None:
    """Handle one reported credential origin: resolve it, or keep it pending.

    An origin an operator already assigned (or a source-verified identity already
    resolved) is not re-opened as pending on every heartbeat; a rotated credential
    carries its binding over instead of reopening a silent quota outage.

    A pass-through provider (Hermes) never has an account to tag: its entry is
    ignored, and a row an earlier report left behind is removed.
    """
    if is_pass_through_provider(provider_id):
        PendingCredentialTagRepo.delete(
            session,
            sidecar_id=payload.sidecar_id,
            provider_id=provider_id,
            credential_origin=origin,
        )
        return
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
        return
    claimed_id = entry.get("account_id")
    if existing_tag is None:
        carried = CredentialTagRepo.inherited_account_for_rotation(
            session,
            provider_id=provider_id,
            credential_origin=origin,
            sidecar_id=payload.sidecar_id,
            claimed_account_id=claimed_id if isinstance(claimed_id, str) else None,
        )
        if carried is not None:
            # The credential rotated — a CLI re-login re-fingerprints the
            # origin (#349) — but the location did not and exactly one
            # account was ever bound there: carry that binding over instead
            # of reopening a silent quota outage (#474).
            CredentialTagRepo.set_tag(
                session,
                provider_id=provider_id,
                credential_origin=origin,
                account_id=carried,
                sidecar_id=payload.sidecar_id,
                set_by="rotation",
            )
            PendingCredentialTagRepo.delete(
                session,
                sidecar_id=payload.sidecar_id,
                provider_id=provider_id,
                credential_origin=origin,
            )
            logger.info(
                "Carried the %s credential binding across a rotation: %s → %s",
                provider_id,
                scrub_log(origin),
                scrub_log(carried),
            )
            return
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
            return
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
    pending.reason = _entry_reason(reasons_in_payload, (provider_id, origin), entry)
    pending.claimed_account_id = (
        canonical_account_id(claimed_id)
        if provider_id == "anthropic" and isinstance(claimed_id, str) and "@" in claimed_id
        else None
    )
    session.add(pending)


def _record_source_blocks(
    session: Session,
    payload: CredentialManifestRequest,
    source_entries: list[dict[str, str]],
    keep_by_provider: dict[str, set[str]],
    reasons_in_payload: dict[tuple[str, str], str | None],
) -> int:
    """Record the credential sources a sidecar found but could not read.

    A browser cookie store the browser holds open, or one whose values are app-bound
    encrypted: there is no credential to map, so this never enters the pending-tag
    machinery. It records why the source is unavailable and nothing else, and it is
    pruned the cycle the sidecar stops reporting it.

    Returns the number of entries recorded.
    """
    recorded = 0
    for entry in source_entries:
        provider_id = entry.get("provider_id")
        origin = entry.get("credential_origin")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        if not isinstance(origin, str) or not origin:
            continue
        recorded += 1
        keep_by_provider.setdefault(provider_id, set()).add(origin)
        pending = PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=payload.sidecar_id,
            provider_id=provider_id,
            credential_origin=origin,
        )
        effective = _entry_reason(reasons_in_payload, (provider_id, origin), entry)
        pending.reason = effective
        if effective in SOURCE_REASONS:
            pending.source_store = _source_field(entry, "source_store")
            pending.source_detail = _source_field(entry, "source_detail")
            pending.source_remedy = _source_field(entry, "source_remedy")
        else:
            # A withheld token outranks the source report for this origin.
            pending.source_store = None
            pending.source_detail = None
            pending.source_remedy = None
        session.add(pending)
    return recorded


def _reported_manifest_origins(payload: CredentialManifestRequest) -> dict[str, set[str]]:
    """All positively reported sources, including resolved observation-only credentials."""
    reported: dict[str, set[str]] = {}
    for entry in payload.entries:
        provider, origin = entry.get("provider_id"), entry.get("credential_origin")
        if isinstance(provider, str) and provider and isinstance(origin, str) and origin:
            reported.setdefault(provider, set()).add(origin)
    for observation in payload.observations:
        reported.setdefault(observation.provider_id, set()).add(observation.credential_origin)
    return reported


async def _retire_missing_claude_sources(
    session: Session, payload: CredentialManifestRequest
) -> None:
    from app.services.collector_manager import manager
    from app.services.credential_sources import retire_missing_claude_cookie_sources

    retired_sources = retire_missing_claude_cookie_sources(
        session,
        sidecar_id=payload.sidecar_id,
        completed_providers=payload.completed_providers,
        keep_by_provider=_reported_manifest_origins(payload),
    )
    # No cache/preference eviction may escape a failed durable transaction.
    session.commit()
    for retired in retired_sources:
        await token_cache.remove_source(retired.provider_id, retired.account_id, retired.source_id)
        manager.drop_credential_source_preference(
            retired.provider_id, retired.account_id, retired.source_id
        )


def _validate_device_manifest(
    request: Request, payload: CredentialManifestRequest, session: Session
) -> None:
    from app.services import sidecar_devices

    payload.sidecar_id = (
        sidecar_devices.authenticated_registry_id(request, payload.sidecar_id) or ""
    )
    enrolled_device = getattr(request.state, "sidecar_device_id", None)
    if enrolled_device is not None:
        from app.services import source_controls

        rejected_providers = set()

        def authorized(entry: dict[str, Any]) -> bool:
            try:
                source_controls.contribution_proof(
                    session,
                    enrolled_device,
                    entry.get("provider_id", ""),
                    entry.get("credential_origin", ""),
                    entry.get("keychain_consent"),
                )
                return True
            except ValueError:
                rejected_providers.add(entry.get("provider_id", ""))
                return False

        payload.entries = [entry for entry in payload.entries if authorized(entry)]
        payload.observations = [
            entry for entry in payload.observations if authorized(entry.model_dump())
        ]
        # A skipped source is not authoritative absence and cannot retire last-good origins.
        if payload.completed_providers is not None:
            payload.completed_providers = [
                provider
                for provider in payload.completed_providers
                if provider not in rejected_providers
            ]


def _refresh_observed_sources(
    session: Session,
    payload: CredentialManifestRequest,
    parked: ParkedMapping,
) -> None:
    """File each observed credential origin under the account it belongs to.

    Each manifest is authoritative: the latest observed token names and
    expiry replace the prior health metadata for that source.

    ``parked`` answers whether a mapping is parked — one whose target account
    is archived. A parked mapping resolves the credential, but the observation
    must not be filed under the retired account, so the source stays where the
    machine's own resolution put it.
    """
    from app.services.credential_sources import (
        describe_origin,
        resolve_source_account,
        sidecar_source_id,
        touch_source,
    )

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
        if parked.is_parked(tag):
            # Parked: the operator parked this credential on a retired account.
            # Keep the source where the machine's own resolution filed it.
            tag = None
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
    body_bytes = await validate_ingest_auth(
        request, x_signature, x_timestamp, session=session, purpose="manifest"
    )
    try:
        payload = CredentialManifestRequest.model_validate_json(body_bytes)
    except Exception as exc:
        detail = _validation_summary(exc)
        logger.debug("manifest: invalid body: %s", detail)
        raise HTTPException(status_code=400, detail=f"Invalid manifest: {detail}") from None

    if not payload.sidecar_id:
        raise HTTPException(status_code=400, detail="sidecar_id is required")

    # Mirror /fleet/ingest: normalize the sidecar_id so a reporter that
    # sends an FQDN doesn't get a pending row keyed on a string no
    # card can surface in the per-sidecar badge query
    # (?sidecar_id=<registry id>). Defensive — today's sidecar normalizes
    # client-side, but a future reporter (custom integration, scripted
    # curl) might not (PR #290 round-2 review, Hermes suggestion #7).
    payload.sidecar_id = normalize_sidecar_id(payload.sidecar_id)

    _validate_device_manifest(request, payload, session)

    # A mapping onto an archived account is *parked*: it resolves the credential
    # but must never file an observation under the archived account.
    parked = ParkedMapping(
        session, [observation.provider_id for observation in payload.observations]
    )

    _refresh_observed_sources(session, payload, parked)
    if payload.observations:
        session.flush()

    keep_by_provider: dict[str, set[str]] = {}
    entries_received = 0
    reasons_in_payload: dict[tuple[str, str], str | None] = {}
    # An origin can be reported twice in one payload: once as a credential the
    # sidecar withheld, and once as a source it could not read. They are
    # different kinds of report, so they are handled separately - and the
    # withheld reason wins for the origin, because an operator still has to
    # assign an account for it.
    source_entries: list[dict[str, str]] = []
    normal_entries: list[dict[str, str]] = []
    for entry in payload.entries:
        reason = entry.get("reason")
        if isinstance(reason, str) and reason in SOURCE_REASONS:
            source_entries.append(entry)
        else:
            normal_entries.append(entry)
    for entry in normal_entries:
        provider_id = entry.get("provider_id")
        origin = entry.get("credential_origin")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        if not isinstance(origin, str) or not origin:
            continue
        entries_received += 1
        _record_pending_entry(
            session,
            payload,
            entry,
            provider_id,
            origin,
            keep_by_provider,
            reasons_in_payload,
        )

    # A credential source the sidecar found but could not read: a browser cookie
    # store the browser holds open, or one whose values are app-bound encrypted.
    entries_received += _record_source_blocks(
        session, payload, source_entries, keep_by_provider, reasons_in_payload
    )

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
    # The sidecar names the providers whose hints it holds, so the server also
    # answers for a provider whose tag was deleted — that provider has no tag
    # row left to make it a candidate, yet the sidecar may still hold its hint.
    # Deduplicated here: every id becomes one term in the query above and one
    # bucket in the response, so a repeated id must not be counted twice.
    candidate_provider_ids.update(
        pid for pid in dict.fromkeys(payload.hint_providers) if isinstance(pid, str) and pid
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
            if is_source_blocked(row) and row.credential_origin not in keep_by_provider.get(
                row.provider_id, set()
            ):
                # A row that records an unusable credential source is not a
                # credential waiting to be mapped: there is nothing for the
                # operator to tag. Its lifecycle is the sidecar's report, so it
                # goes when the sidecar stops reporting that store - not when the
                # provider is collected. On macOS the sidecar withholds a
                # provider with a native source from completed_providers (a
                # withheld scan never proves a logout), so the generic rule would
                # keep the row forever and the advisory would outlive the store
                # it describes. Leaving it out of the keep map retires it.
                continue
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
        if prune_keep or existing_rows
        else 0
    )
    await _retire_missing_claude_sources(session, payload)

    resolved = _account_tag_hints_for_providers(
        session, candidate_pids, sidecar_id=payload.sidecar_id, include_parked=False
    )
    # The answer for a provider is *complete*: an empty bucket says "this
    # provider has no hints", which is how a sidecar learns that a hint it
    # cached before the account was archived is no longer valid. Without the
    # explicit bucket the provider would simply be absent, and a sidecar that
    # merges the map would keep the stale hint until the next /fleet/config.
    for pid in candidate_pids:
        resolved.setdefault(pid, {})

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

    An **archived** target is a *parked* mapping: the operator parked this
    credential on an account they retired. The binding resolves the origin —
    it never becomes pending again and the operator is not nagged — but it
    is inert while the account stays archived: no hint ships to the
    sidecar and no new usage is stamped on the archived account. Restoring
    the account revives the binding automatically. The response reports
    which it was as ``target_archived``.
    """
    from app.services.account_identity import canonical_account_id
    from app.services.credential_sources import is_machine_bound_origin, merge_source_provenance

    if body.scope == "deployment" and is_machine_bound_origin(body.credential_origin):
        raise HTTPException(
            status_code=422,
            detail=(
                "Browser-cookie and keychain credentials belong to one machine; tag them "
                "with scope='sidecar'. An all-machines tag would follow an account switch "
                "on a host it was never made for."
            ),
        )
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

    # An archived target is a *parked* mapping: the operator parked this
    # credential on an account they retired. The binding resolves the origin
    # (the origin stops being pending and the operator is not nagged), but it
    # is inert — no hint ships, nothing is stamped on the archived account, and
    # restoring the account revives it automatically.
    target_archived = bool(row is not None and row.archived)

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
    source_rows = [] if target_archived else list(session.exec(source_stmt).all())
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
            merge_source_provenance(target_source, source_row)
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
            "parked": target_archived,
        },
    )

    return {"status": "ok", "target_archived": target_archived}


@router.get("/credentials/tags")
async def list_credential_tags(
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """List every resolved credential tag (both scopes).

    Once an origin is tagged it never shows up as pending again, so this
    is the only way for the operator to see — and correct — an existing
    mapping. ``sidecar_id`` is ``None`` for deployment-wide ("All
    machines") tags. ``last_matched_at`` / ``stale`` say whether a machine still reports a
    credential this rule resolves.
    """
    from app.services.credential_sources import describe_origin_full
    from app.services.maintenance.stale_credential_rules import find_stale_rules, rule_usage

    usage = rule_usage(session)
    stale_ids = {rule.row_id for rule in find_stale_rules(session, usage=usage)}

    return {
        "items": [
            {
                "provider_id": t.provider_id,
                "credential_origin": t.credential_origin,
                "origin_label": (origin := describe_origin_full(t.credential_origin)).label,
                "origin_app": origin.app,
                "origin_path": origin.path,
                "origin_detail": origin.detail,
                "account_id": t.account_id,
                "sidecar_id": t.sidecar_id,
                "set_by": t.set_by,
                "target_provider_id": t.target_provider_id,
                "set_at": t.set_at.isoformat() if t.set_at else None,
                # Freshest machine-reported source this rule resolved (None = never), and
                # whether the stale_credential_rules check would list it.
                "last_matched_at": (
                    usage[t.id].isoformat() if t.id is not None and t.id in usage else None
                ),
                "stale": t.id in stale_ids,
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


def visible_pending_rows(session: Session) -> list[PendingCredentialTag]:
    """Pending rows an operator still has to resolve: every row except those whose origin
    already has an effective hint (an explicit tag or an active single-account auto-hint).

    One definition for the Untagged list, its per-sidecar counts, and the credential inventory,
    so the badge, the list and the Home banner can never disagree about what is "untagged".

    A row that records an unusable credential source is not one of them: there is no
    credential to map, so it belongs to the "Unavailable" surface instead.
    """
    by_sidecar: dict[str, list[PendingCredentialTag]] = {}
    for r in PendingCredentialTagRepo.list_all(session):
        if is_source_blocked(r) or is_pass_through_provider(r.provider_id):
            continue
        by_sidecar.setdefault(r.sidecar_id, []).append(r)

    visible: list[PendingCredentialTag] = []
    for sc_id, sc_rows in by_sidecar.items():
        pids = sorted({r.provider_id for r in sc_rows})
        hints = _account_tag_hints_for_providers(session, pids, sidecar_id=sc_id)
        visible.extend(
            r for r in sc_rows if r.credential_origin not in hints.get(r.provider_id, {})
        )
    return visible


def _pending_organization_count(serialized: str | None) -> int:
    try:
        organizations = json.loads(serialized or "[]")
    except (TypeError, ValueError):
        return 0
    if not isinstance(organizations, list):
        return 0
    return sum(
        isinstance(org, dict)
        and isinstance(org.get("id"), str)
        and isinstance(org.get("name"), str)
        for org in organizations
    )


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
    from app.services.credential_sources import describe_origin_full, sidecar_source_id

    visible_rows = visible_pending_rows(session)
    counts_by_sidecar: dict[str, int] = {}
    for r in visible_rows:
        counts_by_sidecar[r.sidecar_id] = counts_by_sidecar.get(r.sidecar_id, 0) + 1

    if sidecar_id is not None:
        visible_rows = [r for r in visible_rows if r.sidecar_id == sidecar_id]

    items = []
    for row in visible_rows:
        preview, observed_at, preview_stale = PendingCredentialTagRepo.read_quota_preview(row)
        organization_count = _pending_organization_count(row.claude_organizations_json)
        described = describe_origin_full(row.credential_origin)
        items.append(
            {
                "sidecar_id": row.sidecar_id,
                "provider_id": row.provider_id,
                "credential_origin": row.credential_origin,
                # What the credential is, in words: "Browser cookie", "auth.json", ...
                "origin_label": described.label,
                # The tool that owns it: "Chrome" for a native Claude cookie,
                # "Codex CLI" for a file, ... The tags listing and the credential
                # inventory already send it.
                "origin_app": described.app,
                "origin_detail": described.detail,
                "claimed_account_id": row.claimed_account_id,
                **(
                    {"source_id": sidecar_source_id(row.sidecar_id, row.credential_origin)}
                    if row.provider_id in ("anthropic", "anthropic_console")
                    else {}
                ),
                "selection_required": organization_count > 1,
                "organization_count": organization_count,
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


class ClaudeOrganizationSelection(BaseModel):
    provider_id: Literal["anthropic", "anthropic_console"]
    organization_id: str


async def _pending_claude_source(
    session: Session, provider_id: str, source_id: str
) -> tuple[PendingCredentialTag, dict[str, Any]]:
    from app.services.credential_sources import sidecar_source_id

    if provider_id not in ("anthropic", "anthropic_console"):
        raise HTTPException(status_code=422, detail="Unsupported Claude provider")
    row = next(
        (
            r
            for r in session.exec(
                select(PendingCredentialTag).where(PendingCredentialTag.provider_id == provider_id)
            ).all()
            if sidecar_source_id(r.sidecar_id, r.credential_origin) == source_id
        ),
        None,
    )
    source = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == provider_id, CredentialSource.source_id == source_id
        )
    ).first()
    candidates = await token_cache.get_pending_sources(provider_id)
    candidate = next((c for c in candidates if c["source_id"] == source_id), None)
    if (
        row is None
        or source is None
        or candidate is None
        or source.credential_origin != row.credential_origin
        or source.sidecar_id != row.sidecar_id
        or candidate.get("credential_origin") != row.credential_origin
        or candidate.get("sidecar_id") != row.sidecar_id
    ):
        raise HTTPException(status_code=404, detail="Pending Claude source unavailable")
    if (
        CredentialTagRepo.get(
            session,
            provider_id=provider_id,
            credential_origin=row.credential_origin,
            sidecar_id=row.sidecar_id,
        )
        is not None
    ):
        raise HTTPException(status_code=409, detail="Credential source already assigned")
    return row, candidate


async def _verify_pending_claude_source(
    provider_id: str, candidate: dict[str, Any], organization_id: str | None = None
) -> Any:
    import httpx

    from app.services.claude_verification import verify_claude_cookie
    from app.services.collectors.anthropic_console import ConsoleAPIError

    tokens = candidate["tokens"]
    cookie = tokens.get("cookie_sessionKey") or tokens.get("session_cookie")
    if not cookie:
        raise HTTPException(status_code=409, detail="Claude cookie unavailable")
    try:
        async with (
            token_cache.using_source(
                provider_id, candidate["account_slot"], candidate["source_id"]
            ),
            httpx.AsyncClient(event_hooks={"response": [token_cache.observe_response]}) as client,
        ):
            return await verify_claude_cookie(client, provider_id, cookie, organization_id)
    except ConsoleAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/credentials/{source_id}/claude-organizations")
async def get_claude_source_organizations(
    source_id: str,
    provider_id: Literal["anthropic", "anthropic_console"],
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    row, candidate = await _pending_claude_source(session, provider_id, source_id)
    result = await _verify_pending_claude_source(provider_id, candidate)
    # Validate that the exact candidate survived the upstream calls before recording metadata.
    _current_row, current = await _pending_claude_source(session, provider_id, source_id)
    if current["tokens"] != candidate["tokens"]:
        raise HTTPException(status_code=409, detail="Claude source changed; retry")
    row.claude_organizations_json = json.dumps(result.organizations)
    row.claude_organizations_complete = result.complete
    session.add(row)
    session.commit()
    return {
        "organizations": result.organizations,
        "complete": result.complete,
        "selection_required": result.selection_required,
    }


@router.post("/credentials/{source_id}/claude-organizations")
async def select_claude_source_organization(
    source_id: str,
    body: ClaudeOrganizationSelection,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, str]:
    from app.services.claude_verification import organization_uuid
    from app.services.collector_manager import manager
    from app.services.collectors.anthropic_console import ConsoleAPIError

    try:
        organization_id = organization_uuid(body.organization_id)
    except ConsoleAPIError as exc:
        raise HTTPException(status_code=422, detail="Invalid Claude organization") from exc
    _pending_row, candidate = await _pending_claude_source(session, body.provider_id, source_id)
    result = await _verify_pending_claude_source(body.provider_id, candidate, organization_id)
    if not result.organization_id or not result.account_id:
        raise HTTPException(
            status_code=403, detail="Claude organization is not accessible with this source"
        )
    _current_row, current = await _pending_claude_source(session, body.provider_id, source_id)
    if current["tokens"] != candidate["tokens"]:
        raise HTTPException(status_code=409, detail="Claude source changed; retry")
    promoted = await manager._promote_source_identity(
        body.provider_id,
        "default",
        source_id,
        result.account_id,
        cache_account_id=candidate["account_slot"],
        expected_generation=token_cache.credential_generation(candidate["tokens"]),
        organization_id=organization_id,
        allow_selection=True,
        account_label=result.account_label,
        billing_type=result.billing_type,
    )
    if not promoted:
        raise HTTPException(status_code=409, detail="Claude source changed; retry")
    return {"status": "ok"}


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
    return _pending_usage_session_groups(
        session,
        offset=offset,
        limit=limit,
        sidecar_id=sidecar_id,
        provider_id=provider_id,
        search=search,
    )


# The pending table can hold tens of thousands of rows and the Fleet card
# polls this endpoint every minute, so the grouping happens in SQL. Loading
# every pending row into Python to group it there made the page slower the
# bigger the backlog got — and a bigger backlog is exactly what a backlog
# that cannot be drained in one request produces.
#
# The rows are grouped by provider, sidecar and session. A row without a
# usable session id stands alone: missing session ids cannot safely
# identify related events.
#
# SQLite: this query needs **SQLite 3.38 or newer**. It uses the JSON
# functions (native from 3.38, JSON1 before that) and `AS MATERIALIZED`
# (3.35+), which is what keeps the extracted rows in a transient table
# instead of being re-evaluated per reference — without it the plan
# collapses and a 50,000-row backlog takes minutes rather than seconds.
# The app runs against the *system* SQLite, not the one bundled with
# Python, so the minimum is pinned here and checked at startup
# (`app/core/db.py`): Debian 12 ships 3.40, macOS 14 ships 3.43, both
# fine. The query deliberately avoids `group_concat(... ORDER BY ...)`
# (3.44+) — the group's event ids are a set, not a sequence.
_PENDING_SESSION_GROUPS_SQL = text(
    r"""
    WITH extracted AS MATERIALIZED (
        SELECT
            id,
            provider_id,
            sidecar_id,
            ts,
            CASE
                WHEN NOT json_valid(payload_json) THEN 'event:' || id
                WHEN json_type(payload_json, '$.session_id') = 'text'
                     AND json_extract(payload_json, '$.session_id') <> ''
                    THEN 'session:' || json_extract(payload_json, '$.session_id')
                ELSE 'event:' || id
            END AS session_key,
            CASE
                WHEN NOT json_valid(payload_json) THEN NULL
                WHEN json_type(payload_json, '$.session_id') = 'text'
                     AND json_extract(payload_json, '$.session_id') <> ''
                    THEN json_extract(payload_json, '$.session_id')
                ELSE NULL
            END AS session_id,
            CASE
                WHEN NOT json_valid(payload_json) THEN NULL
                WHEN json_type(payload_json, '$.model_id') = 'text'
                     AND json_extract(payload_json, '$.model_id') <> ''
                    THEN json_extract(payload_json, '$.model_id')
                ELSE NULL
            END AS model_id
        FROM pending_usage_events
    ),
    matched AS (
        -- The groups the filters select. The search matches a row when the
        -- needle is in its session id or in its model id, so a group is
        -- selected when one of its rows matches.
        SELECT provider_id, sidecar_id, session_key
        FROM extracted
        WHERE (:sidecar_id IS NULL OR sidecar_id = :sidecar_id)
          AND (:provider_id IS NULL OR provider_id = :provider_id)
          AND (
              :search IS NULL
              OR lower(COALESCE(session_id, '')) LIKE :search ESCAPE '\'
              OR lower(COALESCE(model_id, '')) LIKE :search ESCAPE '\'
          )
        GROUP BY provider_id, sidecar_id, session_key
    ),
    groups AS (
        SELECT
            e.provider_id,
            e.sidecar_id,
            e.session_key,
            MIN(e.session_id) AS session_id,
            COUNT(*) AS event_count,
            MIN(e.ts) AS first_ts,
            MAX(e.ts) AS last_ts,
            -- The ids of a group are a set, not a sequence: the server
            -- promotes them together and the client sends them back as
            -- they came. Sorting them would need SQLite 3.44's
            -- `group_concat(... ORDER BY ...)`, which the system SQLite on
            -- Debian 12 and macOS 14 does not have.
            group_concat(e.id) AS event_ids,
            group_concat(DISTINCT e.model_id) AS model_ids
        FROM extracted e
        JOIN matched m
          ON m.provider_id = e.provider_id
         AND m.sidecar_id = e.sidecar_id
         AND m.session_key = e.session_key
        GROUP BY e.provider_id, e.sidecar_id, e.session_key
    ),
    page AS (
        -- The session key breaks ties, so a group cannot move between two
        -- pages while the operator pages through the backlog.
        SELECT * FROM groups ORDER BY last_ts DESC, session_key ASC
        LIMIT :limit OFFSET :offset
    ),
    counts AS (
        SELECT
            (SELECT COUNT(*) FROM groups) AS total_groups,
            (SELECT COALESCE(SUM(event_count), 0) FROM groups) AS matching_events,
            (SELECT COUNT(*) FROM pending_usage_events) AS total_events,
            (
                SELECT json_group_array(value)
                FROM (SELECT DISTINCT sidecar_id AS value FROM pending_usage_events ORDER BY sidecar_id)
            ) AS sidecars,
            (
                SELECT json_group_array(value)
                FROM (SELECT DISTINCT provider_id AS value FROM pending_usage_events ORDER BY provider_id)
            ) AS providers
    )
    SELECT
        p.provider_id,
        p.sidecar_id,
        p.session_id,
        p.event_count,
        p.first_ts,
        p.last_ts,
        p.event_ids,
        p.model_ids,
        c.total_groups,
        c.matching_events,
        c.total_events,
        c.sidecars,
        c.providers
    FROM counts c
    LEFT JOIN page p ON 1 = 1
    """
)


def _escape_like(value: str) -> str:
    """Escape the characters a LIKE pattern would read as wildcards.

    The search is a plain substring search, so a needle that contains a
    ``%`` or a ``_`` has to be taken literally.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pending_stored_ts(value: str) -> datetime:
    """Read a stored timestamp the way the ORM reads it."""
    return datetime.fromisoformat(value)


def _pending_usage_session_groups(
    session: Session,
    *,
    offset: int,
    limit: int,
    sidecar_id: str | None,
    provider_id: str | None,
    search: str | None,
) -> dict[str, Any]:
    """Group the pending usage events for the review table.

    The grouping runs in SQL (see :data:`_PENDING_SESSION_GROUPS_SQL`) so
    the page cost does not grow with the backlog.
    """
    needle = search.strip().casefold() if search else ""
    pattern = f"%{_escape_like(needle)}%" if needle else None
    rows = session.execute(
        _PENDING_SESSION_GROUPS_SQL,
        {
            "sidecar_id": sidecar_id,
            "provider_id": provider_id,
            "search": pattern,
            "limit": limit,
            "offset": offset,
        },
    ).all()

    items: list[dict[str, Any]] = []
    totals: dict[str, Any] = {
        "total_events": 0,
        "matching_events": 0,
        "total_groups": 0,
        "sidecars": [],
        "providers": [],
    }
    for row in rows:
        totals = {
            "total_events": int(row.total_events or 0),
            "matching_events": int(row.matching_events or 0),
            "total_groups": int(row.total_groups or 0),
            # Sorted here rather than in SQL: an aggregate's row order is
            # not guaranteed, and the list is the same either way.
            "sidecars": sorted(json.loads(row.sidecars)) if row.sidecars else [],
            "providers": sorted(json.loads(row.providers)) if row.providers else [],
        }
        if row.provider_id is None:
            # The counts row of an empty page.
            continue
        items.append(
            {
                "provider_id": row.provider_id,
                "sidecar_id": row.sidecar_id,
                "session_id": row.session_id,
                "event_ids": [int(value) for value in (row.event_ids or "").split(",") if value],
                "event_count": int(row.event_count or 0),
                "first_ts": iso_utc(_pending_stored_ts(row.first_ts)),
                "last_ts": iso_utc(_pending_stored_ts(row.last_ts)),
                "model_ids": sorted({value for value in (row.model_ids or "").split(",") if value}),
            }
        )
    return {
        "items": items,
        "total_events": totals["total_events"],
        "matching_events": totals["matching_events"],
        "total_groups": totals["total_groups"],
        "sidecars": totals["sidecars"],
        "providers": totals["providers"],
        "offset": offset,
        "limit": limit,
    }


async def _validate_pending_event_assignments(
    assignments: list[PendingEventAssignment], session: Session
) -> list[tuple[str, str, list[PendingUsageEvent]]]:
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
    future_tags: dict[tuple[str, str, str], tuple[str, str]] = {}
    for assignment in assignments:
        account_id = resolve_account_id("", assignment.account_id, None)
        for event_id in assignment.event_ids:
            row = by_id[event_id]
            target = assignment.target_provider_id or account_config_provider_id(row.provider_id)
            if target not in allowed_assignment_provider_ids(row.provider_id):
                raise HTTPException(
                    status_code=422,
                    detail=f"Account provider {target!r} is not a valid target for "
                    f"{row.provider_id!r} events.",
                )
            origin = json.loads(row.payload_json).get(
                "credential_origin"
            ) or CredentialTagRepo.provider_origin(row.provider_id)
            tag_key = (row.provider_id, row.sidecar_id, origin)
            if tag_key in future_tags and future_tags[tag_key] != (target, account_id):
                raise HTTPException(
                    status_code=422,
                    detail="A provider on one host can only be assigned to one account per batch.",
                )
            future_tags[tag_key] = (target, account_id)
    validated: list[tuple[str, str, list[PendingUsageEvent]]] = []
    assignable_accounts: dict[tuple[str, str], bool] = {}
    for assignment in assignments:
        account_id = resolve_account_id("", assignment.account_id, None)
        # One entry per config provider: an assignment whose events span providers
        # must not take its first row's target for the rest.
        rows_by_config_provider: dict[str, list[PendingUsageEvent]] = {}
        for event_id in assignment.event_ids:
            row = by_id[event_id]
            rows_by_config_provider.setdefault(
                account_config_provider_id(row.provider_id), []
            ).append(row)
        for row_config_provider, assignment_rows in rows_by_config_provider.items():
            assignment_target = assignment.target_provider_id or row_config_provider
            account_key = (assignment_target, account_id)
            if account_key not in assignable_accounts:
                configured = session.exec(
                    select(ProviderConfig).where(
                        ProviderConfig.provider_id == assignment_target,
                        ProviderConfig.account_id == account_id,
                    )
                ).first()
                if configured is not None:
                    # Archived rows are always disabled, so accept them explicitly;
                    # disabled-but-not-archived accounts stay rejected.
                    assignable_accounts[account_key] = configured.enabled or configured.archived
                else:
                    assignable_accounts[account_key] = account_key in active_account_keys or (
                        assignment_target not in providers_with_config
                        and account_key in latest_account_keys
                    )
            if not assignable_accounts[account_key]:
                raise HTTPException(
                    status_code=404,
                    detail=f"No active account {account_id!r} known for "
                    f"{assignment_rows[0].provider_id!r}.",
                )
            validated.append((assignment_target, account_id, assignment_rows))
    return validated


def _promote_pending_event_rows(
    request: Request,
    session: Session,
    rows: list[PendingUsageEvent],
    target_provider_id: str,
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
        redirected = target_provider_id != account_config_provider_id(row.provider_id)
        payload = UsageEventPush.model_validate_json(row.payload_json).model_copy(
            update={"account_id": account_id, "account_source": "tag"}
        )
        if redirected:
            payload = redirect_push(payload, target_provider_id, account_id)
        # OpenCode doesn't identify which credential origin handled a
        # message. Persist an explicit provider-level tag scoped to the
        # source sidecar so later messages use the operator's resolution.
        CredentialTagRepo.set_tag(
            session,
            provider_id=row.provider_id,
            credential_origin=payload.credential_origin
            or CredentialTagRepo.provider_origin(row.provider_id),
            account_id=account_id,
            sidecar_id=row.sidecar_id,
            set_by="operator",
            target_provider_id=target_provider_id if redirected else None,
        )
        if (
            redirected
            and session.exec(
                select(UsageEvent.id).where(
                    UsageEvent.provider_id == row.provider_id,
                    UsageEvent.event_id == row.event_id,
                )
            ).first()
            is not None
        ):
            # Already counted under the source provider; just drop the pending copy.
            continue
        existing = session.exec(
            select(UsageEvent).where(
                UsageEvent.provider_id == payload.provider_id,
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
            target_id=(
                f"{rows[0].provider_id}->{target_provider_id}/{account_id}"
                if rows and target_provider_id != account_config_provider_id(rows[0].provider_id)
                else f"{rows[0].provider_id if rows else ''}/{account_id}"
            ),
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
    # One entry per config provider (events may span providers sharing a config).
    for target_provider_id, account_id, rows in validated:
        _promote_pending_event_rows(request, session, rows, target_provider_id, account_id)
    first_target, _first_account, first_rows = validated[0]
    result: dict[str, Any] = {
        "assigned": sum(len(rows) for _target, _account, rows in validated),
        "provider_id": first_rows[0].provider_id if first_rows else None,
    }
    # Only echoed when events were actually re-homed to another provider.
    if first_rows and first_target != account_config_provider_id(first_rows[0].provider_id):
        result["target_provider_id"] = first_target
    # Every validated group as {provider_id, target_provider_id, account_id, assigned}: a
    # subset of the batch endpoint's mapping shape (no sidecar_id, since a group already
    # coalesces by config provider). The top-level fields above describe only the first
    # group (back-compat).
    result["mappings"] = [
        {
            "provider_id": rows[0].provider_id,
            "target_provider_id": target,
            "account_id": account,
            "assigned": len(rows),
        }
        for target, account, rows in validated
    ]
    return result


@router.post("/events/pending/assign-batch")
async def assign_pending_usage_events_batch(
    request: Request,
    body: PendingEventBatchAssignment,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_key),
) -> dict[str, Any]:
    validated = await _validate_pending_event_assignments(body.assignments, session)
    for target_provider_id, account_id, rows in validated:
        _promote_pending_event_rows(
            request,
            session,
            rows,
            target_provider_id,
            account_id,
            clear_pending=False,
            record_audit=False,
        )
    for _target, _account_id, rows in validated:
        for row in rows:
            session.delete(row)
    session.commit()
    assigned = sum(len(rows) for _target, _account_id, rows in validated)
    providers = sorted(
        {row.provider_id for _target, _account_id, rows in validated for row in rows}
    )
    mapping_targets = sorted(
        {
            (row.provider_id, row.sidecar_id, target_provider_id, account_id)
            for target_provider_id, account_id, rows in validated
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
            {
                "provider_id": provider_id,
                "sidecar_id": sidecar_id,
                "target_provider_id": target_provider_id,
                "account_id": account_id,
            }
            for provider_id, sidecar_id, target_provider_id, account_id in mapping_targets
        ],
    }


def _get_active_identities(_session: Session) -> dict[str, str]:
    """Deprecated compatibility field: never infer a host identity from usage."""
    return {}


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
