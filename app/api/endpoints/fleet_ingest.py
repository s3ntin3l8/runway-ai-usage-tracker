"""Sidecar ingest: the HMAC-authenticated push endpoint and its helpers."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlmodel import Session, col, select

from app.api.endpoints.fleet_shared import _validation_summary
from app.core.date_utils import parse_iso8601_utc
from app.core.db import get_session
from app.core.login_dirs import stored_login_dirs
from app.core.rate_limit import limiter
from app.core.security import validate_ingest_auth
from app.core.source_apps import HERMES, MINIMAX_CODE
from app.models.db import (
    CredentialSource,
    LatestUsage,
    ProviderConfig,
    SidecarRegistry,
    SystemConfig,
)
from app.models.schemas import IngestRequest
from app.services.account_identity import (
    canonical_account_id,
    normalize_sidecar_id,
    resolve_account_id,
)
from app.services.account_merges import resolve_merged_account
from app.services.accumulator import (
    prune_stale_latest_usage,
    reconcile_latest_usage_snapshot,
    upsert_latest_usage,
)
from app.services.credential_tags import CredentialTagRepo, ParkedMapping, PendingCredentialTagRepo
from app.services.fleet_registry import (
    effective_auto_update,
    effective_update_channel,
    fleet_registry,
)
from app.services.refresh_policy import parse_provider_flags
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

# HMAC-only sidecar route: POST /ingest. Keep docs/forward-auth.md's exact
# bypass route list in sync; the other fleet routes stay behind Authentik.

router = APIRouter()


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


_VOLATILE_TOKEN_KEYS = {"expiry_date", "expires_at", "expires_in", "access_token"}


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

    # Session names: best-effort, in their own try/except so a name failure can
    # never fail the events above. A name is free text: log counts, never text.
    names_upserted = 0
    names_error = False
    if payload.session_names:
        from app.services.session_names import upsert_session_names

        try:
            names_upserted = upsert_session_names(session, payload.session_names)
        except Exception as e:
            session.rollback()
            names_error = True
            logger.error(
                "Session name upsert failed (%d names): %s",
                len(payload.session_names),
                type(e).__name__,
            )

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
        # Session names written (new or changed); 0 when none were sent or all unchanged.
        "names_upserted": names_upserted,
        # True only when the names upsert raised: the sidecar keeps the names
        # unsent and retries them next cycle (0 upserted alone cannot tell).
        "names_error": names_error,
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


__all__ = [
    "SIDECAR_ONLY_EVENT_PROVIDERS",
    "_COOKIE_BUNDLE_KEYS",
    "_INGEST_CREDENTIAL_KEYS",
    "_VOLATILE_TOKEN_KEYS",
    "_bundle_holds",
    "_can_reconcile_row",
    "_get_active_identities",
    "_is_cookie_key",
    "_reset_anchors_for_sidecar",
    "ingest_metrics",
]
