"""Idempotent event ingestion.

Writes UsageEvent rows, updates rollups, computes cost_usd from
provider_pricing. The full batch commits as one transaction: either every
new event lands together or none of them do. Per-event savepoints isolate
the harmless IntegrityError raised when a sidecar replays a duplicate, so
duplicates don't poison the surrounding batch.
"""

import json
import os
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.date_utils import parse_iso8601_utc
from app.models.db import UsageEvent
from app.models.schemas import UsageEventPush
from app.services.cost_calculator import compute_event_cost_breakdown
from app.services.period_rollups import update_rollups_for_event


@dataclass
class IngestResult:
    events_received: int = 0
    events_inserted: int = 0
    events_duplicate: int = 0
    windows_closed: int = 0


class EventIngestor:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ingest(
        self,
        pushes: list[UsageEventPush],
        *,
        sidecar_id: str | None = None,
    ) -> IngestResult:
        result = IngestResult(events_received=len(pushes))
        try:
            for push in pushes:
                ts = parse_iso8601_utc(push.ts)

                if push.kind == "error":
                    ev = UsageEvent(
                        provider_id=push.provider_id,
                        account_id=push.account_id,
                        sidecar_id=sidecar_id or "local",
                        event_id=push.event_id,
                        ts=ts,
                        kind="error",
                        # store the error tag in stop_reason
                        stop_reason=push.error_reason,
                        raw_json=push.raw_json,
                    )
                    if self._try_insert_event(ev):
                        result.events_inserted += 1
                    else:
                        result.events_duplicate += 1
                    continue  # error events don't roll up

                # Always derive the per-component breakdown from pricing — it
                # feeds cost-composition views (e.g. exclude-cache).
                breakdown = compute_event_cost_breakdown(
                    self.session,
                    provider_id=push.provider_id,
                    model_id=push.model_id,
                    ts=ts,
                    tokens_input=push.tokens_input,
                    tokens_output=push.tokens_output,
                    tokens_cache_read=push.tokens_cache_read,
                    tokens_cache_create=push.tokens_cache_create,
                    tokens_reasoning=push.tokens_reasoning,
                )
                # Provider-supplied cost (e.g. OpenCode logs it per message) is
                # authoritative for the total; otherwise use the computed sum.
                # The components stay pricing-derived (best-effort) either way.
                cost = push.cost_usd if push.cost_usd is not None else breakdown.total
                ev = UsageEvent(
                    provider_id=push.provider_id,
                    account_id=push.account_id,
                    sidecar_id=sidecar_id or "local",
                    event_id=push.event_id,
                    ts=ts,
                    kind=push.kind,
                    model_id=push.model_id,
                    session_id=push.session_id,
                    cwd=push.cwd,
                    # Derive the project label server-side (single source of truth)
                    # so all providers normalise the same way regardless of sidecar.
                    project=(os.path.basename(push.cwd.rstrip("/")) if push.cwd else None),
                    git_branch=push.git_branch,
                    tools_json=(json.dumps(push.tool_names) if push.tool_names else None),
                    subagent_type=push.subagent_type,
                    tokens_input=push.tokens_input,
                    tokens_output=push.tokens_output,
                    tokens_cache_read=push.tokens_cache_read,
                    tokens_cache_create=push.tokens_cache_create,
                    tokens_reasoning=push.tokens_reasoning,
                    cost_usd=cost,
                    cost_input=breakdown.input,
                    cost_output=breakdown.output,
                    cost_cache_read=breakdown.cache_read,
                    cost_cache_create=breakdown.cache_create,
                    stop_reason=push.stop_reason,
                    tool_calls=push.tool_calls,
                    latency_ms=push.latency_ms,
                    raw_json=push.raw_json,
                )
                if not self._try_insert_event(ev):
                    result.events_duplicate += 1
                    continue
                # Rollups share the same outer transaction; if anything past
                # this point raises, the whole batch rolls back.
                update_rollups_for_event(self.session, ev)
                result.events_inserted += 1
        except Exception:
            self.session.rollback()
            raise

        self.session.commit()
        return result

    def _try_insert_event(self, ev: UsageEvent) -> bool:
        """Insert one event inside its own savepoint.

        Returns True on a new row, False when the (provider, account,
        event_id) unique constraint rejected the row as a duplicate. The
        savepoint scope keeps a duplicate from invalidating prior events
        already staged in the outer transaction.
        """
        sp = self.session.begin_nested()
        try:
            self.session.add(ev)
            self.session.flush()
        except IntegrityError:
            sp.rollback()
            return False
        sp.commit()
        return True
