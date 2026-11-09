"""Allowance spend that no tracked usage event explains (Ollama).

Ollama's ``/api/balance`` debits for every client, but ``usage_events`` only
holds what Runway's sidecars collected. The gap between the two is shown on the
allowance card. It is computed here, where the card meets the event store (a
collector has no DB session), and only by the fleet view.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.core.date_utils import parse_iso8601_utc
from app.services.queries.shared import sqlite_utc_timestamp

MIN_UNTRACKED_USD = 0.05
MIN_UNTRACKED_FRACTION = 0.05


def untracked_spend_usd(
    session: Session, card: dict[str, Any], *, now: datetime | None = None
) -> float | None:
    """Return the unexplained allowance spend for an Ollama allowance card, or None."""
    if card.get("provider_id") != "ollama" or card.get("variant") != "allowance":
        return None
    spent, raw_from = card.get("used_value"), card.get("period_from")
    if not isinstance(spent, (int, float)) or not raw_from or not card.get("account_id"):
        return None
    try:
        since = parse_iso8601_utc(str(raw_from))
    except ValueError:
        return None
    until = now or datetime.now(UTC)
    row = session.execute(
        text(
            "SELECT COALESCE(SUM(cost_usd), 0), "
            "COALESCE(SUM(CASE WHEN cost_usd > 0 THEN 1 ELSE 0 END), 0) "
            "FROM usage_events WHERE provider_id = 'ollama' AND account_id = :aid "
            "AND kind = 'message' AND ts >= :since AND ts <= :until"
        ),
        {
            "aid": card["account_id"],
            "since": sqlite_utc_timestamp(since),
            "until": sqlite_utc_timestamp(until),
        },
    ).one()
    tracked, priced = float(row[0]), int(row[1])
    if priced == 0:
        return None  # no priced events: not measurable
    untracked = float(spent) - tracked
    if untracked <= max(MIN_UNTRACKED_USD, MIN_UNTRACKED_FRACTION * float(spent)):
        return None
    return round(untracked, 2)


def apply_untracked_spend(session: Session, cards: list[dict[str, Any]]) -> None:
    """Set ``untracked_spend_usd`` (and the detail suffix) on qualifying cards, in place."""
    for card in cards:
        if card.get("provider_id") != "ollama" or card.get("variant") != "allowance":
            continue
        value = untracked_spend_usd(session, card)
        card["untracked_spend_usd"] = value
        if value is None:
            continue
        suffix = f"${value:.2f} not from tracked clients"
        detail = card.get("detail") or ""
        card["detail"] = f"{detail} · {suffix}" if detail else suffix
