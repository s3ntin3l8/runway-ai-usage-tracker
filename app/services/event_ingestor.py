"""Idempotent event ingestion.

Writes UsageEvent rows, updates rollups, computes cost_usd from
provider_pricing. The full batch commits as one transaction: either every
new event lands together or none of them do. Per-event savepoints isolate
the harmless IntegrityError raised when a sidecar replays a duplicate, so
duplicates don't poison the surrounding batch.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models.db import UsageEvent
from app.models.schemas import UsageEventPush
from app.services.cost_calculator import compute_event_cost
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
                ts = datetime.fromisoformat(push.ts.replace("Z", "+00:00"))

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

                if push.cost_usd is not None:
                    # Provider supplied an authoritative cost (e.g. OpenCode logs
                    # it per message). Use it directly rather than re-computing.
                    cost = push.cost_usd
                else:
                    cost = compute_event_cost(
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
                ev = UsageEvent(
                    provider_id=push.provider_id,
                    account_id=push.account_id,
                    sidecar_id=sidecar_id or "local",
                    event_id=push.event_id,
                    ts=ts,
                    kind=push.kind,
                    model_id=push.model_id,
                    session_id=push.session_id,
                    subagent_type=push.subagent_type,
                    tokens_input=push.tokens_input,
                    tokens_output=push.tokens_output,
                    tokens_cache_read=push.tokens_cache_read,
                    tokens_cache_create=push.tokens_cache_create,
                    tokens_reasoning=push.tokens_reasoning,
                    cost_usd=cost,
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
