"""Claude publication and identity promotion for CollectorManager.

Move-only extract of publication guards and identity promotion.
Verification scheduling and durable-tag reconciliation stay on CollectorManager.
"""

import asyncio
import logging
from typing import Any

from app.core.utils import scrub_log
from app.services.account_identity import is_placeholder_label
from app.services.credential_sources import is_sidecar_source
from app.services.smart_collector import SmartCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class CollectorClaudeIdentity:
    """Publication guards and source-identity promotion.

    CollectorManager keeps verification scheduling and durable-tag
    reconciliation; this cluster calls back into them via ``self``.
    """

    async def reconcile_token_cache_from_durable_tags(
        self,
        *,
        provider_id: str | None = None,
        source_id: str | None = None,
    ) -> int:
        """Declared for the type checker. CollectorManager's method runs instead."""
        raise NotImplementedError

    @staticmethod
    def _promotion_rejection_reason(
        provider_id: str, candidate: dict[str, Any], collector: Any
    ) -> str | None:
        if getattr(collector, "verified_source_generation", None) is not None:
            return "credential binding changed during collection"
        # Preserve legacy generic collection when optional source metadata is absent,
        # while never publishing against a contradictory explicit operator assignment.
        from sqlmodel import Session

        from app.core.db import engine
        from app.services.account_identity import canonical_account_id
        from app.services.credential_tags import CredentialTagRepo

        origin = candidate.get("credential_origin")
        if not isinstance(origin, str):
            return None
        with Session(engine) as session:
            tag = CredentialTagRepo.get(
                session,
                provider_id=provider_id,
                credential_origin=origin,
                sidecar_id=candidate.get("sidecar_id"),
            )
            if (
                tag
                and tag.set_by not in (None, "identity_claim", "identity_verification", "rotation")
                and canonical_account_id(tag.account_id)
                != canonical_account_id(collector.account_id)
            ):
                return "credential source assigned to another account"
        return None

    @staticmethod
    def _source_proof_kwargs(collector: Any) -> dict[str, Any]:
        generation = getattr(collector, "verified_source_generation", None)
        if generation is None:
            return {}
        return {
            "expected_generation": generation,
            "organization_id": getattr(collector, "verified_organization_id", None),
            "account_label": getattr(collector, "verified_account_label", None),
            "billing_type": getattr(collector, "verified_billing_type", None),
        }

    @staticmethod
    def _discard_stale_source_result(
        smart: SmartCollector, reason: str = "credential binding changed during collection"
    ) -> list[dict[str, Any]]:
        """A rejected proof is non-authoritative; a later successful fallback may complete."""
        smart.set_collection_state("skipped", reason)
        return []

    @staticmethod
    def _protect_claude_cards(
        provider_id: str, collector: Any, candidate: dict[str, Any], cards: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        from app.services.claude_verification import ClaudeSourceProof, ClaudeVerifiedCard

        generation = getattr(collector, "verified_source_generation", None)
        organization = getattr(collector, "verified_organization_id", None)
        if not generation:
            return cards
        proof = ClaudeSourceProof(
            provider_id,
            collector.account_id,
            candidate["source_id"],
            generation,
            organization,
            candidate.get("credential_origin"),
            candidate.get("sidecar_id"),
        )
        return [ClaudeVerifiedCard(card, proof) for card in cards]

    def claude_card_proof_valid(self, session: Any, card: dict[str, Any]) -> bool:
        proof = getattr(card, "claude_source_proof", None)
        if proof is None:
            return True
        if (
            token_cache.source_generation(proof.provider_id, proof.account_id, proof.source_id)
            != proof.generation
        ):
            return False
        return self._claude_publication_binding(
            session,
            proof.provider_id,
            proof.account_id,
            proof.organization_id,
            {
                "source_id": proof.source_id,
                "credential_origin": proof.credential_origin,
                "sidecar_id": proof.sidecar_id,
            },
        )

    async def _preview_unresolved_source(
        self, provider_id: str, candidate: dict[str, Any], result: list[dict[str, Any]]
    ) -> None:
        from app.services.credential_tags import safe_quota_preview

        preview = safe_quota_preview([card for card in result if not card.get("error_type")])
        await asyncio.to_thread(
            self._persist_identity_pending_preview, provider_id, candidate, preview
        )

    async def _claude_organization_allows_publication(
        self, provider_id: str, collector: Any, candidate: dict[str, Any]
    ) -> bool:
        organization_id = getattr(collector, "verified_organization_id", None)
        if provider_id not in ("anthropic", "anthropic_console"):
            return True
        generation = getattr(collector, "verified_source_generation", None)
        if not organization_id and not generation:
            return True
        if not generation:
            return False
        return await token_cache.commit_verified_source(
            provider_id,
            collector.account_id,
            collector.account_id,
            candidate["source_id"],
            generation,
            lambda: self._validate_claude_publication(
                provider_id, collector.account_id, organization_id, candidate
            ),
        )

    def _validate_claude_publication(
        self,
        provider_id: str,
        account_id: str,
        organization_id: str | None,
        candidate: dict[str, Any],
    ) -> bool:
        from sqlmodel import Session

        from app.core.db import engine

        with Session(engine) as session:
            allowed = self._claude_publication_binding(
                session, provider_id, account_id, organization_id, candidate
            )
            if allowed:
                session.commit()
            return allowed

    def _claude_publication_binding(
        self,
        session: Any,
        provider_id: str,
        account_id: str,
        organization_id: str | None,
        candidate: dict[str, Any],
    ) -> bool:
        from sqlmodel import select

        from app.models.db import CredentialSource
        from app.services.credential_tags import CredentialTagRepo

        source = session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == provider_id,
                CredentialSource.account_id == account_id,
                CredentialSource.source_id == candidate["source_id"],
            )
        ).first()
        if (
            source is None
            or source.credential_origin != candidate.get("credential_origin")
            or source.sidecar_id != candidate.get("sidecar_id")
        ):
            return False
        if source.sidecar_id and (
            not source.credential_origin
            or CredentialTagRepo.get_account_id(
                session,
                provider_id=provider_id,
                credential_origin=source.credential_origin,
                sidecar_id=source.sidecar_id,
            )
            != account_id
        ):
            return False
        allowed = self._claude_binding_allowed(session, provider_id, account_id, organization_id)
        return allowed

    @staticmethod
    def _claude_binding_allowed(
        session: Any,
        provider_id: str,
        account_id: str,
        organization_id: str | None,
        *,
        allow_selection: bool = False,
        account_label: str | None = None,
        billing_type: str | None = None,
    ) -> bool:
        from sqlmodel import select

        from app.models.db import ProviderAccountLabel, ProviderConfig

        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == provider_id, ProviderConfig.account_id == account_id
            )
        ).first()
        created = row is None
        if row is None:
            row = ProviderConfig(provider_id=provider_id, account_id=account_id)
        if (not row.enabled or row.archived) and not allow_selection:
            return False
        if (
            organization_id
            and row.claude_organization_id not in (None, organization_id)
            and not allow_selection
        ):
            return False
        if organization_id:
            row.claude_organization_id = organization_id
        if account_label and (created or is_placeholder_label(row.account_label)):
            # Name a discovered account after what it is. An operator's own label
            # is never overwritten; an account that never got one still deserves it.
            override = session.exec(
                select(ProviderAccountLabel).where(
                    ProviderAccountLabel.provider_id == provider_id,
                    ProviderAccountLabel.account_id == account_id,
                )
            ).first()
            row.account_label = (override.account_label if override else None) or account_label
        if created and billing_type:
            # A detected billing type only ever describes a new account: an
            # existing row's type is the operator's to choose.
            row.billing_type = billing_type
            row.billing_type_source = "detected"
        session.add(row)
        return True

    @staticmethod
    def reset_attempt_identity(collector: Any, account_id: str, default_label: Any) -> None:
        """Clear what a previous source attempt taught the shared collector instance."""
        if account_id == "default":
            # Collectors can mutate account_id after an API response. Reset before each source
            # attempt so a verified identity from source A can never be attributed to source B
            # on a later poll.
            collector.account_id = "default"
            collector.account_label = default_label
            # The label cache is the other half of that memory: a name one
            # attempt proved must not be stamped on the next attempt's card.
            collector.account_label_cache = default_label
        if hasattr(collector, "credential_account_id"):
            collector.credential_account_id = account_id
        # Proof from one source's response must never be read as another's.
        collector.verified_identity = None
        collector.verified_subject = None
        collector.verified_organization_id = None
        collector.verified_source_generation = None
        collector.verified_account_label = None
        collector.verified_billing_type = None

    async def _cookie_switched_account(
        self,
        provider_id: str,
        account_id: str,
        candidate: dict[str, Any],
        collector: Any,
    ) -> bool:
        """Detect a browser account switch behind a machine-scoped cookie tag, and undo the tag.

        A cookie's origin is the same string whichever account the browser is signed into, so a
        tag keeps applying after a switch. Collectors that learn the account's email from the
        provider (``verified_identity``) let the server notice: when that email resolves to a
        *different canonical account* than the (email-shaped) tagged one, the tag is deleted,
        the source leaves the old account, and the next sidecar push files it as pending so the
        identity verifier maps it to the new account. Anything uncertain is left alone: a tag on
        a non-email account (hash-keyed, label-based, ``user@x @ Org``) is never second-guessed.
        Accounts are keyed by login email, so an operator's own tag on a *different* email than
        the one the cookie logs into counts as a switch too (the verifier then re-maps it).
        """
        from app.services.account_identity import EMAIL_RE, canonical_account_id
        from app.services.credential_sources import is_machine_bound_origin

        origin = candidate.get("credential_origin")
        sidecar_id = candidate.get("sidecar_id")
        verified = getattr(collector, "verified_identity", None)
        subject = getattr(collector, "verified_subject", None)
        if (
            candidate.get("identity_pending")
            or not is_sidecar_source(candidate)
            or not isinstance(origin, str)
            or not origin.startswith("cookie:")
            or not is_machine_bound_origin(origin)
            or not isinstance(sidecar_id, str)
        ):
            return False
        if isinstance(verified, str) and EMAIL_RE.match(verified):
            switched = EMAIL_RE.match(account_id) is not None and canonical_account_id(
                verified
            ) != canonical_account_id(account_id)
        elif provider_id == "anthropic_console" and isinstance(verified, str):
            switched = canonical_account_id(verified) != canonical_account_id(account_id)
        else:
            # No email from the provider: fall back to its stable subject, if it gave one.
            switched = isinstance(subject, str) and await asyncio.to_thread(
                self._subject_drifted, provider_id, account_id, candidate["source_id"], subject
            )
        if not switched:
            return False
        revoked = await asyncio.to_thread(
            self._revoke_cookie_tag,
            provider_id,
            account_id,
            candidate["source_id"],
            origin,
            sidecar_id,
        )
        if not revoked:
            logger.debug(
                "Cookie switch behind %s/%s left alone: no sidecar-scoped operator tag to drop",
                scrub_log(provider_id),
                scrub_log(account_id),
            )
            return False
        await token_cache.remove_source(
            provider_id, account_id, candidate["source_id"], retire_matching_oauth=True
        )
        logger.warning(
            "A browser on %s switched account behind %s: dropped its %s cookie tag; "
            "the source is pending again",
            scrub_log(sidecar_id),
            scrub_log(account_id),
            scrub_log(provider_id),
        )
        return True

    @staticmethod
    def _subject_drifted(provider_id: str, account_id: str, source_id: str, subject: str) -> bool:
        """Compare the login behind a cookie with the one last seen for this source and account.

        The first sighting (or the first one after the source moved to another account, i.e. a
        re-tag) is recorded rather than compared; a different subject for the same account
        means the browser switched users.
        """
        from sqlmodel import Session, col, select

        from app.core.db import engine
        from app.models.db import CredentialSource
        from app.services.account_identity import canonical_account_id

        account = canonical_account_id(account_id)
        with Session(engine) as session:
            row = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    CredentialSource.source_id == source_id,
                    col(CredentialSource.account_id).in_({account_id, account}),
                )
            ).first()
            if row is None:
                return False
            if row.verified_subject is None or row.verified_subject_account != account:
                row.verified_subject = subject
                row.verified_subject_account = account
                session.add(row)
                session.commit()
                return False
            return row.verified_subject.casefold() != subject.casefold()

    @staticmethod
    def _revoke_cookie_tag(
        provider_id: str, account_id: str, source_id: str, origin: str, sidecar_id: str
    ) -> bool:
        """Delete this sidecar's tag on a cookie origin and put its durable row back to pending.

        Only a sidecar-scoped tag that a person or a verification (not a mere claim) wrote
        whose account is the one being polled; an older deployment-wide tag is left to the
        operator because it applies on other machines too.
        """
        from sqlmodel import Session, col, select

        from app.core.db import engine
        from app.models.db import CredentialSource, CredentialTag
        from app.services.account_identity import canonical_account_id
        from app.services.credential_sources import pending_cache_slot
        from app.services.credential_tags import CredentialTagRepo

        with Session(engine) as session:
            tag = session.exec(
                select(CredentialTag).where(
                    CredentialTag.provider_id == provider_id,
                    CredentialTag.credential_origin == origin,
                    CredentialTag.sidecar_id == sidecar_id,
                )
            ).first()
            if (
                tag is None
                or tag.set_by == "identity_claim"
                or canonical_account_id(tag.account_id) != canonical_account_id(account_id)
            ):
                return False
            CredentialTagRepo.delete_tag(
                session, provider_id=provider_id, credential_origin=origin, sidecar_id=sidecar_id
            )
            pending_account = pending_cache_slot(provider_id, source_id)
            row = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    col(CredentialSource.account_id).in_({account_id, tag.account_id}),
                    CredentialSource.source_id == source_id,
                )
            ).first()
            if row is not None:
                row.account_id = pending_account
                # Whoever logs in next is a new login, not a drift from the old one.
                row.verified_subject = None
                row.verified_subject_account = None
                session.add(row)
            session.commit()
        return True

    async def promote_source_identity(
        self,
        provider_id: str,
        old_account_id: str,
        source_id: str,
        account_id: str,
        *,
        cache_account_id: str | None = None,
        expected_generation: str | None = None,
        organization_id: str | None = None,
        allow_selection: bool = False,
        account_label: str | None = None,
        billing_type: str | None = None,
    ) -> bool:
        """Commit a proved identity and move the same credential generation atomically."""

        def commit() -> bool:
            return self._commit_source_identity(
                provider_id,
                old_account_id,
                source_id,
                account_id,
                cache_account_id=cache_account_id,
                organization_id=organization_id,
                allow_selection=allow_selection,
                account_label=account_label,
                billing_type=billing_type,
            )

        if expected_generation is not None:
            return await token_cache.commit_verified_source(
                provider_id,
                cache_account_id or old_account_id,
                account_id,
                source_id,
                expected_generation,
                commit,
            )
        if not commit():
            return False
        moved = await self.reconcile_token_cache_from_durable_tags(
            provider_id=provider_id, source_id=source_id
        )
        if moved:
            return True
        return await token_cache.move_source(
            provider_id, cache_account_id or old_account_id, account_id, source_id
        )

    def _commit_source_identity(
        self,
        provider_id: str,
        old_account_id: str,
        source_id: str,
        account_id: str,
        *,
        cache_account_id: str | None = None,
        organization_id: str | None = None,
        allow_selection: bool = False,
        account_label: str | None = None,
        billing_type: str | None = None,
    ) -> bool:
        from datetime import UTC, datetime

        from sqlmodel import Session, col, select

        from app.core.db import engine
        from app.models.db import CredentialSource
        from app.services.account_identity import canonical_account_id
        from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

        target = canonical_account_id(account_id)
        if not target or target == "default":
            return False
        with Session(engine) as session:
            # The durable row sits under the account the bundle was filed under: ``default``,
            # or (Anthropic) the source id itself while the identity is pending.
            filed_under = {old_account_id}
            if cache_account_id:
                filed_under.add(cache_account_id)
            source = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    col(CredentialSource.account_id).in_(filed_under),
                    CredentialSource.source_id == source_id,
                )
            ).first()
            if source is None or not source.credential_origin:
                return False
            existing_tag = (
                CredentialTagRepo.get(
                    session,
                    provider_id=provider_id,
                    credential_origin=source.credential_origin,
                    sidecar_id=source.sidecar_id,
                )
                if source.sidecar_id
                else None
            )
            if existing_tag is not None and existing_tag.set_by not in (
                None,
                "identity_claim",
                "identity_verification",
                # Carried over from the previous fingerprint (#474): inferred,
                # not mapped for this origin, so a proved identity may correct
                # it — unlike the operator's own assignment below.
                "rotation",
            ):
                # An operator mapped this source while verification was in flight: theirs wins.
                logger.info(
                    "Not promoting %s: the operator already mapped this source",
                    scrub_log(source_id),
                )
                return False
            # A proved model reaches the account even when no organization was
            # proved: an OAuth token has no Claude organization to read, so the
            # model its own cards describe is the only proof it can make.
            if (organization_id or billing_type) and not self._claude_binding_allowed(
                session,
                provider_id,
                target,
                organization_id,
                allow_selection=allow_selection,
                account_label=account_label,
                billing_type=billing_type,
            ):
                return False
            existing_target = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    CredentialSource.account_id == target,
                    CredentialSource.source_id == source_id,
                )
            ).first()
            source_sidecar_id = source.sidecar_id
            source_credential_origin = source.credential_origin
            if existing_target is None:
                source.account_id = target
                session.add(source)
            else:
                existing_target.source_type = source.source_type
                existing_target.source_label = source.source_label
                existing_target.credential_origin = source.credential_origin
                existing_target.sidecar_id = source.sidecar_id
                seen_values = [
                    value
                    for value in (source.last_seen, existing_target.last_seen)
                    if value is not None
                ]
                existing_target.last_seen = (
                    max(
                        value.replace(tzinfo=UTC) if value.tzinfo is None else value
                        for value in seen_values
                    )
                    if seen_values
                    else datetime.now(UTC)
                )
                session.add(existing_target)
                session.delete(source)
            if source_sidecar_id:
                CredentialTagRepo.set_tag(
                    session,
                    provider_id=provider_id,
                    credential_origin=source_credential_origin,
                    account_id=target,
                    sidecar_id=source_sidecar_id,
                    set_by="identity_verification",
                )
                PendingCredentialTagRepo.delete(
                    session,
                    sidecar_id=source_sidecar_id,
                    provider_id=provider_id,
                    credential_origin=source_credential_origin,
                )
            session.commit()
        return True

    def _persist_identity_pending_preview(
        self,
        provider_id: str,
        candidate: dict[str, Any],
        preview: list[dict[str, Any]],
    ) -> None:
        """Persist safe quota fields against the pending credential row."""
        sidecar_id = candidate.get("sidecar_id")
        credential_origin = candidate.get("credential_origin")
        if not isinstance(sidecar_id, str) or not isinstance(credential_origin, str):
            return

        from sqlmodel import Session

        from app.core.db import engine
        from app.services.credential_tags import PendingCredentialTagRepo

        with Session(engine) as session:
            PendingCredentialTagRepo.set_quota_preview(
                session,
                sidecar_id=sidecar_id,
                provider_id=provider_id,
                credential_origin=credential_origin,
                preview=preview,
            )
            session.commit()
