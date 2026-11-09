"""Credential sources, tags and pending identities reported by the sidecars."""

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import Session, col, select

from app.api.endpoints.fleet_shared import account_tag_hints_for_providers, validation_summary
from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key, validate_ingest_auth
from app.core.utils import scrub_log
from app.models.db import (
    CredentialSource,
    CredentialTag,
    PendingCredentialTag,
    ProviderConfig,
)
from app.services import audit_log
from app.services.account_identity import canonical_account_id, normalize_sidecar_id
from app.services.blocked_sources import SOURCE_REASONS, is_source_blocked
from app.services.credential_tags import CredentialTagRepo, ParkedMapping, PendingCredentialTagRepo
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

# HMAC-only sidecar route: POST /credentials/manifest. Keep
# docs/forward-auth.md's exact bypass route list in sync; the other fleet
# routes stay behind Authentik.

router = APIRouter()


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
        detail = validation_summary(exc)
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

    resolved = account_tag_hints_for_providers(
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
        old_preferences = manager.credential_source_preferences.get(
            (body.provider_id, previous_account_id)
        )
        if old_preferences is not None:
            old_preferences.pop(source_id, None)
        manager.credential_source_preferences.setdefault((body.provider_id, target_account_id), {})[
            source_id
        ] = (enabled, priority)

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
        hints = account_tag_hints_for_providers(session, pids, sidecar_id=sc_id)
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
    promoted = await manager.promote_source_identity(
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


# NOTE: /api/v1/fleet/credentials/redeem is intentionally not implemented
# in this PR. Adding the endpoint without a production caller would ship
# unused authenticated surface (HMAC body parsing, decryption, audit
# hooks) that nothing exercises — see code review on PR #283. The token
# module + the issuance in ``/fleet/config`` are the public surface for
# now; the redeem handler lands with the first real caller in the
# follow-up PR.


__all__ = [
    "PASS_THROUGH_PROVIDER_PREFIX",
    "PASS_THROUGH_PROVIDERS",
    "ClaudeOrganizationSelection",
    "CredentialHealthObservation",
    "CredentialManifestRequest",
    "CredentialTagRequest",
    "_entry_reason",
    "_pending_claude_source",
    "_pending_organization_count",
    "_record_pending_entry",
    "_record_source_blocks",
    "_refresh_observed_sources",
    "_reported_manifest_origins",
    "_retire_missing_claude_sources",
    "_source_field",
    "_validate_device_manifest",
    "_verify_pending_claude_source",
    "delete_credential_tag",
    "get_claude_source_organizations",
    "is_pass_through_provider",
    "list_credential_tags",
    "list_pending_credential_tags",
    "post_credential_manifest",
    "post_credential_tag",
    "select_claude_source_organization",
    "visible_pending_rows",
]
