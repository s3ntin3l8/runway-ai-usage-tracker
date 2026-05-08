"""Idempotent event ingestion.

Writes UsageEvent rows, updates rollups, computes cost_usd from
provider_pricing. Each event is flushed individually so IntegrityError
on a duplicate is caught and the batch continues without poisoning
prior good inserts.
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
        for push in pushes:
            ts = datetime.fromisoformat(push.ts.replace("Z", "+00:00"))
            if push.cost_usd is not None:
                # Provider supplied an authoritative cost (e.g. OpenCode logs it per message).
                # Use it directly rather than re-computing from the pricing table.
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
                model_id=push.model_id,
                session_id=push.session_id,
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
            try:
                self.session.add(ev)
                self.session.flush()
            except IntegrityError:
                self.session.rollback()
                result.events_duplicate += 1
                continue
            update_rollups_for_event(self.session, ev)
            result.events_inserted += 1
        self.session.commit()
        return result
