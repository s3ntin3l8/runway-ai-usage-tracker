"""Parity between the set-based rollup rebuild and the per-event replay.

``rebuild_rollups_for_pairs`` used to delete a pair's rollups and replay
every one of its message events through ``update_rollups_for_event`` — about
20 individual upsert statements per event. On a pair with tens of thousands
of events (the shape a real database develops once a provider goes through
an account merge or a legacy-id retag) that's minutes of round trips run
during ``init_db``, with the server unavailable the whole time. The set-based
rewrite must produce byte-identical rollup rows (aside from ``last_updated``,
which is a fresh timestamp either way) for every shape the replay handles.

Each test builds the same events in two independent in-memory databases —
one processed by looping ``update_rollups_for_event`` (the old behavior,
kept only as this test's oracle), the other by ``rebuild_rollups_for_pairs``
— and asserts the resulting rollup rows match.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.period_rollups import (
    rebuild_rollups_for_pairs,
    rebuild_rollups_for_providers,
    update_rollups_for_event,
)


def _new_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _event_kwargs(i: int, **overrides) -> dict:
    base: dict = {
        "provider_id": "minimax",
        "account_id": "s3ntin3l8@gmail.com",
        "sidecar_id": "dev-01",
        "event_id": f"msg_{i}",
        "ts": datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        "kind": "message",
        "model_id": "MiniMax-M3",
        "tokens_input": 100,
        "tokens_output": 20,
        "tokens_cache_read": 0,
        "tokens_cache_create": 0,
        "tokens_reasoning": 0,
        "cost_usd": 0.05,
        "cost_input": 0.03,
        "cost_output": 0.02,
        "cost_cache_read": 0.0,
        "cost_cache_create": 0.0,
    }
    base.update(overrides)
    return base


def _rollup_snapshot(session: Session, provider_id: str, account_id: str) -> set[tuple]:
    """Every rollup row for a pair, as a comparable tuple excluding id/last_updated."""
    rows = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == provider_id,
            UsagePeriodRollup.account_id == account_id,
        )
    ).all()
    return {
        (
            r.period_type,
            r.period_key,
            r.model_id,
            r.sidecar_id,
            r.msgs,
            r.tokens_input,
            r.tokens_output,
            r.tokens_cache_read,
            r.tokens_cache_create,
            r.tokens_reasoning,
            round(r.cost_usd, 9),
            round(r.cost_input, 9),
            round(r.cost_output, 9),
            round(r.cost_cache_read, 9),
            round(r.cost_cache_create, 9),
        )
        for r in rows
    }


def _assert_parity(events: list[dict], provider_id: str, account_id: str) -> None:
    replay_session = _new_session()
    for kwargs in events:
        ev = UsageEvent(**kwargs)
        replay_session.add(ev)
        replay_session.flush()
        if ev.kind == "message":
            update_rollups_for_event(replay_session, ev)
    replay_session.commit()
    expected = _rollup_snapshot(replay_session, provider_id, account_id)

    rebuild_session = _new_session()
    for kwargs in events:
        rebuild_session.add(UsageEvent(**kwargs))
    rebuild_session.commit()
    rebuild_rollups_for_pairs(rebuild_session, {(provider_id, account_id)})
    rebuild_session.commit()
    actual = _rollup_snapshot(rebuild_session, provider_id, account_id)

    assert actual == expected


def test_parity_basic_single_model_and_sidecar():
    events = [
        _event_kwargs(0),
        _event_kwargs(1, ts=datetime(2026, 9, 1, 12, 30, tzinfo=UTC)),
    ]
    _assert_parity(events, "minimax", "s3ntin3l8@gmail.com")


def test_parity_null_model_id():
    _assert_parity([_event_kwargs(0, model_id=None)], "minimax", "s3ntin3l8@gmail.com")


def test_parity_empty_model_id():
    _assert_parity([_event_kwargs(0, model_id="")], "minimax", "s3ntin3l8@gmail.com")


def test_parity_empty_sidecar_id():
    _assert_parity([_event_kwargs(0, sidecar_id="")], "minimax", "s3ntin3l8@gmail.com")


def test_parity_both_model_and_sidecar_empty():
    _assert_parity([_event_kwargs(0, model_id="", sidecar_id="")], "minimax", "s3ntin3l8@gmail.com")


def test_parity_multiple_models_and_sidecars():
    combos = [("MiniMax-M3", "dev-01"), ("MiniMax-M2", "dev-01"), ("MiniMax-M3", "mgmt"), ("", "")]
    events = [
        _event_kwargs(
            i,
            model_id=model,
            sidecar_id=sidecar,
            ts=datetime(2026, 9, 1, 12, i, tzinfo=UTC),
        )
        for i, (model, sidecar) in enumerate(combos)
    ]
    _assert_parity(events, "minimax", "s3ntin3l8@gmail.com")


def test_parity_across_hour_day_month_year_boundaries():
    timestamps = [
        datetime(2025, 12, 31, 23, 59, tzinfo=UTC),  # year/month/day/hour boundary
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 6, 30, 23, 0, tzinfo=UTC),
        datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 15, 13, 0, tzinfo=UTC),
        datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
    ]
    events = [_event_kwargs(i, ts=ts) for i, ts in enumerate(timestamps)]
    _assert_parity(events, "minimax", "s3ntin3l8@gmail.com")


def test_parity_null_cost_fields():
    events = [
        _event_kwargs(
            0,
            cost_usd=None,
            cost_input=None,
            cost_output=None,
            cost_cache_read=None,
            cost_cache_create=None,
        )
    ]
    _assert_parity(events, "minimax", "s3ntin3l8@gmail.com")


def test_error_kind_events_excluded():
    """Error events carry no tokens/cost and must not contribute rows."""
    events = [
        _event_kwargs(0, kind="message"),
        _event_kwargs(
            1, event_id="msg_err", kind="error", tokens_input=0, tokens_output=0, cost_usd=0.0
        ),
    ]
    _assert_parity(events, "minimax", "s3ntin3l8@gmail.com")

    session = _new_session()
    for kwargs in events:
        session.add(UsageEvent(**kwargs))
    session.commit()
    rebuild_rollups_for_pairs(session, {("minimax", "s3ntin3l8@gmail.com")})
    session.commit()
    lifetime = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "minimax",
            UsagePeriodRollup.account_id == "s3ntin3l8@gmail.com",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert lifetime is not None
    assert lifetime.msgs == 1  # not 2 — the error event contributes nothing


def test_parity_at_moderate_scale():
    """A few hundred events across models/sidecars/periods — the shape a
    real duplicate-event or legacy-provider-id cleanup rebuild sees."""
    base = datetime(2026, 8, 1, tzinfo=UTC)
    models = ["MiniMax-M3", "MiniMax-M2", ""]
    sidecars = ["dev-01", "mgmt", ""]
    events = [
        _event_kwargs(
            i,
            ts=base + timedelta(hours=i * 3),
            model_id=models[i % 3],
            sidecar_id=sidecars[i % 3],
            tokens_input=100 + i,
            tokens_output=10 + i,
            cost_usd=0.001 * i,
        )
        for i in range(300)
    ]
    _assert_parity(events, "minimax", "s3ntin3l8@gmail.com")


def test_rebuild_drops_stale_rows_no_longer_supported_by_events():
    """A grain that existed before (e.g. a model that's since been retagged
    away) must not survive the rebuild once no event supports it."""
    session = _new_session()
    ev = UsageEvent(**_event_kwargs(0, model_id="MiniMax-M3"))
    session.add(ev)
    session.commit()
    rebuild_rollups_for_pairs(session, {("minimax", "s3ntin3l8@gmail.com")})
    session.commit()
    before = _rollup_snapshot(session, "minimax", "s3ntin3l8@gmail.com")
    assert any(row[2] == "MiniMax-M3" for row in before)

    ev.model_id = "MiniMax-M2"
    session.add(ev)
    session.commit()
    rebuild_rollups_for_pairs(session, {("minimax", "s3ntin3l8@gmail.com")})
    session.commit()
    after = _rollup_snapshot(session, "minimax", "s3ntin3l8@gmail.com")
    assert not any(row[2] == "MiniMax-M3" for row in after)
    assert any(row[2] == "MiniMax-M2" for row in after)


def test_multiple_pairs_only_touch_their_own_rows():
    session = _new_session()
    session.add(UsageEvent(**_event_kwargs(0, account_id="alice@example.com", event_id="a")))
    session.add(UsageEvent(**_event_kwargs(0, account_id="bob@example.com", event_id="b")))
    session.commit()
    rebuild_rollups_for_pairs(
        session, {("minimax", "alice@example.com"), ("minimax", "bob@example.com")}
    )
    session.commit()
    alice = _rollup_snapshot(session, "minimax", "alice@example.com")
    bob = _rollup_snapshot(session, "minimax", "bob@example.com")
    assert alice and bob


# ── rebuild_rollups_for_providers: the coarser whole-provider rebuild ──────


def test_rebuild_rollups_for_providers_discovers_pairs_from_events():
    session = _new_session()
    session.add(UsageEvent(**_event_kwargs(0, account_id="alice@example.com", event_id="a")))
    session.add(UsageEvent(**_event_kwargs(0, account_id="bob@example.com", event_id="b")))
    session.commit()

    count = rebuild_rollups_for_providers(session, ["minimax"])
    session.commit()

    assert count == 2
    alice = _rollup_snapshot(session, "minimax", "alice@example.com")
    bob = _rollup_snapshot(session, "minimax", "bob@example.com")
    assert alice and bob


def test_rebuild_rollups_for_providers_none_means_every_provider():
    session = _new_session()
    session.add(UsageEvent(**_event_kwargs(0, provider_id="minimax", event_id="a")))
    session.add(UsageEvent(**_event_kwargs(0, provider_id="kimi_coding", event_id="b")))
    session.commit()

    count = rebuild_rollups_for_providers(session, None)
    session.commit()

    assert count == 2
    assert _rollup_snapshot(session, "minimax", "s3ntin3l8@gmail.com")
    assert _rollup_snapshot(session, "kimi_coding", "s3ntin3l8@gmail.com")


def test_rebuild_rollups_for_providers_scopes_to_the_given_providers_only():
    session = _new_session()
    session.add(UsageEvent(**_event_kwargs(0, provider_id="minimax", event_id="a")))
    session.add(UsageEvent(**_event_kwargs(0, provider_id="kimi_coding", event_id="b")))
    session.commit()

    rebuild_rollups_for_providers(session, ["minimax"])
    session.commit()

    assert not _rollup_snapshot(session, "kimi_coding", "s3ntin3l8@gmail.com")


def test_rebuild_rollups_for_providers_excludes_error_only_pairs():
    """A pair with only error-kind events (no message events) contributes
    nothing to rollups and shouldn't be discovered as a pair to rebuild."""
    session = _new_session()
    session.add(UsageEvent(**_event_kwargs(0, provider_id="minimax", event_id="a", kind="error")))
    session.commit()

    count = rebuild_rollups_for_providers(session, ["minimax"])

    assert count == 0
