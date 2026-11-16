"""Data Health `superseded_default_cards` — a stale `default` card next to a live real one.

Prod, 2026-11-14: OpenCode, Ollama Cloud, DeepSeek and GitHub Copilot each grew a
second greyed card under account `default` while the account-keyed card kept
refreshing. The rows come from a collector that ran for one cycle without its
config (or from a default error card that outlived its collector) and that
nothing retires. `orphan_gauge_series` ignores them for 30 days because they
were written recently; this check is the narrow, immediate cleanup for cards
only (snapshots and events are history and stay).
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.services.data_health.base import (
    AsyncHook,
    Check,
    CheckReport,
    Finding,
    FindingGroup,
    FixPlan,
    FixResult,
    Severity,
)
from app.services.maintenance.superseded_default_cards import (
    apply_delete,
    find_superseded,
    plan_delete,
)


class SupersededDefaultCardsCheck(Check):
    id = "superseded_default_cards"
    title = "A stale generic card sits next to the live one"
    description = (
        "A provider has cards under the generic “default” account that no collector has "
        "refreshed for over an hour, while a real account's card for the same provider is current."
    )
    impact = "The dashboard shows a duplicate, greyed-out card for the provider."
    recommended_action = (
        "Delete the stale generic cards. Quota history and usage events are not touched."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        groups = [
            FindingGroup(
                key=item.provider_id,
                label=f"{item.provider_id}: stale default card",
                count=item.card_count,
                fixable=True,
                samples=[
                    Finding(
                        label=f"{item.provider_id}/default",
                        detail={
                            "provider_id": item.provider_id,
                            "cards": item.card_count,
                            "slots": ", ".join(item.slots),
                            "last_updated": item.last_updated.isoformat(),
                            "live_accounts": ", ".join(item.live_accounts),
                        },
                    )
                ],
                detail={"live_accounts": item.live_accounts, "slots": item.slots},
            )
            for item in find_superseded(session)
        ]
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(group.count for group in groups),
            groups=groups,
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        item = plan_delete(session, provider_id=group_key)
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=(
                f"Delete {item.card_count} stale default card(s) for {item.provider_id}; "
                f"live: {', '.join(item.live_accounts)}"
            ),
            counts={"latest_usage": item.card_count},
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        result = apply_delete(session, provider_id=group_key)
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=f"Deleted the stale default cards for {group_key}",
                counts={
                    "latest_usage_deleted": result.latest_usage_deleted,
                    "contributions_deleted": result.contributions_deleted,
                },
            ),
            [],
        )
