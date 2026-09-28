"""Compute cost_usd for a usage event using provider_pricing."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import NamedTuple

from sqlmodel import Session, func, select

from app.models.db import ProviderPricing

logger = logging.getLogger(__name__)

# Trailing "-<major>(.<minor>...)" version suffix, e.g. "opus-4.8" -> "opus".
_VERSION_SUFFIX = re.compile(r"-\d+(?:\.\d+)*$")


class CostBreakdown(NamedTuple):
    """Per-component USD cost. `output` includes reasoning (billed at the output
    rate). `total` is the sum — equal to what `compute_event_cost` returns."""

    input: float
    output: float
    cache_read: float
    cache_create: float

    @property
    def total(self) -> float:
        return round(self.input + self.output + self.cache_read + self.cache_create, 6)


@dataclass(frozen=True, slots=True)
class PricingRowSnapshot:
    """Immutable pricing values safe to retain across session commits."""

    provider_id: str
    model_id: str
    effective_from: date
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_create_per_mtok: float
    cache_create_1h_per_mtok: float


class PricingIndex:
    """Preloaded provider pricing rows for bulk event consumers."""

    def __init__(self, rows: list[PricingRowSnapshot]) -> None:
        exact: dict[tuple[str, str], list[PricingRowSnapshot]] = defaultdict(list)
        insensitive: dict[tuple[str, str], list[PricingRowSnapshot]] = defaultdict(list)
        for row in rows:
            exact[(row.provider_id, row.model_id)].append(row)
            insensitive[(row.provider_id, row.model_id.lower())].append(row)
        self._exact = exact
        self._insensitive = insensitive

    @classmethod
    def load(cls, session: Session, providers: list[str] | None = None) -> PricingIndex:
        columns = (
            ProviderPricing.provider_id,
            ProviderPricing.model_id,
            ProviderPricing.effective_from,
            ProviderPricing.input_per_mtok,
            ProviderPricing.output_per_mtok,
            ProviderPricing.cache_read_per_mtok,
            ProviderPricing.cache_create_per_mtok,
            ProviderPricing.cache_create_1h_per_mtok,
        )
        statement = select(*columns)  # type: ignore[call-overload]
        if providers:
            statement = statement.where(ProviderPricing.provider_id.in_(providers))  # type: ignore[attr-defined]
        statement = statement.order_by(ProviderPricing.effective_from.desc())  # type: ignore[attr-defined]
        rows = [PricingRowSnapshot(*values) for values in session.execute(statement).all()]
        return cls(rows)

    @staticmethod
    def _at_or_before(
        rows: list[PricingRowSnapshot] | None, effective_on: date
    ) -> PricingRowSnapshot | None:
        if rows:
            return next((row for row in rows if row.effective_from <= effective_on), None)
        return None

    def exact(
        self, provider_id: str, model_id: str, effective_on: date
    ) -> PricingRowSnapshot | None:
        return self._at_or_before(self._exact.get((provider_id, model_id)), effective_on)

    def case_insensitive(
        self, provider_id: str, model_id: str, effective_on: date
    ) -> PricingRowSnapshot | None:
        return self._at_or_before(
            self._insensitive.get((provider_id, model_id.lower())), effective_on
        )


PricingRow = ProviderPricing | PricingRowSnapshot


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


def _price_row_ci(
    session: Session, provider_id: str, model_id: str, ts: datetime
) -> ProviderPricing | None:
    """Case-insensitive fallback for providers whose upstream model_id casing
    isn't guaranteed to match the seed exactly (e.g. OpenCode's `modelID` for
    MiniMax — "MiniMax-M3" today, but not contractually stable). Only tried
    after the exact and version-stripped lookups miss."""
    return session.exec(
        select(ProviderPricing)
        .where(
            ProviderPricing.provider_id == provider_id,
            func.lower(ProviderPricing.model_id) == model_id.lower(),
            ProviderPricing.effective_from <= ts.date(),
        )
        .order_by(ProviderPricing.effective_from.desc())  # type: ignore[attr-defined]
    ).first()


def resolve_price_row(
    session: Session,
    provider_id: str,
    model_id: str | None,
    ts: datetime,
    *,
    index: PricingIndex | None = None,
) -> PricingRow | None:
    """The price row `compute_event_cost_breakdown` would bill at, or `None`
    if nothing matches (including a falsy `model_id`) — the fallback chain as
    its own primitive so a caller can distinguish "no seeded row" (an
    unpriced model — see the Data Health `unpriced_models` check) from "a row
    exists and its rate is legitimately zero."
    """
    if not model_id:
        return None

    def exact_lookup(candidate: str) -> PricingRow | None:
        if index is not None:
            return index.exact(provider_id, candidate, ts.date())
        return _price_row(session, provider_id, candidate, ts)

    def insensitive_lookup(candidate: str) -> PricingRow | None:
        if index is not None:
            return index.case_insensitive(provider_id, candidate, ts.date())
        return _price_row_ci(session, provider_id, candidate, ts)

    row = exact_lookup(model_id)
    if row is None:
        # Versioned ids ("opus-4.8") have no dedicated pricing row, so strip
        # the version suffix and bill at the family rate ("opus"). Providers
        # that price per version (Gemini "pro-2.5", ChatGPT "gpt-5.4-mini")
        # match exactly above and never reach this fallback.
        family = _VERSION_SUFFIX.sub("", model_id)
        if family != model_id:
            row = exact_lookup(family)
    if row is None:
        # Codenamed/unseeded variants ("gpt-5.7-nova") don't end in a bare
        # digit, so _VERSION_SUFFIX above never fires. Progressively
        # right-trim "-"-separated segments and retry each ("gpt-5.7-nova" ->
        # "gpt-5.7"; note this cannot cross the dot to reach "gpt-5" — "." is
        # not a segment separator, so a dotted minor version stays intact),
        # so a brand-new slug we haven't seeded yet has a chance to land on
        # a seeded sibling/family row instead of silently billing $0.00 until
        # someone edits pricing_seed.py. Only runs when the exact and
        # version-stripped lookups above already missed, so it can never
        # change the price of an event that already resolves correctly today.
        # This is a fallback, not a substitute for seeding the real rate —
        # an unseeded variant may land on a family rate that doesn't match
        # its actual price.
        segments = model_id.split("-")
        while row is None and len(segments) > 1:
            segments.pop()
            trimmed = "-".join(segments)
            row = exact_lookup(trimmed)
            if row is not None:
                # This is the discoverability gap the fallback itself creates:
                # once a family row exists, every unseeded sibling silently
                # bills at its rate instead of $0.00 with no other signal.
                # Surface it so a real seeding gap doesn't go unnoticed.
                logger.warning(
                    "cost_calculator: %s/%s has no pricing row — billing at "
                    "the '%s' family rate via segment-trim fallback",
                    provider_id,
                    model_id,
                    trimmed,
                )
    if row is None:
        # Last resort: case-insensitive match on the exact id. One extra query,
        # only hit when all prior lookups miss.
        row = insensitive_lookup(model_id)
    return row


def compute_event_cost_breakdown(  # noqa: PLR0913 — one param per priced token dimension
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
    tokens_cache_create_1h: int = 0,
    tokens_cache_create_5m: int = 0,
    _resolved_price_row: PricingRow | None = None,
    _price_row_resolved: bool = False,
) -> CostBreakdown:
    """Per-component USD cost for an event using the price row in effect at `ts`.

    Pricing is keyed on **UTC date** (`ts.date()`): two events 60 seconds apart
    that span midnight UTC may pick different price rows if a new
    `effective_from` falls on the second day. The provider_pricing table is
    designed for date-level price changes, not intraday — this is intentional.
    Aware datetimes in non-UTC timezones are NOT converted before the date
    extraction; callers pass UTC-aware timestamps (event ingestion stores
    `ts` in UTC, and `query_*` helpers preserve tz-awareness).

    All components are 0.0 when no pricing row matches (see `resolve_price_row`
    to distinguish that from a legitimately-zero rate). Reasoning tokens are
    billed at the output rate (Anthropic / OpenAI convention).

    Cache writes: `tokens_cache_create` is priced in full at `cache_create_per_mtok`
    (the 5-minute-TTL rate) UNLESS the caller also breaks it down into
    `tokens_cache_create_1h`/`_5m` (Anthropic only, from JSONL `cache_creation.
    ephemeral_*_input_tokens`), in which case the 1h portion bills at
    `cache_create_1h_per_mtok` instead — falling back to the 5m rate if no
    dedicated 1h rate is seeded. When both split params are 0 (every other
    provider, and any event predating this split), behavior is unchanged from
    before this split existed.
    """
    row = (
        _resolved_price_row
        if _price_row_resolved
        else resolve_price_row(session, provider_id, model_id, ts)
    )
    if row is None:
        return CostBreakdown(0.0, 0.0, 0.0, 0.0)

    split_total = tokens_cache_create_1h + tokens_cache_create_5m
    if split_total:
        # Split known: price the 1h portion at its own rate (falling back to
        # the 5m rate if unseeded) and the 5m portion — plus any remainder not
        # accounted for by the split, e.g. rounding — at the 5m rate.
        rate_1h = row.cache_create_1h_per_mtok or row.cache_create_per_mtok
        remainder = max(tokens_cache_create - split_total, 0)
        cache_create_cost = round(
            tokens_cache_create_1h / 1_000_000 * rate_1h
            + (tokens_cache_create_5m + remainder) / 1_000_000 * row.cache_create_per_mtok,
            6,
        )
    else:
        # No split provided — treat the whole total as 5m-rate, identical to
        # every call site before this split was introduced.
        cache_create_cost = round(tokens_cache_create / 1_000_000 * row.cache_create_per_mtok, 6)

    return CostBreakdown(
        input=round(tokens_input / 1_000_000 * row.input_per_mtok, 6),
        output=round((tokens_output + tokens_reasoning) / 1_000_000 * row.output_per_mtok, 6),
        cache_read=round(tokens_cache_read / 1_000_000 * row.cache_read_per_mtok, 6),
        cache_create=cache_create_cost,
    )


def compute_event_cost(  # noqa: PLR0913 — one param per priced token dimension
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
    tokens_cache_create_1h: int = 0,
    tokens_cache_create_5m: int = 0,
) -> float:
    """Total USD cost for an event — the sum of `compute_event_cost_breakdown`."""
    return compute_event_cost_breakdown(
        session,
        provider_id=provider_id,
        model_id=model_id,
        ts=ts,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        tokens_cache_create_1h=tokens_cache_create_1h,
        tokens_cache_create_5m=tokens_cache_create_5m,
    ).total
