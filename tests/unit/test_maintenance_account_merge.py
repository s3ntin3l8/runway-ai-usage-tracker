"""Tests for app/services/maintenance/account_merge.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import LatestUsage, QuotaSnapshot
from app.services.maintenance.account_merge import (
    _chunked_retag_snapshots,
    delete_gauge_series,
    merge_gauge_series,
    plan_merge_gauge_series,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _card(session: Session, account_id: str, **overrides) -> LatestUsage:
    base = {
        "provider_id": "gemini",
        "account_id": account_id,
        "window_type": "daily",
        "variant": "",
        "model_id": "pro",
        "card_json": json.dumps(
            {"account_id": account_id, "account_label": account_id, "pct_used": 10.0, "msgs": 5}
        ),
    }
    base.update(overrides)
    row = LatestUsage(**base)
    session.add(row)
    session.commit()
    return row


def _snapshot(session: Session, account_id: str, ts: datetime, **overrides) -> QuotaSnapshot:
    base = {
        "provider_id": "gemini",
        "account_id": account_id,
        "window_type": "daily",
        "variant": "",
        "model_id": "pro",
        "ts": ts,
        "pct_used": 10.0,
    }
    base.update(overrides)
    row = QuotaSnapshot(**base)
    session.add(row)
    session.commit()
    return row


def test_merges_into_an_existing_target_card():
    session = _session()
    _card(session, "default", card_json=json.dumps({"account_id": "default", "pct_used": 20.0}))
    _card(
        session,
        "alice@example.com",
        card_json=json.dumps({"account_id": "alice@example.com", "msgs": 3}),
    )

    result = merge_gauge_series(
        session, provider_id="gemini", source="default", target="alice@example.com"
    )

    assert result.merged == 1
    assert result.retagged == 0
    rows = list(session.exec(select(LatestUsage)))
    assert len(rows) == 1
    assert rows[0].account_id == "alice@example.com"
    merged_card = json.loads(rows[0].card_json)
    assert merged_card["account_id"] == "alice@example.com"  # target's identity wins
    assert merged_card["pct_used"] == 20.0  # source's quota value carried over
    assert merged_card["msgs"] == 3  # target's own enrichment preserved


def test_retags_when_no_target_card_exists_for_the_grain():
    session = _session()
    _card(session, "default")

    result = merge_gauge_series(
        session, provider_id="gemini", source="default", target="alice@example.com"
    )

    assert result.retagged == 1
    assert result.merged == 0
    row = session.exec(select(LatestUsage)).one()
    assert row.account_id == "alice@example.com"
    card = json.loads(row.card_json)
    assert card["account_id"] == "alice@example.com"
    assert card["account_label"] == "alice@example.com"


def test_snapshots_retag_and_drop_exact_collisions():
    session = _session()
    ts_unique = datetime(2026, 9, 1, tzinfo=UTC)
    ts_collide = datetime(2026, 9, 2, tzinfo=UTC)
    _snapshot(session, "default", ts_unique)
    _snapshot(session, "default", ts_collide)
    _snapshot(session, "alice@example.com", ts_collide)  # pre-existing collision

    result = merge_gauge_series(
        session, provider_id="gemini", source="default", target="alice@example.com"
    )

    assert result.snapshots_retagged == 1
    assert result.snapshots_collided == 1
    remaining = list(session.exec(select(QuotaSnapshot)))
    assert len(remaining) == 2  # the retagged one + the pre-existing target row
    assert all(r.account_id == "alice@example.com" for r in remaining)
    assert {r.ts.replace(tzinfo=UTC) for r in remaining} == {ts_unique, ts_collide}


def test_plan_matches_apply_counts():
    session = _session()
    _card(session, "default")
    _card(session, "default", model_id="flash")
    _card(session, "alice@example.com", model_id="flash")  # collides with the second card
    ts = datetime(2026, 9, 1, tzinfo=UTC)
    _snapshot(session, "default", ts)

    plan = plan_merge_gauge_series(
        session, provider_id="gemini", source="default", target="alice@example.com"
    )
    assert plan.merged == 1
    assert plan.retagged == 1
    assert plan.snapshots_retagged == 1
    assert plan.snapshots_collided == 0

    result = merge_gauge_series(
        session, provider_id="gemini", source="default", target="alice@example.com"
    )
    assert (
        result.merged,
        result.retagged,
        result.snapshots_retagged,
        result.snapshots_collided,
    ) == (
        plan.merged,
        plan.retagged,
        plan.snapshots_retagged,
        plan.snapshots_collided,
    )


def test_plan_is_read_only():
    session = _session()
    _card(session, "default")

    plan_merge_gauge_series(
        session, provider_id="gemini", source="default", target="alice@example.com"
    )

    row = session.exec(select(LatestUsage)).one()
    assert row.account_id == "default"


def test_delete_gauge_series_removes_both_tables():
    session = _session()
    _card(session, "default")
    _snapshot(session, "default", datetime(2026, 9, 1, tzinfo=UTC))
    _snapshot(session, "default", datetime(2026, 9, 2, tzinfo=UTC))
    _card(session, "alice@example.com")  # untouched — different account

    result = delete_gauge_series(session, provider_id="gemini", account_id="default")

    assert result.latest_usage_deleted == 1
    assert result.snapshots_deleted == 2
    remaining = list(session.exec(select(LatestUsage)))
    assert len(remaining) == 1
    assert remaining[0].account_id == "alice@example.com"
    assert list(session.exec(select(QuotaSnapshot))) == []


def test_delete_gauge_series_no_op_when_nothing_matches():
    session = _session()
    result = delete_gauge_series(session, provider_id="gemini", account_id="default")
    assert result.latest_usage_deleted == 0
    assert result.snapshots_deleted == 0


def test_chunked_retag_snapshots_spans_multiple_batches_with_collisions():
    """A batch containing both a movable row and a colliding row must
    resolve each correctly and never loop forever re-matching a row an
    `OR IGNORE` batch failed to move (the same class of bug the id-cursor
    design in _chunked_sql.py guards against)."""
    session = _session()
    for i in range(9):
        _snapshot(session, "default", datetime(2026, 9, 1 + i, tzinfo=UTC))
    # A collision for the 5th source row (index 4, 0-based ts day 5).
    _snapshot(session, "alice@example.com", datetime(2026, 9, 5, tzinfo=UTC))

    retagged, collided = _chunked_retag_snapshots(
        session, "gemini", "default", "alice@example.com", batch_size=3
    )

    assert retagged == 8
    assert collided == 1
    remaining = list(session.exec(select(QuotaSnapshot)))
    assert (
        len(remaining) == 9
    )  # 8 retagged + the pre-existing target row; the collision was dropped
    assert all(r.account_id == "alice@example.com" for r in remaining)
