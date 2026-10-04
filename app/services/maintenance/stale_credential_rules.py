"""Remove operator assignment rules (`credential_tags`) that no machine matches any
more — the Data Health `stale_credential_rules` fixer.

A rule maps a host-side credential origin to an account. When the credential it
described stops being reported (a file rule dropped from the sidecar, a machine
retired, a CLI logged out) nothing deletes the rule; it just sits in the Rules tab.

What counts as a *match* is exactly what attribution uses: every machine-reported
`credential_sources` row is resolved with `credential_inventory._resolve_tag` (exact
origin, then its plain form; a machine-scoped rule beats an all-machines one), and the
winning rule is "used" at that row's `last_seen`. A rule shadowed everywhere by
machine-scoped rules is therefore unused too. A plain origin join would be wrong for
`#fingerprint` origins.

Deliberately **not** considered, because a "no matching credential" test is wrong
for them and deleting one would silently break attribution:

* `provider:<id>` rules — they attribute usage *events* and are the sidecar's
  fallback hint for any credential on a machine, not a claim on one credential;
* redirect rules (`target_provider_id` set) — events are rewritten to the target;
* automatic rules (`set_by` identity_claim / identity_verification / rotation) —
  they manage themselves;
* `config:` / server origins — never reported by a machine;
* rules younger than the window.

A machine-scoped rule is only judged while its machine is clearly alive (it has
reported within the last day) so an offline laptop is not penalised; an all-machines
rule is only judged while at least one machine is alive. Removing a rule that was
still needed is loud, not silent: the credential reappears under "Needs mapping".

Known gap: `stale_credential_sources` deletes the very rows this check reads as
evidence. Both use the same window, so the outcome is the same.

`apply_stale_rules` re-derives what `plan_stale_rules` reported and owns its commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from app.models.db import CredentialSource, CredentialTag, SidecarRegistry
from app.services.credential_inventory import _resolve_tag
from app.services.credential_tags import live_sidecar_ids
from app.services.maintenance.stale_credential_sources import STALE_AFTER

AUTOMATIC_SET_BY = frozenset({"identity_claim", "identity_verification", "rotation"})
# A machine-scoped rule is only judged while its machine reported this recently.
MACHINE_ALIVE_WITHIN = timedelta(days=1)
PROVIDER_ORIGIN_PREFIX = "provider:"


@dataclass(frozen=True)
class StaleRule:
    row_id: int
    provider_id: str
    credential_origin: str
    account_id: str
    sidecar_id: str | None
    last_matched_at: datetime | None

    @property
    def scope(self) -> str:
        return self.sidecar_id or "all machines"


@dataclass
class StaleRulesPlan:
    provider_id: str
    rules: list[StaleRule] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rules)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "never_matched": sum(r.last_matched_at is None for r in self.rules),
            "not_matched_recently": sum(r.last_matched_at is not None for r in self.rules),
        }


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def rule_usage(session: Session) -> dict[int, datetime]:
    """``tag id -> last_seen of the freshest machine-reported source it resolved``."""
    tags: dict[tuple[str, str], list[CredentialTag]] = {}
    for tag in session.exec(select(CredentialTag)).all():
        tags.setdefault((tag.provider_id, tag.credential_origin), []).append(tag)

    usage: dict[int, datetime] = {}
    rows = session.exec(
        select(CredentialSource).where(col(CredentialSource.sidecar_id).is_not(None))
    ).all()
    for row in rows:
        if row.source_id.startswith("config:") or row.source_type == "server":
            continue
        winner = _resolve_tag(tags, row)
        if winner is None or winner.id is None:
            continue
        seen = _aware(row.last_seen)
        if winner.id not in usage or seen > usage[winner.id]:
            usage[winner.id] = seen
    return usage


def _judgeable(tag: CredentialTag) -> bool:
    return (
        tag.id is not None
        and tag.set_by not in AUTOMATIC_SET_BY
        and tag.target_provider_id is None
        and not tag.credential_origin.startswith(PROVIDER_ORIGIN_PREFIX)
        and not tag.credential_origin.startswith("config:")
    )


def find_stale_rules(
    session: Session,
    *,
    provider_id: str | None = None,
    now: datetime | None = None,
    usage: dict[int, datetime] | None = None,
) -> list[StaleRule]:
    """Rules the check would list. Pass ``usage`` (from :func:`rule_usage`) to reuse it."""
    now = now or datetime.now(UTC)
    live = set(live_sidecar_ids(session))
    alive_cutoff = now - MACHINE_ALIVE_WITHIN
    alive = {
        row.sidecar_id
        for row in session.exec(
            select(SidecarRegistry).where(col(SidecarRegistry.sidecar_id).in_(live))
        ).all()
        if _aware(row.last_seen) >= alive_cutoff
    }
    if not alive:
        # Nothing has reported in the last day, so absence of a match proves nothing —
        # for an all-machines rule as much as for a machine-scoped one.
        return []

    if usage is None:
        usage = rule_usage(session)
    query = select(CredentialTag)
    if provider_id is not None:
        query = query.where(col(CredentialTag.provider_id) == provider_id)
    stale: list[StaleRule] = []
    for tag in session.exec(query).all():
        if not _judgeable(tag) or tag.id is None:
            continue
        if tag.set_at and now - _aware(tag.set_at) <= STALE_AFTER:
            continue
        if tag.sidecar_id is not None and tag.sidecar_id not in alive:
            continue
        last = usage.get(tag.id)
        if last is not None and now - last <= STALE_AFTER:
            continue
        stale.append(
            StaleRule(
                row_id=tag.id,
                provider_id=tag.provider_id,
                credential_origin=tag.credential_origin,
                account_id=tag.account_id,
                sidecar_id=tag.sidecar_id,
                last_matched_at=last,
            )
        )
    return sorted(stale, key=lambda r: (r.provider_id, r.credential_origin, r.sidecar_id or ""))


def plan_stale_rules(
    session: Session, provider_id: str, *, now: datetime | None = None
) -> StaleRulesPlan:
    return StaleRulesPlan(
        provider_id=provider_id, rules=find_stale_rules(session, provider_id=provider_id, now=now)
    )


def apply_stale_rules(session: Session, provider_id: str) -> StaleRulesPlan:
    """Delete exactly what :func:`plan_stale_rules` reports right now. Commits."""
    plan = plan_stale_rules(session, provider_id)
    for stale in plan.rules:
        row = session.get(CredentialTag, stale.row_id)
        if row is not None:
            session.delete(row)
    if plan.rules:
        session.commit()
    return plan
