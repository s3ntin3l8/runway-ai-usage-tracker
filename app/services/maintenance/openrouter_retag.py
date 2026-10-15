"""Move OpenRouter-served OpenCode events onto the `openrouter` provider — the Data
Health `openrouter_model_events` fixer.

OpenCode tags a message with the *gateway* it went through (`providerID`), not the
backend that served it. A `vendor/model:free` or `openrouter/...` model id can only
come from OpenRouter, yet older sidecars (and BYOK setups) filed such messages under
`opencode`, `opencode-free` or `opencode-byok`. The BYOK ones landed under `default`,
which `lone_default_events` cannot fix: it keeps the provider and `opencode-byok` has
no other account.

The same message is often present under two of those providers (an `opencode` row and
an `opencode-byok` one), and the `(provider_id, event_id)` unique index only separates
them while their provider ids differ. Retagging both onto `openrouter` would collide,
so duplicates are resolved first — within the source set, then against rows already
under `openrouter` — with the same tie-break as `legacy_retag.pick_winner`.

The target account must be unambiguous: exactly one non-default `openrouter` account.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, and_, func, or_
from sqlmodel import Session, col, select

from app.models.db import UsageEvent
from app.services.data_health._provider_accounts import candidate_targets
from app.services.maintenance._chunked_sql import chunked_update
from app.services.maintenance.legacy_retag import pick_winner
from app.services.maintenance.rollups import rebuild_rollups_for_pairs
from app.services.maintenance.windows import rebuild_windows_for_providers

TARGET_PROVIDER_ID = "openrouter"
# Gateways OpenCode files OpenRouter traffic under.
SOURCE_PROVIDER_IDS: tuple[str, ...] = ("opencode", "opencode-free", "opencode-byok")

_BATCH = 500


def model_clause() -> ColumnElement[bool]:
    """SQL twin of `model_normalization.is_openrouter_model_id` (LIKE is ASCII
    case-insensitive in SQLite, matching the predicate's lowercasing)."""
    model = col(UsageEvent.model_id)
    return or_(
        model.like("openrouter/%"),
        and_(model.like("%/%"), model.like("%:free")),
    )


def _source_where(provider_id: str | None = None) -> list[ColumnElement[bool]]:
    providers = (provider_id,) if provider_id else SOURCE_PROVIDER_IDS
    return [col(UsageEvent.provider_id).in_(providers), model_clause()]


class NoTargetAccountError(ValueError):
    """There is no single non-default `openrouter` account to move events onto."""


def resolve_target(session: Session) -> str:
    candidates = candidate_targets(session, TARGET_PROVIDER_ID)
    if len(candidates) != 1:
        raise NoTargetAccountError(
            f"Need exactly one non-default {TARGET_PROVIDER_ID} account to move events onto; "
            f"found {len(candidates)}"
        )
    return candidates[0]


@dataclass
class OpenRouterRetagPlan:
    provider_id: str = ""
    target_account: str | None = None
    total: int = 0
    duplicates: int = 0  # an event_id that also exists elsewhere in the set / under openrouter
    retagged: int = 0  # total minus duplicates dropped
    by_model: dict[str, int] = field(default_factory=dict)
    samples: list[UsageEvent] = field(default_factory=list)


@dataclass
class OpenRouterRetagResult:
    target_account: str = ""
    retagged: int = 0
    duplicates_resolved: int = 0
    rollup_pairs_rebuilt: int = 0
    windows_rebuilt: int = 0


def _duplicate_event_ids(session: Session, provider_id: str | None = None) -> set[str]:
    """Event ids that would collide once the source rows move to `openrouter`.

    With `provider_id` set, the ids of that provider's rows only (the preview is
    per source provider); without, the whole source set (what apply resolves).
    """
    scope = _source_where(provider_id)
    in_openrouter = {
        row[0]
        for row in session.execute(
            select(UsageEvent.event_id)
            .where(*scope)
            .intersect(
                select(UsageEvent.event_id).where(col(UsageEvent.provider_id) == TARGET_PROVIDER_ID)
            )
        )
    }
    # An id present under two source providers (counted against the whole set, so a
    # per-provider preview still sees the sibling copy).
    sibling_ids = {
        row[0]
        for row in session.execute(
            select(UsageEvent.event_id)
            .where(*_source_where())
            .group_by(col(UsageEvent.event_id))
            .having(func.count() > 1)
        )
    }
    if provider_id is None:
        return in_openrouter | sibling_ids
    own = {row[0] for row in session.execute(select(UsageEvent.event_id).where(*scope))}
    return in_openrouter | (sibling_ids & own)


def plan_openrouter_retag(
    session: Session, provider_id: str, *, sample_size: int = 5
) -> OpenRouterRetagPlan:
    """Read-only preview for one source provider."""
    where = _source_where(provider_id)
    total = session.execute(select(func.count()).where(*where)).one()[0]
    by_model = {
        row[0]: row[1]
        for row in session.execute(
            select(UsageEvent.model_id, func.count())
            .where(*where)
            .group_by(col(UsageEvent.model_id))
            .order_by(func.count().desc())
        )
    }
    duplicates = len(_duplicate_event_ids(session, provider_id)) if total else 0
    try:
        target: str | None = resolve_target(session)
    except NoTargetAccountError:
        target = None
    samples = list(
        session.exec(
            select(UsageEvent).where(*where).order_by(col(UsageEvent.ts)).limit(sample_size)
        )
    )
    return OpenRouterRetagPlan(
        provider_id=provider_id,
        target_account=target,
        total=total,
        duplicates=duplicates,
        retagged=total - duplicates,
        by_model=by_model,
        samples=samples,
    )


def _resolve_duplicates(session: Session) -> tuple[int, set[tuple[str, str]]]:
    """Delete the losing copy of every colliding event. Returns the count and the
    (provider, account) pairs the deleted rows belonged to, whose rollups go stale."""
    resolved = 0
    pairs: set[tuple[str, str]] = set()
    ids = sorted(_duplicate_event_ids(session))
    for start in range(0, len(ids), _BATCH):
        batch = ids[start : start + _BATCH]
        groups: dict[str, list[UsageEvent]] = {}
        # Source rows plus any copy already under openrouter (whatever its model id).
        rows = session.exec(
            select(UsageEvent).where(
                col(UsageEvent.event_id).in_(batch),
                or_(
                    and_(*_source_where()),
                    col(UsageEvent.provider_id) == TARGET_PROVIDER_ID,
                ),
            )
        )
        for ev in rows:
            groups.setdefault(ev.event_id, []).append(ev)
        for copies in groups.values():
            if len(copies) < 2:
                continue  # raced away since the scan; the next scan picks it up
            winner = copies[0]
            for other in copies[1:]:
                winner, loser = pick_winner(winner, other)
                pairs.add((loser.provider_id, loser.account_id))
                session.delete(loser)
                resolved += 1
        session.commit()
        session.expunge_all()
    return resolved, pairs


def apply_openrouter_retag(session: Session, provider_id: str) -> OpenRouterRetagResult:
    """Move every OpenRouter-served event under `provider_id` onto `openrouter`.

    Duplicates across the whole source set are resolved first (so `provider_id`'s
    fix never leaves a sibling provider's copy to collide later), then the rest are
    retagged to the single target account with the source-reported cost preserved
    (`cost_reported_usd`) the way `legacy_retag` does. Every write is chunked and
    committed per batch, and the whole thing is resumable. Rollups for every touched
    (provider, account) pair and the closed windows of every touched provider are
    rebuilt from events afterwards.
    """
    target = resolve_target(session)
    result = OpenRouterRetagResult(target_account=target)

    result.duplicates_resolved, touched_pairs = _resolve_duplicates(session)
    touched_pairs.add((TARGET_PROVIDER_ID, target))
    for source_provider, account in session.execute(
        select(UsageEvent.provider_id, UsageEvent.account_id)
        .where(*_source_where(provider_id))
        .distinct()
    ):
        touched_pairs.add((source_provider, account))

    result.retagged = chunked_update(
        session,
        UsageEvent,
        _source_where(provider_id),
        {
            "provider_id": TARGET_PROVIDER_ID,
            "account_id": target,
            "attribution_source": "tag",
            "cost_reported_usd": func.coalesce(UsageEvent.cost_reported_usd, UsageEvent.cost_usd),
        },
    )

    rebuild_rollups_for_pairs(session, touched_pairs)
    session.commit()
    result.rollup_pairs_rebuilt = len(touched_pairs)
    result.windows_rebuilt = rebuild_windows_for_providers(
        session, sorted({provider for provider, _account in touched_pairs})
    )
    return result
