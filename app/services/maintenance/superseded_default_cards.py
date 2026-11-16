"""Drop the live cards a real account has superseded under the generic `default` account.

A server collector that ran without its provider config (for example while the
database was locked, see `CollectorManager.sync_collectors`) can write `default`
cards for a provider whose real, account-keyed card is refreshed every cycle.
Nothing retires those rows afterwards, so the dashboard shows a second, stale
card per provider.

Scope is deliberately narrow: only `latest_usage` and its
`latest_usage_contributions` for `(provider_id, 'default')`. Quota snapshots,
usage events and rollups under `default` are history, handled by
`orphan_gauge_series` / `lone_default_events`.

A provider qualifies only when all of these hold (re-checked by `plan` and
`apply`, so a stale preview cannot delete a card that came back to life):

- no `ProviderConfig` row is keyed `default` for it (a configured `default`
  account owns its cards);
- none of its `default` cards has been refreshed within `STALE_AFTER`;
- a real (non-`default`) account has a healthy card refreshed within
  `STALE_AFTER`, i.e. the provider is demonstrably alive under a real identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from app.models.db import LatestUsage, LatestUsageContribution, ProviderConfig
from app.services.accumulator import is_error_card_json
from app.services.maintenance.chunked_sql import chunked_delete

# Matches the one-hour staleness window of latest-usage contributions and of
# `accumulator_upsert.evict_superseded_error_rows`.
STALE_AFTER = timedelta(hours=1)

DEFAULT_ACCOUNT = "default"


@dataclass
class SupersededDefaultCards:
    provider_id: str
    card_count: int
    last_updated: datetime
    live_accounts: list[str] = field(default_factory=list)
    slots: list[str] = field(default_factory=list)


@dataclass
class DeleteResult:
    latest_usage_deleted: int = 0
    contributions_deleted: int = 0


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _slot(row: LatestUsage) -> str:
    return "/".join(part for part in (row.window_type, row.variant, row.model_id) if part)


def _inspect(
    session: Session, provider_id: str, *, now: datetime | None = None
) -> SupersededDefaultCards | None:
    """Return the superseded `default` cards of one provider, or None if it does not qualify."""
    now = now or datetime.now(UTC)
    cutoff = now - STALE_AFTER

    if (
        session.exec(
            select(ProviderConfig.id).where(
                col(ProviderConfig.provider_id) == provider_id,
                col(ProviderConfig.account_id) == DEFAULT_ACCOUNT,
            )
        ).first()
        is not None
    ):
        return None

    default_rows = session.exec(
        select(LatestUsage).where(
            col(LatestUsage.provider_id) == provider_id,
            col(LatestUsage.account_id) == DEFAULT_ACCOUNT,
        )
    ).all()
    if not default_rows:
        return None
    last_updated = max(_aware(row.updated_at) for row in default_rows)
    if last_updated >= cutoff:
        return None

    live_accounts = sorted(
        {
            row.account_id
            for row in session.exec(
                select(LatestUsage).where(
                    col(LatestUsage.provider_id) == provider_id,
                    col(LatestUsage.account_id) != DEFAULT_ACCOUNT,
                )
            ).all()
            if _aware(row.updated_at) >= cutoff and not is_error_card_json(row.card_json)
        }
    )
    if not live_accounts:
        return None

    return SupersededDefaultCards(
        provider_id=provider_id,
        card_count=len(default_rows),
        last_updated=last_updated,
        live_accounts=live_accounts,
        slots=sorted({_slot(row) for row in default_rows}),
    )


def find_superseded(session: Session) -> list[SupersededDefaultCards]:
    """Every provider with superseded `default` cards (read-only)."""
    provider_ids = sorted(
        set(
            session.exec(
                select(LatestUsage.provider_id).where(
                    col(LatestUsage.account_id) == DEFAULT_ACCOUNT
                )
            ).all()
        )
    )
    found = (_inspect(session, provider_id) for provider_id in provider_ids)
    return [item for item in found if item is not None]


def plan_delete(session: Session, *, provider_id: str) -> SupersededDefaultCards:
    """Preview of `apply_delete`; raises ValueError when the provider no longer qualifies."""
    superseded = _inspect(session, provider_id)
    if superseded is None:
        raise ValueError(
            f"{provider_id}: no superseded default cards "
            "(refreshed recently, configured, or no live real account)"
        )
    return superseded


def apply_delete(session: Session, *, provider_id: str) -> DeleteResult:
    """Delete the provider's `default` cards and their contributions."""
    plan_delete(session, provider_id=provider_id)
    latest_deleted = chunked_delete(
        session,
        LatestUsage,
        [
            col(LatestUsage.provider_id) == provider_id,
            col(LatestUsage.account_id) == DEFAULT_ACCOUNT,
        ],
    )
    contributions_deleted = chunked_delete(
        session,
        LatestUsageContribution,
        [
            col(LatestUsageContribution.provider_id) == provider_id,
            col(LatestUsageContribution.account_id) == DEFAULT_ACCOUNT,
        ],
    )
    return DeleteResult(
        latest_usage_deleted=latest_deleted, contributions_deleted=contributions_deleted
    )
