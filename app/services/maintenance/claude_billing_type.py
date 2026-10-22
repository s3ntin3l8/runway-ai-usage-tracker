"""Price the Claude accounts that were created before detection existed.

A Claude account's billing model is detected from the shape of the cards it
publishes, and the detection only ever lands on a **new** account row: an
existing row's model is the operator's to choose. So every account created
before the detection existed keeps ``billing_type = "unknown"``, and the
dashboard shows a usage value where a plan shows an estimate.

``unknown`` means both "never detected" and "the operator chose unknown", so
the column alone cannot tell them apart — ``billing_type_source`` can: it
records who wrote the value (``operator``, ``detected`` or nobody). This
repair prices the rows nobody decided about, and only those.

The evidence is the account's own published cards (``latest_usage``): the same
shapes the detector already reads, from the same account the dashboard shows.
No credential is decrypted and no provider is called — the backfill reads what
the account already published.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, col, select

from app.models.db import LatestUsage, ProviderConfig
from app.services.billing_type import UNKNOWN, detect_billing_type

#: The providers whose accounts the detector can price.
CLAUDE_PROVIDERS = ("anthropic", "anthropic_console")


@dataclass
class ClaudeBillingTypeCandidate:
    """One account the repair looked at, and what it would make of it."""

    provider_id: str
    account_id: str
    account_label: str | None = None
    model: str | None = None  # None = no published card decides a model


@dataclass
class ClaudeBillingTypePlan:
    """What the repair would change, and what it would leave alone."""

    total: int = 0
    priced: list[ClaudeBillingTypeCandidate] = field(default_factory=list)
    skipped: list[ClaudeBillingTypeCandidate] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)


@dataclass
class ClaudeBillingTypeResult:
    """What the repair changed."""

    priced: int = 0
    skipped: int = 0


def _published_cards(session: Session, provider_id: str, account_id: str) -> list[dict[str, Any]]:
    """The cards the account published, as the detector reads them."""
    rows = session.exec(
        select(LatestUsage.card_json).where(
            LatestUsage.provider_id == provider_id,
            LatestUsage.account_id == account_id,
        )
    ).all()
    cards: list[dict[str, Any]] = []
    for card_json in rows:
        try:
            card = json.loads(card_json)
        except (TypeError, ValueError):
            continue
        if isinstance(card, dict):
            cards.append(card)
    return cards


def _account_model(session: Session, provider_id: str, account_id: str) -> str | None:
    """The model the account's published cards describe, or ``None``.

    ``None`` is the absence of a proof: an account that never published a
    card, or whose cards say nothing about how it bills, is not guessed at.
    """
    cards = _published_cards(session, provider_id, account_id)
    if not cards:
        return None
    model = detect_billing_type(cards)
    return None if model == UNKNOWN else model


def _candidates(session: Session, provider_id: str | None = None) -> list[ProviderConfig]:
    """The rows nobody decided a billing model for."""
    query = select(ProviderConfig).where(
        col(ProviderConfig.provider_id).in_(CLAUDE_PROVIDERS),
        ProviderConfig.billing_type == UNKNOWN,
        col(ProviderConfig.billing_type_source).is_(None),
    )
    if provider_id is not None:
        query = query.where(ProviderConfig.provider_id == provider_id)
    return list(session.exec(query).all())


def plan_claude_billing_type(
    session: Session, provider_id: str | None = None, sample_size: int = 5
) -> ClaudeBillingTypePlan:
    """What the repair would price, and what it would skip."""
    rows = _candidates(session, provider_id)
    plan = ClaudeBillingTypePlan(total=len(rows))
    for row in rows:
        model = _account_model(session, row.provider_id, row.account_id)
        candidate = ClaudeBillingTypeCandidate(
            provider_id=row.provider_id,
            account_id=row.account_id,
            account_label=row.account_label,
            model=model,
        )
        if model is None:
            plan.skipped.append(candidate)
            continue
        plan.priced.append(candidate)
        if len(plan.samples) < sample_size:
            plan.samples.append(f"{row.provider_id}/{row.account_id}")
    return plan


def apply_claude_billing_type(
    session: Session, provider_id: str | None = None
) -> ClaudeBillingTypeResult:
    """Price the accounts the plan would price.

    A row somebody decided about while the plan was being read is left alone:
    the operator's choice wins over anything this repair can prove.
    """
    plan = plan_claude_billing_type(session, provider_id=provider_id)
    priced = 0
    for candidate in plan.priced:
        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == candidate.provider_id,
                ProviderConfig.account_id == candidate.account_id,
            )
        ).first()
        if row is None:
            continue
        if row.billing_type != UNKNOWN or row.billing_type_source is not None:
            continue
        if candidate.model is None:
            # No published card decides a model, so nothing is written.
            continue
        row.billing_type = candidate.model
        row.billing_type_source = "detected"
        session.add(row)
        priced += 1
    session.commit()
    return ClaudeBillingTypeResult(priced=priced, skipped=len(plan.skipped))
