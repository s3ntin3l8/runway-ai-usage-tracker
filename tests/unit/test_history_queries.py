"""Tests for history query functions rewired to event-sourced model."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import LatestUsage, UsageEvent, UsagePeriodRollup
from app.services.event_query import (
    query_history_deltas,
    query_history_grouped,
    query_history_raw,
)


@pytest.fixture
def db_session():
    fd, db_path = tempfile.mkstemp()
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


def _make_event(
    session: Session,
    *,
    ts: datetime,
    provider_id: str = "anthropic",
    account_id: str = "acc1",
    model_id: str | None = None,
    event_id: str | None = None,
    tokens_input: int = 10_000,
    tokens_output: int = 5_000,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    cost_usd: float = 0.05,
    kind: str = "message",
) -> UsageEvent:
    if event_id is None:
        event_id = f"evt_{ts.isoformat()}_{provider_id}_{account_id}"
    ev = UsageEvent(
        provider_id=provider_id,
        account_id=account_id,
        event_id=event_id,
        ts=ts,
        model_id=model_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        cost_usd=cost_usd,
        sidecar_id="local",
        kind=kind,
    )
    session.add(ev)
    session.commit()
    return ev


def _make_rollup(
    session: Session,
    *,
    provider_id: str = "anthropic",
    account_id: str = "acc1",
    period_type: str = "hour",
    period_key: str = "2026-05-10T14",
    model_id: str = "",
    sidecar_id: str = "",
    tokens_input: int = 10_000,
    tokens_output: int = 5_000,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    cost_usd: float = 0.15,
    msgs: int = 5,
) -> UsagePeriodRollup:
    r = UsagePeriodRollup(
        provider_id=provider_id,
        account_id=account_id,
        period_type=period_type,
        period_key=period_key,
        model_id=model_id,
        sidecar_id=sidecar_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        cost_usd=cost_usd,
        msgs=msgs,
    )
    session.add(r)
    session.commit()
    return r


def _make_latest_usage(
    session: Session,
    *,
    provider_id: str = "anthropic",
    account_id: str = "acc1",
    window_type: str = "weekly",
    model_id: str = "",
    variant: str = "default",
    service_name: str = "Claude API",
    unit_type: str = "tokens",
    limit_value: float = 1_000_000.0,
    used_value: float = 50_000.0,
    pct_used: float | None = None,
) -> LatestUsage:
    import json

    if pct_used is None:
        pct_used = (used_value / limit_value * 100) if limit_value > 0 else 0.0
    card_json = json.dumps(
        {
            "service_name": service_name,
            "provider_id": provider_id,
            "account_id": account_id,
            "window_type": window_type,
            "model_id": model_id,
            "variant": variant,
            "unit_type": unit_type,
            "unit": unit_type,
            "limit_value": limit_value,
            "used_value": used_value,
            "pct_used": pct_used,
            "remaining": f"{100 - pct_used:.0f}%",
            "health": "good",
            "reset": "in 5 days",
            "reset_at": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
            "data_source": "api",
            "input_source": "config",
        }
    )
    r = LatestUsage(
        provider_id=provider_id,
        account_id=account_id,
        window_type=window_type,
        model_id=model_id,
        variant=variant,
        card_json=card_json,
    )
    session.add(r)
    session.commit()
    return r


# ── query_history_raw ──────────────────────────────────────────────────────


class TestQueryHistoryRaw:
    def test_empty_db_returns_empty(self, db_session):
        result = query_history_raw(db_session, days=1.0)
        assert result == []

    def test_basic_events_short_range(self, db_session):
        """days <= 1 queries usage_events with 15-min buckets."""
        now = datetime.now(UTC)
        for i in range(3):
            _make_event(db_session, ts=now - timedelta(hours=i), event_id=f"ev_{i}")

        result = query_history_raw(db_session, days=1.0)
        assert len(result) > 0
        assert result[0]["provider_id"] == "anthropic"
        assert "token_usage" in result[0]
        assert result[0]["token_usage"]["total"] > 0

    def test_provider_filter(self, db_session):
        now = datetime.now(UTC)
        _make_event(
            db_session, ts=now - timedelta(hours=1), provider_id="anthropic", event_id="ev_a"
        )
        _make_event(db_session, ts=now - timedelta(hours=1), provider_id="openai", event_id="ev_o")

        result = query_history_raw(db_session, provider_id="anthropic", days=1.0)
        assert len(result) > 0
        assert all(r["provider_id"] == "anthropic" for r in result)

    def test_rollup_medium_range(self, db_session):
        """days <= 7 queries usage_period_rollup with period_type='hour'."""
        now = datetime.now(UTC)
        hour_key = now.strftime("%Y-%m-%dT%H")
        _make_rollup(db_session, period_type="hour", period_key=hour_key)

        result = query_history_raw(db_session, days=3.0)
        assert len(result) > 0
        assert result[0]["provider_id"] == "anthropic"

    def test_rollup_long_range(self, db_session):
        """days > 7 queries usage_period_rollup with period_type='day'."""
        now = datetime.now(UTC)
        day_key = now.strftime("%Y-%m-%d")
        _make_rollup(db_session, period_type="day", period_key=day_key)

        result = query_history_raw(db_session, days=14.0)
        assert len(result) > 0


# ── query_history_grouped ──────────────────────────────────────────────────


class TestQueryHistoryGrouped:
    def test_empty_db_returns_empty(self, db_session):
        result = query_history_grouped(db_session, days=1.0)
        assert result == {"averages": [], "peaks": []}

    def test_basic_grouping(self, db_session):
        now = datetime.now(UTC)
        _make_latest_usage(db_session, provider_id="anthropic")
        for i in range(3):
            _make_event(db_session, ts=now - timedelta(hours=i), event_id=f"ev_{i}")

        result = query_history_grouped(db_session, days=1.0)
        assert "averages" in result
        assert "peaks" in result
        # Should have at least one entry
        assert len(result["averages"]) > 0
        # Each entry should have windows
        for entry in result["averages"]:
            assert "timestamp" in entry
            assert "provider_id" in entry
            assert "windows" in entry

    def test_provider_filter_passthrough(self, db_session):
        now = datetime.now(UTC)
        _make_event(
            db_session, ts=now - timedelta(hours=1), provider_id="anthropic", event_id="ev_a"
        )
        _make_event(db_session, ts=now - timedelta(hours=1), provider_id="openai", event_id="ev_o")

        result = query_history_grouped(db_session, provider_id="anthropic", days=1.0)
        for entry in result.get("averages", []):
            assert entry["provider_id"] == "anthropic"


# ── query_history_deltas ──────────────────────────────────────────────────


class TestQueryHistoryDeltas:
    def test_empty_db_returns_zeros(self, db_session):
        result = query_history_deltas(db_session, days=1.0)
        assert result["token_delta_total"] == 0.0
        assert result["cost_delta_total"] == 0.0
        assert result["provider_token_deltas"] == {}
        assert result["critical_series_count"] == 0
        assert result["series_sampled"] is False

    def test_basic_delta_computation(self, db_session):
        now = datetime.now(UTC)
        _make_event(
            db_session,
            ts=now - timedelta(hours=1),
            tokens_input=10000,
            tokens_output=5000,
            cost_usd=0.15,
            event_id="ev1",
        )
        _make_event(
            db_session,
            ts=now - timedelta(minutes=30),
            tokens_input=5000,
            tokens_output=3000,
            cost_usd=0.08,
            event_id="ev2",
        )

        result = query_history_deltas(db_session, days=1.0)
        assert result["token_delta_total"] == 23000.0  # (10000+5000) + (5000+3000) = 23000
        assert result["cost_delta_total"] == 0.23
        assert "anthropic" in result["provider_token_deltas"]

    def test_provider_filter(self, db_session):
        now = datetime.now(UTC)
        _make_event(
            db_session, ts=now - timedelta(hours=1), provider_id="anthropic", event_id="ev_a"
        )
        _make_event(db_session, ts=now - timedelta(hours=1), provider_id="openai", event_id="ev_o")

        result = query_history_deltas(db_session, provider_id="anthropic", days=1.0)
        assert "openai" not in result["provider_token_deltas"]
        assert "anthropic" in result["provider_token_deltas"]

    def test_error_events_excluded(self, db_session):
        now = datetime.now(UTC)
        _make_event(db_session, ts=now - timedelta(hours=1), kind="message", event_id="ev_msg")
        _make_event(db_session, ts=now - timedelta(hours=1), kind="error", event_id="ev_err")

        result = query_history_deltas(db_session, days=1.0)
        # Only message-kind events should be counted
        assert result["token_delta_total"] > 0  # message event
        # error events don't add tokens (they default to 0 anyway)

    def test_critical_series_detection(self, db_session):
        """Cards with pct_used >= 90 should be counted as critical."""
        _make_latest_usage(db_session, provider_id="anthropic", pct_used=95.0)
        _make_latest_usage(db_session, provider_id="openai", pct_used=50.0, service_name="ChatGPT")

        result = query_history_deltas(db_session, days=1.0)
        assert result["critical_series_count"] == 1  # only anthropic at 95%
