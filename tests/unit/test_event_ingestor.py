from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import UsageEvent, UsagePeriodRollup
from app.models.schemas import UsageEventPush
from app.services.event_ingestor import EventIngestor
from app.services.pricing_seed import seed_pricing_table


def _seeded_session():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
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


def test_ingest_derives_project_and_persists_context():
    """cwd flows through; project is the server-derived basename; tools/latency persist."""
    s = _seeded_session()
    EventIngestor(s).ingest(
        [
            _make_push(
                cwd="/home/user/projects/runway/",
                git_branch="main",
                tool_names=["Read", "Bash"],
                latency_ms=1234,
            )
        ],
        sidecar_id="dev-01",
    )
    row = s.exec(select(UsageEvent)).first()
    assert row.cwd == "/home/user/projects/runway/"
    assert row.project == "runway"  # basename, trailing slash stripped
    assert row.git_branch == "main"
    assert row.tools_json == '["Read", "Bash"]'
    assert row.latency_ms == 1234


def test_ingest_without_cwd_leaves_project_null():
    s = _seeded_session()
    EventIngestor(s).ingest([_make_push()], sidecar_id="dev-01")
    row = s.exec(select(UsageEvent)).first()
    assert row.cwd is None
    assert row.project is None
    assert row.tools_json is None


def test_ingest_uses_provided_cost_when_set():
    """When push.cost_usd is not None, the server uses it directly."""
    s = _seeded_session()
    # Provide an explicit cost for an opencode event with no pricing row seeded
    push = _make_push(
        event_id="oc_001",
        provider_id="opencode",
        model_id="gpt-4o",
        cost_usd=0.0088,
    )
    res = EventIngestor(s).ingest([push], sidecar_id="dev-01")
    assert res.events_inserted == 1
    row = s.exec(select(UsageEvent).where(UsageEvent.event_id == "oc_001")).first()
    assert row is not None
    assert abs(row.cost_usd - 0.0088) < 1e-9


def test_ingest_computes_cost_when_not_set():
    """When push.cost_usd is None (default), cost is computed from the pricing table."""
    s = _seeded_session()
    # sonnet has a pricing row seeded; cost should be > 0
    push = _make_push(event_id="msg_compute", cost_usd=None)
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    row = s.exec(select(UsageEvent).where(UsageEvent.event_id == "msg_compute")).first()
    assert row is not None
    # input=100 @ $3/Mtok + output=200 @ $15/Mtok = $0.0003 + $0.003 = $0.0033
    assert row.cost_usd > 0


def test_ingest_stores_cost_components_and_rolls_them_up():
    """The per-component cost columns are written on the event and summed into
    the rollup, alongside the existing total."""
    s = _seeded_session()
    push = _make_push(
        event_id="msg_cache",
        tokens_cache_read=1_000_000,  # sonnet cache_read $0.30/MT
        tokens_cache_create=1_000_000,  # sonnet cache_create $3.75/MT
    )
    EventIngestor(s).ingest([push], sidecar_id="dev-01")

    ev = s.exec(select(UsageEvent).where(UsageEvent.event_id == "msg_cache")).first()
    assert ev.cost_cache_read == 0.30
    assert ev.cost_cache_create == 3.75
    assert ev.cost_input > 0 and ev.cost_output > 0

    day = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert day.cost_cache_read == ev.cost_cache_read
    assert day.cost_cache_create == ev.cost_cache_create


def test_ingest_stores_components_even_with_authoritative_total():
    """A provider-supplied cost_usd stays authoritative, but pricing-derived
    components are still stored (best-effort) when a pricing row exists."""
    s = _seeded_session()
    push = _make_push(event_id="auth_1", tokens_cache_read=1_000_000, cost_usd=99.0)
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    ev = s.exec(select(UsageEvent).where(UsageEvent.event_id == "auth_1")).first()
    assert ev.cost_usd == 99.0  # total untouched
    assert ev.cost_cache_read == 0.30  # component still computed from pricing


def test_ingest_persists_claude_code_dimensions_and_prices_cache_split():
    """effort/speed/service_tier/entrypoint/app_version/web-tool counts persist
    verbatim, and the 1h/5m cache split is priced through to cost_cache_create
    (sonnet: 1h @ $6.00/MT, 5m @ $3.75/MT)."""
    s = _seeded_session()
    push = _make_push(
        event_id="msg_dims",
        tokens_cache_create=1_000_000,
        tokens_cache_create_1h=700_000,
        tokens_cache_create_5m=300_000,
        effort="high",
        speed="standard",
        service_tier="standard",
        entrypoint="cli",
        app_version="2.1.217",
        web_search_requests=1,
        web_fetch_requests=2,
    )
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    ev = s.exec(select(UsageEvent).where(UsageEvent.event_id == "msg_dims")).first()

    assert ev.effort == "high"
    assert ev.speed == "standard"
    assert ev.service_tier == "standard"
    assert ev.entrypoint == "cli"
    assert ev.app_version == "2.1.217"
    assert ev.web_search_requests == 1
    assert ev.web_fetch_requests == 2
    assert ev.tokens_cache_create_1h == 700_000
    assert ev.tokens_cache_create_5m == 300_000
    assert ev.cost_cache_create == round(700_000 / 1_000_000 * 6.00 + 300_000 / 1_000_000 * 3.75, 6)


def test_ingest_then_replay_is_idempotent():
    """Replaying the exact same batch must not double-count anything.

    Property: after ingest(B) then ingest(B) again, the count of usage_events
    rows and the rollup sums are identical to a single ingest(B).
    """
    s = _seeded_session()
    batch = [_make_push(event_id=f"msg_{i}") for i in range(100)]

    # First ingest establishes the baseline.
    EventIngestor(s).ingest(batch, sidecar_id="dev-01")
    baseline_events = len(s.exec(select(UsageEvent)).all())
    baseline_day = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.period_key == "2026-05-08",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert baseline_events == 100
    assert baseline_day.msgs == 100

    # Replay the same batch.
    res = EventIngestor(s).ingest(batch, sidecar_id="dev-01")

    # All 100 should be deduplicated; no new event rows, no rollup drift.
    assert res.events_inserted == 0
    assert res.events_duplicate == 100
    assert len(s.exec(select(UsageEvent)).all()) == baseline_events
    day = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.period_key == "2026-05-08",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert day.msgs == baseline_day.msgs
    assert day.tokens_input == baseline_day.tokens_input
    assert day.tokens_output == baseline_day.tokens_output


def test_partial_batch_failure_rolls_back_all_rollups(monkeypatch):
    """A mid-batch crash must leave neither events nor rollups partially persisted.

    Event-sourcing invariant: rollups are a derived view of usage_events. If
    the ingest transaction fails, neither side may be visible — otherwise
    rollup recomputation from events disagrees with what's stored.
    """
    from app.services import event_ingestor as ingestor_mod

    s = _seeded_session()
    batch = [_make_push(event_id=f"msg_{i}") for i in range(100)]

    real_update = ingestor_mod.update_rollups_for_event
    calls = {"n": 0}

    def boom(session, ev):
        calls["n"] += 1
        if calls["n"] == 50:
            raise RuntimeError("simulated rollup failure on event 50")
        return real_update(session, ev)

    monkeypatch.setattr(ingestor_mod, "update_rollups_for_event", boom)

    try:
        EventIngestor(s).ingest(batch, sidecar_id="dev-01")
    except RuntimeError:
        pass  # expected — injected failure; we verify the rollback below

    # Re-open the session view of the database. The failed ingest must not
    # have left events 1..49 (or their rollups) visible.
    s.rollback()
    events = s.exec(select(UsageEvent)).all()
    rollups = s.exec(select(UsagePeriodRollup)).all()
    assert events == [], f"expected zero events, found {len(events)}"
    assert rollups == [], f"expected zero rollups, found {len(rollups)}"
