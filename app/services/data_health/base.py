"""Shared types for a Data Health check: a read-only `detect`, a read-only
`plan` for one fixable group of findings, and a committing `apply` for that
same group — the contract every module in `app/services/data_health/checks/`
implements, backed by the repair services in `app/services/maintenance/`.

Findings are reported through `Finding` — an explicit whitelist of primitive
fields a check chooses to surface, never a serialized ORM row. A
`ProviderConfig` row carries encrypted credential columns (the API key, the
session cookie); a `UsageEvent` sample is safe to show in full, but nothing
here assumes that by default — each check module decides, field by field,
what it puts in a `Finding.detail`. See #364 for the class of leak this
guards against.

A check's findings can span several independently-fixable groups (e.g.
`legacy_provider_ids` has one group per legacy provider id; a group with no
sensible fix target is reported `fixable=False` with a reason, like
opencode-byok's untargetable lone `default` events, rather than omitted).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlmodel import Session

AsyncHook = Callable[[], Awaitable[None]]


class Severity(StrEnum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    """One secret-safe sample row. `detail` values must be JSON-primitive
    (str/int/float/bool/None) — never a credential, cookie, or raw model."""

    label: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParamSpec:
    """Describes one parameter a group's `plan`/`apply` call needs. `options`,
    when given, is the exhaustive server-validated choice set (e.g. the
    provider's other configured account ids) — the UI renders a picker, not
    free text."""

    name: str
    label: str
    required: bool = True
    options: list[str] | None = None


@dataclass
class FindingGroup:
    """One independently-fixable slice of a check's findings — e.g. one
    legacy provider id, or one stray account under one provider."""

    key: str
    label: str
    count: int
    fixable: bool
    not_fixable_reason: str | None = None
    params: list[ParamSpec] = field(default_factory=list)
    samples: list[Finding] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckReport:
    check_id: str
    severity: Severity
    total_count: int
    groups: list[FindingGroup] = field(default_factory=list)
    # Populated by the registry/jobs layer, not by the check itself — a
    # check has no way to know another check's live findings.
    blocked_by: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)

    @property
    def fixable_count(self) -> int:
        return sum(g.count for g in self.groups if g.fixable)


@dataclass
class FixPlan:
    check_id: str
    group_key: str
    summary: str
    counts: dict[str, Any] = field(default_factory=dict)
    samples: list[Finding] = field(default_factory=list)
    confirmation_text: str | None = None


@dataclass
class FixResult:
    check_id: str
    group_key: str
    summary: str
    counts: dict[str, Any] = field(default_factory=dict)


class Check(ABC):
    """A Data Health check. Instances are stateless and long-lived (one per
    process, held in `registry.py`) — all per-request state lives in the
    `Session` and `params` arguments, never on `self`."""

    id: str
    title: str
    description: str
    impact: str
    recommended_action: str
    severity: Severity
    # check ids whose *unresolved* findings must be fixed before this
    # check's own fixer is allowed to run — enforced by jobs.py, not here.
    blocked_by: tuple[str, ...] = ()

    @abstractmethod
    def detect(self, session: Session) -> CheckReport:
        """Read-only. Must not write to `session`."""

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        """Read-only preview of `apply` for one group. Default: not fixable."""
        raise NotImplementedError(f"{self.id!r} has no fixer for group {group_key!r}")

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        """Commits. Returns `(result, hooks)` — `hooks` are async callables
        the caller (jobs.py, which owns the event loop) must await
        afterward. Default: not fixable."""
        raise NotImplementedError(f"{self.id!r} has no fixer for group {group_key!r}")
