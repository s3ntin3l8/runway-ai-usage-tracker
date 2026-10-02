"""Remove `credential_sources` rows stranded on an account with nothing
behind it — the Data Health `orphan_credential_sources` fixer.

`credential_sources` keys every row on `(provider_id, account_id, source_id)`,
so an account rename that does not carry its credential rows along (see
`config_rekey.py`, whose `apply_rekey_config` now moves them; this repair is
for the rows an older build, or a rename done outside the app, already left
behind) produces two shapes of orphan:

* **config ghost** — a `config:` row, whose id embeds its account
  (`config:{provider}:{account_id}`) and is written only alongside a
  `provider_configs` row. No config for that account, no claim.
* **duplicate** — the same `source_id` filed under two accounts, one of them
  an account with no configuration, quota card, usage or operator-set
  tag/label (a credential-sources-only identity — the one Settings renders as
  a second identity beside the real one). The copy under the account that
  still has evidence stays.

Both are pure deletions: nothing is retargeted, and `apply` re-derives the
same rows `plan` reported rather than trusting a stale preview. Rows whose
account has evidence — including a copy under a second *real* account — are
operator territory and are never touched. A group where no copy has a real
home is left alone entirely: with no evidence either way there is nothing to
say which copy is the live one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, col, select

from app.models.db import CredentialSource, ProviderConfig
from app.services.credential_sources import real_account_ids

CONFIG_SOURCE_PREFIX = "config:"


@dataclass(frozen=True)
class OrphanSource:
    """One credential source row to delete, as plain values (never a row)."""

    row_id: int
    provider_id: str
    account_id: str
    source_id: str
    source_label: str
    sidecar_id: str | None
    kind: str  # config_ghost | duplicate

    @property
    def label(self) -> str:
        where = self.sidecar_id or self.account_id
        return f"{self.source_label} · {where}"


@dataclass
class OrphanSourcesPlan:
    provider_id: str
    orphans: list[OrphanSource] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.orphans)

    @property
    def config_ghosts(self) -> int:
        return sum(1 for o in self.orphans if o.kind == "config_ghost")

    @property
    def duplicates(self) -> int:
        return sum(1 for o in self.orphans if o.kind == "duplicate")

    @property
    def counts(self) -> dict[str, int]:
        return {
            "config_ghosts": self.config_ghosts,
            "duplicates": self.duplicates,
            "sources_deleted": self.total,
        }


def _as_orphan(row: CredentialSource, kind: str) -> OrphanSource:
    assert row.id is not None  # a table row always has its primary key here
    return OrphanSource(
        row_id=row.id,
        provider_id=row.provider_id,
        account_id=row.account_id,
        source_id=row.source_id,
        source_label=row.source_label,
        sidecar_id=row.sidecar_id,
        kind=kind,
    )


def find_orphan_sources(session: Session, provider_id: str) -> list[OrphanSource]:
    """Read-only: every row for *provider_id* an apply would delete."""
    rows = list(
        session.exec(
            select(CredentialSource)
            .where(col(CredentialSource.provider_id) == provider_id)
            .order_by(col(CredentialSource.id))
        ).all()
    )
    if not rows:
        return []

    configured = set(
        session.exec(
            select(ProviderConfig.account_id).where(col(ProviderConfig.provider_id) == provider_id)
        ).all()
    )
    real = real_account_ids(session, provider_id)

    orphans: list[OrphanSource] = []
    by_source: dict[str, list[CredentialSource]] = {}
    for row in rows:
        if row.source_id.startswith(CONFIG_SOURCE_PREFIX):
            if row.account_id not in configured:
                orphans.append(_as_orphan(row, "config_ghost"))
            continue  # a config id embeds its account: never filed twice
        by_source.setdefault(row.source_id, []).append(row)

    for group in by_source.values():
        if len(group) < 2 or not any(r.account_id in real for r in group):
            continue
        orphans.extend(_as_orphan(row, "duplicate") for row in group if row.account_id not in real)
    return orphans


def plan_orphan_sources(session: Session, provider_id: str) -> OrphanSourcesPlan:
    """Read-only preview of :func:`apply_orphan_sources`."""
    return OrphanSourcesPlan(
        provider_id=provider_id, orphans=find_orphan_sources(session, provider_id)
    )


def apply_orphan_sources(session: Session, provider_id: str) -> OrphanSourcesPlan:
    """Delete exactly what :func:`find_orphan_sources` reports right now. Commits."""
    plan = plan_orphan_sources(session, provider_id)
    if not plan.orphans:
        return plan
    for orphan in plan.orphans:
        row = session.get(CredentialSource, orphan.row_id)
        if row is not None:
            session.delete(row)
    session.commit()
    return plan
