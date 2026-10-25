"""Idempotent event ingestion.

Writes UsageEvent rows, updates rollups, computes cost_usd from
provider_pricing. The full batch commits as one transaction: either every
new event lands together or none of them do. Per-event savepoints isolate
the harmless IntegrityError raised when a sidecar replays a duplicate, so
duplicates don't poison the surrounding batch.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.date_utils import parse_iso8601_utc
from app.models.db import PendingUsageEvent, ProviderConfig, UsageEvent
from app.models.schemas import UsageEventPush
from app.services.account_identity import canonical_account_id, is_redirect_source_provider
from app.services.credential_tags import CredentialTagRepo, ParkedMapping
from app.services.maintenance.event_cost import resolve_event_cost
from app.services.model_normalization import normalize_ag_model
from app.services.period_rollups import update_rollups_for_event
from app.services.project_label import derive_project


@dataclass
class IngestResult:
    events_received: int = 0
    events_inserted: int = 0
    events_duplicate: int = 0
    # Already stored under another account and moved to the pushed one.
    events_reattributed: int = 0
    windows_closed: int = 0


# Everything a re-attributed row takes from the re-push. Identity columns
# (id, provider_id, event_id, sidecar_id — the same by construction) stay.
_REFRESHED_FIELDS = tuple(
    name
    for name in UsageEvent.model_fields
    if name not in ("id", "provider_id", "event_id", "sidecar_id")
)


def _is_hermes_event(push: UsageEventPush) -> bool:
    return push.event_id.startswith("hermes|")


def redirect_push(push: UsageEventPush, target_provider_id: str, account_id: str) -> UsageEventPush:
    """Re-home an event onto an account of a related provider (operator tag).

    Hermes reports raw Gemini ids (``gemini-3-pro-preview``) that match no
    seeded row once the event is stored under Antigravity, so re-bucket them
    with the target provider's normalizer. Gemini-CLI events arrive already
    normalized and keep their model id.
    """
    update: dict[str, str | None] = {
        "provider_id": target_provider_id,
        "account_id": account_id,
        "account_source": "tag",
    }
    if target_provider_id == "antigravity" and push.model_id and _is_hermes_event(push):
        update["model_id"] = normalize_ag_model(push.model_id, "", {})
    return push.model_copy(update=update)


class EventIngestor:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _resolve_identityless_account(
        self,
        push: UsageEventPush,
        sidecar_id: str | None,
        parked: ParkedMapping,
        cache: dict[str, str | None],
    ) -> tuple[str, str] | None:
        """The account an identity-less event belongs to, and where it came from.

        An operator tag wins: it is the explicit choice, and it is what the
        "Unassigned usage" card writes when the operator assigns the held-back
        events. Only when no tag applies does the host's own native identity
        for the provider answer (#931).

        ``None`` means the event is held back for the operator.
        """
        mapped_tag = CredentialTagRepo.get(
            self.session,
            provider_id=push.provider_id,
            credential_origin=push.credential_origin
            or CredentialTagRepo.provider_origin(push.provider_id),
            sidecar_id=sidecar_id,
        )
        if mapped_tag is not None and not parked.is_parked(mapped_tag):
            return mapped_tag.account_id, "tag"
        native = self._native_identity_account(push.provider_id, sidecar_id, parked, cache)
        if native:
            return native, "local"
        return None

    def _native_identity_account(
        self,
        provider_id: str,
        sidecar_id: str | None,
        parked: ParkedMapping,
        cache: dict[str, str | None],
    ) -> str | None:
        """The account this host's own native collector reported for a provider.

        A credential the sidecar found is filed under the account it resolved
        to, so that row is the machine's own answer to "which account is
        this provider on *this* host". It is the only host-scoped identity
        available for an event that names a provider but not an account — a
        Hermes event is billed to the account of the upstream it called, and
        the sidecar only ever learns the provider.

        Exactly one such account means the provider's traffic on this host
        belongs to it. Zero (the credential carries no identity) or several
        (the host holds more than one account for the provider) leave the
        event where it was — held for the operator to assign.

        An archived account is retired: nothing may be stamped on it, so the
        event is held like any other unresolved one.

        ``cache`` keeps one lookup per provider for the whole batch.
        """
        if provider_id not in cache:
            from app.services.credential_sources import sidecar_native_accounts

            accounts = sidecar_native_accounts(self.session, provider_id, sidecar_id)
            account: str | None = None
            if len(accounts) == 1:
                candidate = accounts.pop()
                if not parked.is_parked_account(provider_id, candidate):
                    account = candidate
            cache[provider_id] = account
        return cache[provider_id]

    def _drop_pending_copies(self, push: UsageEventPush) -> None:
        """Remove the pending copies of an event that is now stored.

        An event held back for assignment can resolve later — the operator
        tags the provider, or the host's native identity answers. Its pending
        copy is then stale: the "Unassigned usage" card would keep listing an
        event that is already counted, and a later assignment would move a
        row that is already stored.
        """
        for row in self.session.exec(
            select(PendingUsageEvent).where(
                PendingUsageEvent.provider_id == push.provider_id,
                PendingUsageEvent.event_id == push.event_id,
            )
        ).all():
            self.session.delete(row)

    def ingest(
        self,
        pushes: list[UsageEventPush],
        *,
        sidecar_id: str | None = None,
    ) -> IngestResult:
        result = IngestResult(events_received=len(pushes))
        redirects: dict[str, tuple[str, str] | None] = {}
        # A mapping onto an archived account is *parked*: it resolves the
        # credential, but nothing may be stamped on the retired account — not
        # a card, not a usage event. The predicate is tag-aware: a redirect
        # tag names an account of its *target* provider, so its parked-ness
        # is looked up there.
        parked = ParkedMapping(self.session, [push.provider_id for push in pushes])
        # One native-identity lookup per provider for the whole batch.
        native_accounts: dict[str, str | None] = {}
        try:
            for push in pushes:
                if is_redirect_source_provider(push.provider_id):
                    if push.provider_id not in redirects:
                        tag = CredentialTagRepo.get_redirect(
                            self.session, provider_id=push.provider_id, sidecar_id=sidecar_id
                        )
                        # get_redirect only returns tags with a target set.
                        redirects[push.provider_id] = (
                            (tag.target_provider_id, tag.account_id)
                            if tag is not None and tag.target_provider_id
                            else None
                        )
                    redirect = redirects[push.provider_id]
                    if redirect is not None:
                        target_provider_id, target_account = redirect
                        if parked.is_parked_account(target_provider_id, target_account):
                            # Parked: the target account is retired, so the
                            # events must not be re-homed onto it. The push
                            # keeps its own provider and falls through to the
                            # identity-less branch below, where the parked
                            # mapping keeps it out of the retired account and
                            # holds it for assignment instead.
                            redirect = None
                    if redirect is not None:
                        target_provider_id, target_account = redirect
                        is_default = push.account_source == "default" or (
                            push.account_source is None
                            and canonical_account_id(push.account_id) == "default"
                        )
                        # 'tag' events for a different account keep their source provider.
                        if is_default or (
                            push.account_source == "tag"
                            and canonical_account_id(push.account_id)
                            == canonical_account_id(target_account)
                        ):
                            if (
                                self.session.exec(
                                    select(UsageEvent.id).where(
                                        UsageEvent.provider_id == push.provider_id,
                                        UsageEvent.event_id == push.event_id,
                                    )
                                ).first()
                                is not None
                            ):
                                # Source-side check only: a row already stored under the
                                # target provider is caught later by the unique index
                                # (IntegrityError -> _reattribute, a no-op for the same account).
                                # Already stored under the source provider (e.g.
                                # history from before the redirect); don't double-count.
                                result.events_duplicate += 1
                                continue
                            push = redirect_push(
                                push, target_provider_id, canonical_account_id(target_account)
                            )
                ts = parse_iso8601_utc(push.ts)
                # Same canonical form the card path uses (resolve_account_id),
                # so a mixed-case email from a sidecar can't split an account
                # into an events-only twin of its quota card.
                account_id = canonical_account_id(push.account_id)

                if push.account_source == "default" or (
                    push.account_source is None and account_id == "default"
                ):
                    # Identity-less event providers have no credential origin
                    # in their event stream. Their explicit provider mapping
                    # is the available identity signal. Resolve it at ingest
                    # time as well as in the sidecar so a saved mapping takes
                    # effect immediately, even with a stale hint cache.
                    #
                    # A *parked* mapping — one whose account is archived — does
                    # not name an account to bill: the event is held for
                    # assignment instead of being stamped on the account the
                    # operator retired.
                    identity = self._resolve_identityless_account(
                        push, sidecar_id, parked, native_accounts
                    )
                    if identity:
                        account_id = canonical_account_id(identity[0])
                        push = push.model_copy(
                            update={"account_id": account_id, "account_source": identity[1]}
                        )
                        # The event is no longer waiting for an assignment: drop
                        # any pending copy, so the "Unassigned usage" card stops
                        # listing an event that is already counted.
                        self._drop_pending_copies(push)
                    else:
                        existing = self.session.exec(
                            select(UsageEvent.id).where(
                                UsageEvent.provider_id == push.provider_id,
                                UsageEvent.event_id == push.event_id,
                            )
                        ).first()
                        if existing is not None:
                            # A stale or replayed unresolved payload must not
                            # create a second pending copy of an event that has
                            # already been assigned to a real account.
                            result.events_duplicate += 1
                            continue
                        sidecar = sidecar_id or "local"
                        row = self.session.exec(
                            select(PendingUsageEvent).where(
                                PendingUsageEvent.provider_id == push.provider_id,
                                PendingUsageEvent.event_id == push.event_id,
                                PendingUsageEvent.sidecar_id == sidecar,
                            )
                        ).first()
                        payload = json.dumps(
                            push.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
                        )
                        if row is None:
                            self.session.add(
                                PendingUsageEvent(
                                    provider_id=push.provider_id,
                                    event_id=push.event_id,
                                    sidecar_id=sidecar,
                                    ts=ts,
                                    payload_json=payload,
                                )
                            )
                            result.events_inserted += 1
                        else:
                            row.payload_json = payload
                            row.last_seen = datetime.now(UTC)
                            self.session.add(row)
                            result.events_duplicate += 1
                        continue

                if push.kind == "error":
                    ev = UsageEvent(
                        provider_id=push.provider_id,
                        account_id=account_id,
                        attribution_source=push.account_source or "unknown",
                        credential_origin=push.credential_origin,
                        sidecar_id=sidecar_id or "local",
                        source_app=push.source_app or push.provider_id,
                        event_id=push.event_id,
                        ts=ts,
                        kind="error",
                        # store the error tag in stop_reason
                        stop_reason=push.error_reason,
                        raw_json=push.raw_json,
                    )
                    if self._try_insert_event(ev):
                        result.events_inserted += 1
                    elif self._reattribute(ev):
                        result.events_reattributed += 1
                    else:
                        result.events_duplicate += 1
                    continue  # error events don't roll up

                # PAYG accounts prefer a provider-reported amount (e.g. an
                # OpenCode log value); subscription accounts, and any account
                # whose model has a resolvable price row, use the computed
                # estimate. An account with no resolvable price row at all
                # falls back to the reported cost rather than billing
                # $0.00 for an unseeded model — see resolve_event_cost.
                # Components stay pricing-derived either way.
                config = self.session.exec(
                    select(ProviderConfig).where(
                        ProviderConfig.provider_id == push.provider_id,
                        ProviderConfig.account_id == account_id,
                    )
                ).first()
                billing_type = config.billing_type if config else "unknown"
                resolved = resolve_event_cost(
                    self.session,
                    provider_id=push.provider_id,
                    model_id=push.model_id,
                    ts=ts,
                    tokens_input=push.tokens_input,
                    tokens_output=push.tokens_output,
                    tokens_cache_read=push.tokens_cache_read,
                    tokens_cache_create=push.tokens_cache_create,
                    tokens_reasoning=push.tokens_reasoning,
                    tokens_cache_create_1h=push.tokens_cache_create_1h,
                    tokens_cache_create_5m=push.tokens_cache_create_5m,
                    billing_type=billing_type,
                    reported_cost=push.cost_usd,
                )
                breakdown = resolved.breakdown
                ev = UsageEvent(
                    provider_id=push.provider_id,
                    account_id=account_id,
                    attribution_source=push.account_source or "unknown",
                    credential_origin=push.credential_origin,
                    sidecar_id=sidecar_id or "local",
                    # The client app that made the call. provider_id names the
                    # upstream provider billed, which several client apps share,
                    # so the fallback keeps old sidecars (that report no app)
                    # grouped the same way the pre-column rows were backfilled.
                    source_app=push.source_app or push.provider_id,
                    event_id=push.event_id,
                    ts=ts,
                    kind=push.kind,
                    model_id=push.model_id,
                    session_id=push.session_id,
                    turn_id=push.turn_id,
                    cwd=push.cwd,
                    # Derive the project label server-side (single source of truth)
                    # so all providers normalise the same way regardless of sidecar.
                    # Worktree/tmp cwds collapse to the project root (rule 1); the
                    # per-session subfolder-drift fix runs offline in
                    # scripts/consolidate_session_projects.py.
                    #
                    # The client may name the git repository its working directory
                    # belongs to — it resolves that on its own host, because the
                    # server never reads the client's filesystem. That name wins:
                    # two checkouts of one repository group together even when
                    # their folders are named differently. Without it, the
                    # folder's own name is the label.
                    project=push.project_ref or derive_project(push.cwd),
                    project_ref=push.project_ref,
                    git_branch=push.git_branch,
                    tools_json=(json.dumps(push.tool_names) if push.tool_names else None),
                    subagent_type=push.subagent_type,
                    tokens_input=push.tokens_input,
                    tokens_output=push.tokens_output,
                    tokens_cache_read=push.tokens_cache_read,
                    tokens_cache_create=push.tokens_cache_create,
                    tokens_cache_create_1h=push.tokens_cache_create_1h,
                    tokens_cache_create_5m=push.tokens_cache_create_5m,
                    tokens_reasoning=push.tokens_reasoning,
                    cost_usd=resolved.cost_usd,
                    cost_reported_usd=resolved.cost_reported_usd,
                    cost_estimated_usd=resolved.cost_estimated_usd,
                    cost_input=breakdown.input,
                    cost_output=breakdown.output,
                    cost_cache_read=breakdown.cache_read,
                    cost_cache_create=breakdown.cache_create,
                    stop_reason=push.stop_reason,
                    tool_calls=push.tool_calls,
                    latency_ms=push.latency_ms,
                    raw_json=push.raw_json,
                    effort=push.effort,
                    speed=push.speed,
                    service_tier=push.service_tier,
                    entrypoint=push.entrypoint,
                    app_version=push.app_version,
                    web_search_requests=push.web_search_requests,
                    web_fetch_requests=push.web_fetch_requests,
                )
                if not self._try_insert_event(ev):
                    if self._reattribute(ev):
                        result.events_reattributed += 1
                    else:
                        result.events_duplicate += 1
                    continue
                # Rollups share the same outer transaction; if anything past
                # this point raises, the whole batch rolls back.
                update_rollups_for_event(self.session, ev)
                result.events_inserted += 1
        except Exception:
            self.session.rollback()
            raise

        self.session.commit()
        return result

    def _reattribute(self, incoming: UsageEvent) -> bool:
        """Move an already-stored event to ``incoming.account_id`` if warranted.

        Called after the insert hit the ``(provider_id, event_id)`` unique
        index. The event is re-attributed only when the **same sidecar**
        now reports it under a different account — that host's identity for
        the credential changed (operator retag, a tag hint arrived, a hint
        was withdrawn), and the host is authoritative for its own events.
        A different sidecar sending the same event (e.g. a shared home
        directory) keeps the first attribution, so two hosts can't flip it
        back and forth. The moved row takes the re-push's payload (model,
        project context, tokens, cost), so a retag can't leave stale
        enrichment behind, and rollups move with it: the stored values are
        subtracted from the old account, the refreshed ones added to the new.
        Returns True when the row moved.
        """
        existing = self.session.exec(
            select(UsageEvent).where(
                UsageEvent.provider_id == incoming.provider_id,
                UsageEvent.event_id == incoming.event_id,
            )
        ).first()
        if (
            existing is None
            or existing.account_id == incoming.account_id
            or existing.sidecar_id != incoming.sidecar_id
        ):
            return False
        # Only messages roll up; decide on the stored kind before the
        # refresh and on the pushed kind after it.
        if existing.kind == "message":
            update_rollups_for_event(self.session, existing, sign=-1)
        for field in _REFRESHED_FIELDS:
            setattr(existing, field, getattr(incoming, field))
        self.session.add(existing)
        self.session.flush()
        if existing.kind == "message":
            update_rollups_for_event(self.session, existing)
        return True

    def _try_insert_event(self, ev: UsageEvent) -> bool:
        """Insert one event inside its own savepoint.

        Returns True on a new row, False when the ``(provider_id, event_id)``
        unique index rejected the row as already stored (see
        :meth:`_reattribute`). The savepoint scope keeps a duplicate from
        invalidating prior events already staged in the outer transaction.
        """
        sp = self.session.begin_nested()
        try:
            self.session.add(ev)
            self.session.flush()
        except IntegrityError:
            sp.rollback()
            return False
        sp.commit()
        return True
