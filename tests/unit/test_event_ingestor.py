from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup
from app.models.schemas import UsageEventPush
from app.services.event_ingestor import EventIngestor
from app.services.pricing_seed import seed_pricing_table


def _seeded_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    s = Session(engine)
    seed_pricing_table(s)
    return s


def _make_push(event_id="msg_1", ts="2026-05-08T14:23:11+00:00", **kw):
    base = {
        "provider_id": "anthropic",
        "account_id": "user@x.com",
        "event_id": event_id,
        "ts": ts,
        "model_id": "sonnet",
        "tokens_input": 100,
        "tokens_output": 200,
    }
    base.update(kw)
    return UsageEventPush(**base)


def test_ingest_single_event_writes_event_row():
    s = _seeded_session()
    res = EventIngestor(s).ingest([_make_push()], sidecar_id="dev-01")
    assert res.events_inserted == 1
    assert res.events_duplicate == 0
    rows = s.exec(select(UsageEvent)).all()
    assert len(rows) == 1
    assert rows[0].sidecar_id == "dev-01"
    assert rows[0].cost_usd > 0


def test_ingest_duplicate_event_is_noop():
    s = _seeded_session()
    push = _make_push()
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    res2 = EventIngestor(s).ingest([push], sidecar_id="dev-01")
    assert res2.events_inserted == 0
    assert res2.events_duplicate == 1
    assert len(s.exec(select(UsageEvent)).all()) == 1


def test_ingest_updates_rollups():
    s = _seeded_session()
    EventIngestor(s).ingest(
        [_make_push("msg_1"), _make_push("msg_2")],
        sidecar_id="dev-01",
    )
    day_row = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.period_key == "2026-05-08",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert day_row.msgs == 2
    assert day_row.tokens_input == 200
