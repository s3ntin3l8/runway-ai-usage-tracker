"""Detect how an account bills from the shape of the cards it publishes.

Mirrors the dashboard's card classification (``webapp/src/lib/quota.ts``): a
card that reports a percentage of a limit describes a subscription, a card
that reports a currency balance describes pay-as-you-go spend. A card that
only counts tokens says nothing about the billing model, so it never decides
one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SUBSCRIPTION = "subscription"
PAY_AS_YOU_GO = "pay_as_you_go"
UNKNOWN = "unknown"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def card_pct(card: Mapping[str, Any]) -> float | None:
    """The percentage a card reports, mirroring the dashboard's ``cardPct()``."""
    pct = _number(card.get("pct_used"))
    if pct is not None:
        return pct
    used = _number(card.get("used_value"))
    limit = _number(card.get("limit_value"))
    if used is None or limit is None or limit <= 0:
        return None
    return used / limit * 100


def card_billing_type(card: Mapping[str, Any]) -> str:
    """The billing model a single card implies (``unknown`` when it implies none)."""
    if card_pct(card) is not None:
        return SUBSCRIPTION
    unit_type = card.get("unit_type")
    if unit_type in ("currency", "credits") or card.get("currency"):
        return PAY_AS_YOU_GO
    return UNKNOWN


def detect_billing_type(cards: Iterable[Mapping[str, Any]]) -> str:
    """The billing model implied by the cards an account publishes.

    A quota card wins over a spend card: an account publishing both is a plan
    with prepaid usage on top, not a pay-as-you-go account.
    """
    detected = UNKNOWN
    for card in cards:
        kind = card_billing_type(card)
        if kind == SUBSCRIPTION:
            return SUBSCRIPTION
        if kind == PAY_AS_YOU_GO:
            detected = PAY_AS_YOU_GO
    return detected
