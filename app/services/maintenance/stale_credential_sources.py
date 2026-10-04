"""Remove machine-reported `credential_sources` rows nothing reports any more —
the Data Health `stale_credential_sources` fixer.

A sidecar re-reports every credential it finds each cycle (`last_seen`), and
nothing ever deletes a row that stops being reported: a file rule that was
dropped, a machine that was retired, a credential that was removed. Two shapes
are safe to forget — each comes straight back on the machine's next report if
the credential is in fact still there:

* **stale** — machine-reported (has a `sidecar_id`) and not seen for
  `STALE_AFTER`. The window is deliberately long so a laptop that is simply off
  for a week keeps its per-source settings.
* **not a credential** — a fixed `provider:<id>` origin (an exec rule) that
  never carried a token type. Older sidecars reported metadata-only lookups
  (e.g. `git config user.email`) this way, one untried "Sidecar credential" row
  per machine.

`config:` and server (env/file) rows are never touched: they are managed in
Settings / the server environment, not by machine reports. `apply` re-derives
what `plan` reported rather than trusting a stale preview. `apply_stale_sources`
owns its commit — callers must not commit again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from app.models.db import CredentialSource

STALE_AFTER = timedelta(days=14)
PROVIDER_ORIGIN_PREFIX = "provider:"


@dataclass(frozen=True)
class StaleSource:
    row_id: int
    provider_id: str
    account_id: str
    source_id: str
    source_label: str
    sidecar_id: str | None
    kind: str  # stale | not_a_credential

    @property
    def label(self) -> str:
        return f"{self.source_label} · {self.sidecar_id or self.account_id}"


@dataclass
class StaleSourcesPlan:
    provider_id: str
    sources: list[StaleSource] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.sources)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "stale": sum(s.kind == "stale" for s in self.sources),
            "not_a_credential": sum(s.kind == "not_a_credential" for s in self.sources),
        }


def _has_token_types(row: CredentialSource) -> bool:
    if not row.token_types_json:
        return False
    try:
        return bool(json.loads(row.token_types_json))
    except ValueError:
        return False


def _is_stale(last_seen: datetime | None, now: datetime) -> bool:
    # A row that never recorded a sighting (older code paths) is as stale as it gets.
    if last_seen is None:
        return True
    aware = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=UTC)
    return now - aware > STALE_AFTER


def plan_stale_sources(
    session: Session, provider_id: str, *, now: datetime | None = None
) -> StaleSourcesPlan:
    now = now or datetime.now(UTC)
    rows = session.exec(
        select(CredentialSource).where(
            col(CredentialSource.provider_id) == provider_id,
            col(CredentialSource.sidecar_id).is_not(None),
        )
    ).all()
    plan = StaleSourcesPlan(provider_id=provider_id)
    for row in rows:
        if row.id is None or row.source_id.startswith("config:") or row.source_type == "server":
            continue
        kind: str | None = None
        if (row.credential_origin or "").startswith(PROVIDER_ORIGIN_PREFIX) and not (
            _has_token_types(row)
        ):
            kind = "not_a_credential"
        elif _is_stale(row.last_seen, now):
            kind = "stale"
        if kind:
            plan.sources.append(
                StaleSource(
                    row_id=row.id,
                    provider_id=row.provider_id,
                    account_id=row.account_id,
                    source_id=row.source_id,
                    source_label=row.source_label,
                    sidecar_id=row.sidecar_id,
                    kind=kind,
                )
            )
    return plan


def apply_stale_sources(session: Session, provider_id: str) -> StaleSourcesPlan:
    """Delete exactly what :func:`plan_stale_sources` reports right now. Commits."""
    plan = plan_stale_sources(session, provider_id)
    for stale in plan.sources:
        row = session.get(CredentialSource, stale.row_id)
        if row is not None:
            session.delete(row)
    if plan.sources:
        session.commit()
    return plan
