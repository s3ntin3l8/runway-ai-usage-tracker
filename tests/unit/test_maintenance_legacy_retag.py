"""Tests for app/services/maintenance/legacy_retag.py.

The opencode-xai/xai scenario here mirrors production data exactly: every
opencode-xai event shares its event_id with an xai event from the same
message (the OpenCode sidecar's canonical fold landing after the fact).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import LatestUsage, QuotaSnapshot, UsageEvent, UsagePeriodRollup
from app.services.maintenance.legacy_retag import (
    apply_legacy_retag,
    pick_winner,
    plan_legacy_retag,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _event(session: Session, **overrides) -> UsageEvent:
    base = {
        "provider_id": "opencode-xai",
        "account_id": "default",
        "sidecar_id": "dev-01",
        "event_id": f"msg_{overrides.get('event_id', 'x')}",
        "ts": datetime(2026, 9, 26, tzinfo=UTC),
        "kind": "message",
        "model_id": "grok-4.7",
        "tokens_input": 100,
        "tokens_output": 20,
        "cost_usd": 0.05,
    }
    base.update(overrides)
    ev = UsageEvent(**base)
    session.add(ev)
    session.commit()
    return ev


# ── pick_winner ─────────────────────────────────────────────────────────


def _ev(**overrides) -> UsageEvent:
    base = {
        "id": 1,
        "provider_id": "opencode-xai",
        "account_id": "default",
        "event_id": "e",
        "ts": datetime(2026, 1, 1, tzinfo=UTC),
        "kind": "message",
        "tokens_input": 0,
        "tokens_output": 0,
    }
    base.update(overrides)
    return UsageEvent(**base)


def test_pick_winner_message_beats_error():
    msg = _ev(id=1, kind="message", tokens_input=0)
    err = _ev(id=2, kind="error", tokens_input=999)
    winner, loser = pick_winner(msg, err)
    assert winner is msg
    assert loser is err
    winner, loser = pick_winner(err, msg)
    assert winner is msg
    assert loser is err


def test_pick_winner_more_tokens_wins():
    smaller = _ev(id=1, tokens_input=10)
    larger = _ev(id=2, tokens_input=1000)
    winner, loser = pick_winner(smaller, larger)
    assert winner is larger
    assert loser is smaller


def test_pick_winner_lower_id_breaks_a_true_tie():
    first = _ev(id=1, tokens_input=10)
    second = _ev(id=2, tokens_input=10)
    winner, loser = pick_winner(first, second)
    assert winner is first
    assert loser is second


# ── plan/apply_legacy_retag ─────────────────────────────────────────────


def test_plan_rejects_an_unknown_legacy_provider_id():
    session = _session()
    with pytest.raises(ValueError, match="not a known legacy provider"):
        plan_legacy_retag(session, "not-a-real-provider")


def test_plan_reports_the_canonical_target_and_counts():
    session = _session()
    _event(session, event_id="1")
    _event(session, event_id="2", provider_id="xai")  # collides with event_id 1? no, distinct id

    plan = plan_legacy_retag(session, "opencode-xai")

    assert plan.canonical_provider_id == "xai"
    assert plan.total == 1  # only the opencode-xai row
    assert plan.collisions == 0


def test_plan_is_read_only():
    session = _session()
    _event(session, event_id="1")

    plan_legacy_retag(session, "opencode-xai")

    ev = session.exec(select(UsageEvent)).one()
    assert ev.provider_id == "opencode-xai"


def test_apply_retags_a_lone_legacy_event_with_no_collision():
    session = _session()
    _event(session, event_id="1")

    result = apply_legacy_retag(session, "opencode-xai")

    assert result.retagged == 1
    assert result.collisions_resolved == 0
    ev = session.exec(select(UsageEvent)).one()
    assert ev.provider_id == "xai"


def test_apply_resolves_a_real_collision_keeping_the_richer_row():
    """The exact production shape: the same message under both
    opencode-xai and xai, xai's copy having more tokens (arrived later,
    fuller data)."""
    session = _session()
    _event(session, event_id="1", tokens_input=100, tokens_output=20)
    _event(session, event_id="1", provider_id="xai", tokens_input=132297, tokens_output=1405)

    result = apply_legacy_retag(session, "opencode-xai")

    assert result.collisions_resolved == 1
    assert result.retagged == 0  # the legacy row was the loser, nothing left to retag
    rows = list(session.exec(select(UsageEvent)))
    assert len(rows) == 1
    assert rows[0].provider_id == "xai"
    assert rows[0].tokens_input == 132297  # the richer (xai) copy survived


def test_apply_backfills_cost_reported_usd_before_retagging():
    """A legacy id's cost_usd is the source-reported subscription amount;
    once retagged, the provider-prefix check that used to recognize it
    (see event_cost.py's docstring) no longer applies — cost_reported_usd
    must be backfilled so the amount survives."""
    session = _session()
    _event(session, event_id="1", cost_usd=0.42, cost_reported_usd=None)

    apply_legacy_retag(session, "opencode-xai")

    ev = session.exec(select(UsageEvent)).one()
    assert ev.provider_id == "xai"
    assert ev.cost_reported_usd == 0.42


def test_apply_does_not_overwrite_an_existing_cost_reported_usd():
    session = _session()
    _event(session, event_id="1", cost_usd=0.42, cost_reported_usd=0.99)

    apply_legacy_retag(session, "opencode-xai")

    ev = session.exec(select(UsageEvent)).one()
    assert ev.cost_reported_usd == 0.99


def test_apply_drops_the_legacy_providers_gauge_series():
    session = _session()
    _event(session, event_id="1")
    session.add(
        LatestUsage(
            provider_id="opencode-xai",
            account_id="default",
            window_type="session",
            variant="",
            model_id="",
            card_json="{}",
        )
    )
    session.add(
        QuotaSnapshot(
            provider_id="opencode-xai",
            account_id="default",
            window_type="session",
            variant="",
            model_id="",
            ts=datetime(2026, 9, 26, tzinfo=UTC),
            pct_used=1.0,
        )
    )
    session.commit()

    result = apply_legacy_retag(session, "opencode-xai")

    assert result.latest_usage_dropped == 1
    assert result.quota_snapshots_dropped == 1
    assert list(session.exec(select(LatestUsage))) == []
    assert list(session.exec(select(QuotaSnapshot))) == []


def test_apply_rebuilds_rollups_for_the_canonical_provider():
    session = _session()
    _event(session, event_id="1", account_id="s3ntin3l8@gmail.com")

    apply_legacy_retag(session, "opencode-xai")

    lifetime = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.provider_id == "xai",
            UsagePeriodRollup.account_id == "s3ntin3l8@gmail.com",
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert lifetime is not None
    assert lifetime.msgs == 1
    # Nothing left under the legacy provider_id.
    stale = session.exec(
        select(UsagePeriodRollup).where(UsagePeriodRollup.provider_id == "opencode-xai")
    ).first()
    assert stale is None


def test_apply_handles_multiple_events_with_mixed_collisions():
    session = _session()
    _event(session, event_id="1")  # lone legacy event
    _event(session, event_id="2")
    _event(session, event_id="2", provider_id="xai", tokens_input=99999)  # collision, xai wins

    result = apply_legacy_retag(session, "opencode-xai")

    assert result.collisions_resolved == 1
    assert result.retagged == 1  # only event_id=1 survives to be retagged
    rows = {r.event_id: r for r in session.exec(select(UsageEvent))}
    assert set(rows) == {"1", "2"}
    assert all(r.provider_id == "xai" for r in rows.values())
    assert rows["2"].tokens_input == 99999
