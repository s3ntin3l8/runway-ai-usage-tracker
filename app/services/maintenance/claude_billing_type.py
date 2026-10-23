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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, col, select

from app.models.db import LatestUsage, ProviderConfig, SystemConfig
from app.services.billing_type import UNKNOWN, detect_billing_type

#: The providers whose accounts the detector can price.
CLAUDE_PROVIDERS = ("anthropic", "anthropic_console")

#: How old a published card may be and still count as evidence. A card that
#: has not been refreshed in this long describes an account the collector has
#: not reached since — the account may have changed since, so the card proves
#: nothing about how it bills *now*. The sibling ``orphan_gauge_series``
#: check gates on the same window for the same class of risk.
EVIDENCE_MAX_AGE_DAYS = 30

#: The Claude collectors' own default poll interval, in seconds (see
#: ``CollectorManager.collector_registry``). Used when neither the account nor
#: the operator set one.
DEFAULT_POLL_INTERVAL_SECONDS = 900

#: How many collection cycles a card may lag behind the account's newest card
#: and still count as part of its current state. The collector republishes a
#: window every cycle, so a card further behind than this belongs to a state
#: the account has since left — while a card a cycle or two behind is simply
#: one the collector has not refreshed *yet*.
EVIDENCE_PUBLICATION_CYCLES = 2


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


def _default_poll_interval_seconds(session: Session) -> int:
    """How often the collector refreshes an account's cards.

    The account's own interval wins; then the operator's fleet-wide one; then
    the collector's default.
    """
    cfg = session.exec(select(SystemConfig)).first()
    if cfg is not None and cfg.default_poll_interval_seconds:
        return int(cfg.default_poll_interval_seconds)
    return DEFAULT_POLL_INTERVAL_SECONDS


def _published_cards(
    session: Session, provider_id: str, account_id: str, poll_interval_seconds: int
) -> list[dict[str, Any]]:
    """The cards the account published, as the detector reads them.

    A card is evidence only while it still describes the account, so three
    filters apply:

    - a card the collector marked ``stale`` / ``collection_failing`` describes
      a credential that stopped working, so it says nothing about how the
      account bills (``app/api/endpoints/usage.py`` drops those cards too);
    - a card that has not been refreshed within ``EVIDENCE_MAX_AGE_DAYS``
      describes an account the collector has not reached since, so it may
      describe a state the account has since left;
    - a card more than ``EVIDENCE_PUBLICATION_CYCLES`` collection cycles behind
      the account's newest card describes an older publication. The window is
      the account's own poll interval, not a fixed span: a card a cycle behind
      is one the collector has not refreshed *yet*, while a card further behind
      belongs to a state the account has since left.

    Newest first.
    """
    cutoff = datetime.now(UTC) - timedelta(days=EVIDENCE_MAX_AGE_DAYS)
    rows = session.exec(
        select(LatestUsage.card_json, LatestUsage.updated_at).where(
            LatestUsage.provider_id == provider_id,
            LatestUsage.account_id == account_id,
        )
    ).all()
    dated: list[tuple[datetime, dict[str, Any]]] = []
    for card_json, updated_at in rows:
        if updated_at is None:
            continue
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if updated_at < cutoff:
            continue
        try:
            card = json.loads(card_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(card, dict):
            continue
        if card.get("stale") is True or card.get("collection_failing") is True:
            continue
        dated.append((updated_at, card))
    if not dated:
        return []
    dated.sort(key=lambda item: item[0], reverse=True)
    newest = dated[0][0]
    publication_floor = newest - timedelta(
        seconds=EVIDENCE_PUBLICATION_CYCLES * max(poll_interval_seconds, 1)
    )
    return [card for updated_at, card in dated if updated_at >= publication_floor]


def _account_model(session: Session, row: ProviderConfig, poll_interval_seconds: int) -> str | None:
    """The model the account's published cards describe, or ``None``.

    ``None`` is the absence of a proof: an account that never published a
    card, or whose cards say nothing about how it bills, is not guessed at.
    """
    cards = _published_cards(session, row.provider_id, row.account_id, poll_interval_seconds)
    if not cards:
        return None
    model = detect_billing_type(cards)
    return None if model == UNKNOWN else model


def _candidates(session: Session, provider_id: str | None = None) -> list[ProviderConfig]:
    """The rows nobody decided a billing model for.

    Archived and disabled rows are candidates too: pricing the model of an
    account the operator paused is harmless, and the row keeps whatever the
    operator set if they set one.
    """
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
    default_interval = _default_poll_interval_seconds(session)
    plan = ClaudeBillingTypePlan(total=len(rows))
    for row in rows:
        model = _account_model(session, row, row.poll_interval_seconds or default_interval)
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
