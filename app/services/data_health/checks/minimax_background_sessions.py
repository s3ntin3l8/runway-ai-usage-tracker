"""Data Health check — MiniMax Code background work filed as a conversation.

The MiniMax Code extractor files a background task's rows under the **parent**
session and tags them ``subagent_type="Explore"`` — the shape a Claude Code
sidechain row has. Each such row carries its provenance in ``raw_json``:

```json
{"source_session_id": "mvs_bg", "parent_session_id": "mvs_main",
 "agent_name": "explore", "purpose": "local-background-task:bg_1234"}
```

When a row carries a provenance that names a parent it is **not** filed under,
the work shows up in the Sessions view as a conversation of its own. The
provenance already says where it belongs, so the server can file it itself —
no runtime store needed, and no local detection.

What this check cannot see: rows collected *before* the extractor learned the
parent link carry no provenance at all. Only the MiniMax Code runtime store
can map those, so the repair script
(``scripts/link_minimax_background_sessions.py``) covers them.
"""

from __future__ import annotations

import json
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
from app.services.maintenance.minimax_background_sessions import (
    apply_provenance_relinks,
    plan_provenance_relinks,
)


def _group_key(child_session_id: str, parent_session_id: str) -> str:
    """The key of one (child, parent) group.

    The pair is encoded as a JSON array: a session id is unvalidated
    sidecar text, so no separator can be assumed absent — the pairs
    ``(a->b, c)`` and ``(a, b->c)`` would encode to the same string with
    one. The key travels in a JSON request body, so the encoding costs
    nothing.
    """
    return json.dumps([child_session_id, parent_session_id], separators=(",", ":"))


def _parse_group_key(group_key: str) -> tuple[str, str] | None:
    """Split a group key back into the (child, parent) pair.

    ``None`` when the key is not one of ours — a report cached across a
    deploy may hold a key from an older shape.
    """
    try:
        pair = json.loads(group_key)
    except (TypeError, ValueError):
        return None
    if not isinstance(pair, list) or len(pair) != 2:
        return None
    child, parent = pair
    if not isinstance(child, str) or not isinstance(parent, str):
        return None
    return child, parent


class MinimaxBackgroundSessionsCheck(Check):
    id = "minimax_background_sessions"
    title = "Background work is filed as a conversation"
    description = (
        "MiniMax Code background-task usage carries the provenance of the session that "
        "spawned it, but the rows sit on the background task's own session — so the work "
        "shows up in the Sessions view as a conversation of its own."
    )
    impact = (
        "The background work is missing from the session that owns it, and the Sessions view "
        "shows a conversation where there is only a background task."
    )
    recommended_action = (
        "Move the rows under the session their provenance names. The server cannot check that "
        "the named session exists, so a row may land on a session that holds nothing else. "
        "Rows collected before the provenance existed carry none and are not visible here — run "
        "scripts/link_minimax_background_sessions.py for those."
    )
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        # The detection reuses the repair's own query, so the check and the
        # fix can never disagree about what counts as misfiled.
        plan = plan_provenance_relinks(session)
        # Grouped by (child, parent): a fix moves each row to *its own* named
        # parent, so a group that named one parent while the rows disagreed
        # would preview the wrong target.
        by_pair: dict[tuple[str, str], int] = {}
        for relink in plan.relinks:
            key = (relink.session_id, relink.parent_session_id)
            by_pair[key] = by_pair.get(key, 0) + 1

        groups: list[FindingGroup] = []
        for (child_session_id, parent_session_id), count in sorted(by_pair.items()):
            groups.append(
                FindingGroup(
                    key=_group_key(child_session_id, parent_session_id),
                    label=f"{child_session_id} → {parent_session_id}",
                    count=count,
                    fixable=True,
                    samples=[
                        Finding(
                            label=child_session_id,
                            detail={
                                "session_id": child_session_id,
                                "parent_session_id": parent_session_id,
                                "usage_events": count,
                            },
                        )
                    ],
                    detail={
                        "session_id": child_session_id,
                        "parent_session_id": parent_session_id,
                        "usage_events": count,
                    },
                )
            )
        return CheckReport(
            check_id=self.id,
            severity=self.severity,
            total_count=sum(g.count for g in groups),
            groups=groups,
        )

    def plan(self, session: Session, group_key: str, params: dict[str, Any]) -> FixPlan:
        pair = _parse_group_key(group_key)
        if pair is None:
            return FixPlan(
                check_id=self.id,
                group_key=group_key,
                summary="This finding is stale — rescan to see the current groups.",
                counts={"usage_events_to_move": 0},
            )
        child_session_id, parent_session_id = pair
        plan = plan_provenance_relinks(
            session,
            child_session_id=child_session_id,
            parent_session_id=parent_session_id,
        )
        if plan.total == 0:
            summary = f"{child_session_id} is already filed under {parent_session_id}"
        else:
            summary = f"Move {plan.total} MiniMax Code background row(s) under {parent_session_id}"
        return FixPlan(
            check_id=self.id,
            group_key=group_key,
            summary=summary,
            counts={"usage_events_to_move": plan.total},
            samples=[Finding(label=sample, detail={}) for sample in plan.samples],
        )

    def apply(
        self, session: Session, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        # Re-plan at apply time: the rows may have been filed since the scan.
        pair = _parse_group_key(group_key)
        if pair is None:
            return (
                FixResult(
                    check_id=self.id,
                    group_key=group_key,
                    summary="This finding is stale — rescan to see the current groups.",
                    counts={"usage_events_moved": 0},
                ),
                [],
            )
        child_session_id, parent_session_id = pair
        plan = plan_provenance_relinks(
            session,
            child_session_id=child_session_id,
            parent_session_id=parent_session_id,
        )
        result = apply_provenance_relinks(session, plan)
        if result.relinked == 0:
            summary = f"{child_session_id} is already filed under {parent_session_id}"
        else:
            summary = (
                f"Moved {result.relinked} MiniMax Code background row(s) under {parent_session_id}"
            )
        return (
            FixResult(
                check_id=self.id,
                group_key=group_key,
                summary=summary,
                counts={"usage_events_moved": result.relinked},
            ),
            [],
        )
