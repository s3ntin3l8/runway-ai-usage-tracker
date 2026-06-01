"""Compute cost_usd for a usage event using provider_pricing."""

import re
from datetime import datetime

from sqlmodel import Session, select

from app.models.db import ProviderPricing

# Trailing "-<major>(.<minor>...)" version suffix, e.g. "opus-4.8" -> "opus".
_VERSION_SUFFIX = re.compile(r"-\d+(?:\.\d+)*$")


def _price_row(
    session: Session, provider_id: str, model_id: str, ts: datetime
) -> ProviderPricing | None:
    return session.exec(
        select(ProviderPricing)
        .where(
            ProviderPricing.provider_id == provider_id,
            ProviderPricing.model_id == model_id,
            ProviderPricing.effective_from <= ts.date(),
        )
        .order_by(ProviderPricing.effective_from.desc())  # type: ignore[attr-defined]
    ).first()


def compute_event_cost(
    session: Session,
    *,
    provider_id: str,
    model_id: str | None,
    ts: datetime,
    tokens_input: int,
    tokens_output: int,
    tokens_cache_read: int,
    tokens_cache_create: int,
    tokens_reasoning: int = 0,
) -> float:
    """Return USD cost for an event using the price row in effect at `ts`.

    Pricing is keyed on **UTC date** (`ts.date()`): two events 60 seconds apart
    that span midnight UTC may pick different price rows if a new
    `effective_from` falls on the second day. The provider_pricing table is
    designed for date-level price changes, not intraday — this is intentional.
    Aware datetimes in non-UTC timezones are NOT converted before the date
    extraction; callers pass UTC-aware timestamps (event ingestion stores
    `ts` in UTC, and `query_*` helpers preserve tz-awareness).

    Returns 0.0 when no pricing row matches. Reasoning tokens are billed at
    the output rate (Anthropic / OpenAI convention).
    """
    if not model_id:
        return 0.0
    row = _price_row(session, provider_id, model_id, ts)
    if row is None:
        # Versioned ids ("opus-4.8") have no dedicated pricing row, so strip
        # the version suffix and bill at the family rate ("opus"). Providers
        # that price per version (Gemini "pro-2.5", ChatGPT "gpt-5.4-mini")
        # match exactly above and never reach this fallback.
        family = _VERSION_SUFFIX.sub("", model_id)
        if family != model_id:
            row = _price_row(session, provider_id, family, ts)
    if row is None:
        return 0.0
    return round(
        tokens_input / 1_000_000 * row.input_per_mtok
        + (tokens_output + tokens_reasoning) / 1_000_000 * row.output_per_mtok
        + tokens_cache_read / 1_000_000 * row.cache_read_per_mtok
        + tokens_cache_create / 1_000_000 * row.cache_create_per_mtok,
        6,
    )
