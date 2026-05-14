"""Integration tests for the four new event-query endpoints:
GET /api/v1/usage/events
GET /api/v1/usage/window-history
GET /api/v1/usage/heatmap
GET /api/v1/usage/sessions
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import UsageEvent, UsageWindow
from app.services.pricing_seed import seed_pricing_table

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        seed_pricing_table(s)
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


def _client():
    return TestClient(app)


def _event(
    *,
    provider_id: str = "anthropic",
    account_id: str = "user@example.com",
    sidecar_id: str = "dev-01",
    event_id: str = "msg_001",
    ts: datetime | None = None,
    model_id: str | None = "sonnet",
    session_id: str | None = "sess-abc",
    tokens_input: int = 100,
    tokens_output: int = 50,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    cost_usd: float = 0.0,
    stop_reason: str | None = "end_turn",
    tool_calls: int = 0,
    latency_ms: int | None = None,
) -> UsageEvent:
    if ts is None:
        ts = datetime.now(UTC)
    return UsageEvent(
        provider_id=provider_id,
        account_id=account_id,
        sidecar_id=sidecar_id,
        event_id=event_id,
        ts=ts,
        model_id=model_id,
        session_id=session_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        cost_usd=cost_usd,
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        latency_ms=latency_ms,
    )


def _window(
    *,
    provider_id: str = "anthropic",
    account_id: str = "user@example.com",
    window_type: str = "weekly",
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    model_id: str = "",
    sidecar_id: str = "",
    msgs: int = 100,
    tokens_input: int = 500_000,
    tokens_output: int = 50_000,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    cost_usd: float = 10.0,
    limit_value: float | None = 100.0,
    pct_used: float | None = 50.0,
) -> UsageWindow:
    if window_start is None:
        window_start = datetime(2026, 4, 28, 18, 0, 0, tzinfo=UTC)
    if window_end is None:
        window_end = datetime(2026, 5, 5, 18, 0, 0, tzinfo=UTC)
    return UsageWindow(
        provider_id=provider_id,
        account_id=account_id,
        window_type=window_type,
        window_start=window_start,
        window_end=window_end,
        model_id=model_id,
        sidecar_id=sidecar_id,
        msgs=msgs,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        cost_usd=cost_usd,
        limit_value=limit_value,
        pct_used=pct_used,
    )


# ===========================================================================
# Task 7.1 — /events
# ===========================================================================


class TestEventsEndpoint:
    """Tests for GET /api/v1/usage/events."""

    def test_events_returns_rows_in_desc_order(self, session):
        now = datetime.now(UTC)
        session.add(_event(event_id="msg_a", ts=now - timedelta(minutes=2)))
        session.add(_event(event_id="msg_b", ts=now - timedelta(minutes=1)))
        session.add(_event(event_id="msg_c", ts=now))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data["events"]) == 3
        # desc = newest first
        tss = [e["ts"] for e in data["events"]]
        assert tss == sorted(tss, reverse=True)

    def test_events_returns_rows_in_asc_order(self, session):
        now = datetime.now(UTC)
        session.add(_event(event_id="msg_a", ts=now - timedelta(minutes=2)))
        session.add(_event(event_id="msg_b", ts=now - timedelta(minutes=1)))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "order": "asc",
            },
        )
        assert r.status_code == 200
        tss = [e["ts"] for e in r.json()["events"]]
        assert tss == sorted(tss)

    def test_events_filters_by_provider_account(self, session):
        session.add(_event(event_id="a1", provider_id="anthropic", account_id="u@x"))
        session.add(_event(event_id="a2", provider_id="chatgpt", account_id="u@x"))
        session.add(_event(event_id="a3", provider_id="anthropic", account_id="other@x"))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={"provider_id": "anthropic", "account_id": "u@x"},
        )
        assert r.status_code == 200
        events = r.json()["events"]
        assert len(events) == 1
        assert events[0]["event_id"] == "a1"

    def test_events_respects_limit(self, session):
        now = datetime.now(UTC)
        for i in range(10):
            session.add(_event(event_id=f"msg_{i:03d}", ts=now - timedelta(seconds=i)))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={"provider_id": "anthropic", "account_id": "user@example.com", "limit": 3},
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["events"]) == 3
        assert data["limit"] == 3

    def test_events_filters_by_model_and_sidecar(self, session):
        now = datetime.now(UTC)
        session.add(_event(event_id="s1", model_id="sonnet", sidecar_id="dev-01", ts=now))
        session.add(
            _event(
                event_id="h1", model_id="haiku", sidecar_id="dev-01", ts=now - timedelta(seconds=1)
            )
        )
        session.add(
            _event(
                event_id="s2", model_id="sonnet", sidecar_id="dev-02", ts=now - timedelta(seconds=2)
            )
        )
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "model_id": "sonnet",
                "sidecar_id": "dev-01",
            },
        )
        assert r.status_code == 200
        events = r.json()["events"]
        assert len(events) == 1
        assert events[0]["event_id"] == "s1"

    def test_events_since_until(self, session):
        now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
        session.add(_event(event_id="before", ts=now - timedelta(hours=2)))
        session.add(_event(event_id="in_window", ts=now))
        session.add(_event(event_id="after", ts=now + timedelta(hours=2)))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "since": "2026-05-08T11:00:00Z",
                "until": "2026-05-08T13:00:00Z",
            },
        )
        assert r.status_code == 200
        events = r.json()["events"]
        assert len(events) == 1
        assert events[0]["event_id"] == "in_window"

    def test_events_excludes_raw_json_by_default(self, session):
        session.add(_event(event_id="r1"))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200
        # raw_json should not be present by default
        assert "raw_json" not in r.json()["events"][0]

    def test_events_total_reflects_count(self, session):
        now = datetime.now(UTC)
        for i in range(5):
            session.add(_event(event_id=f"m{i}", ts=now - timedelta(seconds=i)))
        session.commit()

        r = _client().get(
            "/api/v1/usage/events",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 5

    def test_events_empty_when_no_matching_data(self, session):
        r = _client().get(
            "/api/v1/usage/events",
            params={"provider_id": "anthropic", "account_id": "nobody@nowhere"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["events"] == []
        assert data["total"] == 0


# ===========================================================================
# Task 7.2 — /window-history
# ===========================================================================


class TestWindowHistoryEndpoint:
    """Tests for GET /api/v1/usage/window-history."""

    def test_window_history_returns_recent_windows(self, session):
        # Add two closed weekly windows
        w1_start = datetime(2026, 4, 21, 18, 0, 0, tzinfo=UTC)
        w1_end = datetime(2026, 4, 28, 18, 0, 0, tzinfo=UTC)
        w2_start = datetime(2026, 4, 28, 18, 0, 0, tzinfo=UTC)
        w2_end = datetime(2026, 5, 5, 18, 0, 0, tzinfo=UTC)

        # totals row for window 1
        session.add(
            _window(window_start=w1_start, window_end=w1_end, model_id="", sidecar_id="", msgs=50)
        )
        # totals row for window 2
        session.add(
            _window(window_start=w2_start, window_end=w2_end, model_id="", sidecar_id="", msgs=100)
        )
        session.commit()

        r = _client().get(
            "/api/v1/usage/window-history",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "window_type": "weekly",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "windows" in data
        assert len(data["windows"]) == 2
        # Most recent first
        assert data["windows"][0]["totals"]["msgs"] == 100

    def test_window_history_groups_per_model_and_per_sidecar(self, session):
        w_start = datetime(2026, 4, 28, 18, 0, 0, tzinfo=UTC)
        w_end = datetime(2026, 5, 5, 18, 0, 0, tzinfo=UTC)

        # all-up totals
        session.add(
            _window(
                window_start=w_start,
                window_end=w_end,
                model_id="",
                sidecar_id="",
                msgs=100,
                cost_usd=20.0,
                limit_value=100.0,
                pct_used=50.0,
            )
        )
        # per-model
        session.add(
            _window(
                window_start=w_start,
                window_end=w_end,
                model_id="sonnet",
                sidecar_id="",
                msgs=60,
                cost_usd=12.0,
                limit_value=None,
                pct_used=None,
            )
        )
        session.add(
            _window(
                window_start=w_start,
                window_end=w_end,
                model_id="haiku",
                sidecar_id="",
                msgs=40,
                cost_usd=8.0,
                limit_value=None,
                pct_used=None,
            )
        )
        # per-sidecar
        session.add(
            _window(
                window_start=w_start,
                window_end=w_end,
                model_id="",
                sidecar_id="dev-01",
                msgs=100,
                cost_usd=20.0,
                limit_value=None,
                pct_used=None,
            )
        )
        # cross (model + sidecar) — should NOT appear in by_model or by_sidecar
        session.add(
            _window(
                window_start=w_start,
                window_end=w_end,
                model_id="sonnet",
                sidecar_id="dev-01",
                msgs=60,
                cost_usd=12.0,
                limit_value=None,
                pct_used=None,
            )
        )
        session.commit()

        r = _client().get(
            "/api/v1/usage/window-history",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "window_type": "weekly",
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data["windows"]) == 1
        win = data["windows"][0]

        assert win["totals"]["msgs"] == 100
        assert win["limit_value"] == 100.0
        assert win["pct_used"] == 50.0

        model_ids = {m["model_id"] for m in win["by_model"]}
        assert model_ids == {"sonnet", "haiku"}

        sidecar_ids = {s["sidecar_id"] for s in win["by_sidecar"]}
        assert sidecar_ids == {"dev-01"}

    def test_window_history_respects_limit(self, session):
        for i in range(10):
            w_start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(weeks=i)
            w_end = w_start + timedelta(weeks=1)
            session.add(_window(window_start=w_start, window_end=w_end))
        session.commit()

        r = _client().get(
            "/api/v1/usage/window-history",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "window_type": "weekly",
                "limit": 3,
            },
        )
        assert r.status_code == 200
        # 3 windows, each with 1 row → 3 windows
        assert len(r.json()["windows"]) == 3

    def test_window_history_unknown_window_type_returns_empty(self, session):
        session.add(_window(window_type="weekly"))
        session.commit()

        r = _client().get(
            "/api/v1/usage/window-history",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "window_type": "does_not_exist",
            },
        )
        assert r.status_code == 200
        assert r.json()["windows"] == []


# ===========================================================================
# Task 7.3 — /heatmap
# ===========================================================================


class TestHeatmapEndpoint:
    """Tests for GET /api/v1/usage/heatmap."""

    def test_heatmap_returns_168_cells(self, session):
        r = _client().get(
            "/api/v1/usage/heatmap",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "cells" in data
        assert len(data["cells"]) == 168  # 7 * 24

    def test_heatmap_groups_events_by_dow_hour(self, session):
        # Insert event at a known dow/hour
        # 2026-05-08 is a Friday = dow 5 in strftime('%w') (0=Sun, 5=Fri)
        ts = datetime(2026, 5, 8, 14, 30, 0, tzinfo=UTC)  # Friday 14:00 UTC
        session.add(_event(event_id="e1", ts=ts, tokens_input=1000, tokens_output=500))
        session.commit()

        r = _client().get(
            "/api/v1/usage/heatmap",
            params={"provider_id": "anthropic", "account_id": "user@example.com", "days": 90},
        )
        assert r.status_code == 200
        cells = {(c["dow"], c["hour"]): c["tokens"] for c in r.json()["cells"]}
        # dow=5 (Friday), hour=14
        assert cells[(5, 14)] == 1500  # 1000+500 (cache_read=0, cache_create=0 not counted)

    def test_heatmap_excludes_old_events(self, session):
        now = datetime.now(UTC)
        # Insert event that is too old (more than 14 days ago)
        old_ts = now - timedelta(days=20)
        session.add(_event(event_id="old", ts=old_ts, tokens_input=9999, tokens_output=9999))
        # Insert recent event
        recent_ts = now - timedelta(days=1)
        session.add(_event(event_id="recent", ts=recent_ts, tokens_input=100, tokens_output=50))
        session.commit()

        r = _client().get(
            "/api/v1/usage/heatmap",
            params={"provider_id": "anthropic", "account_id": "user@example.com", "days": 14},
        )
        assert r.status_code == 200
        cells = r.json()["cells"]
        total_tokens = sum(c["tokens"] for c in cells)
        # Only the recent event should be included
        assert total_tokens == 150
        assert 9999 not in [c["tokens"] for c in cells]

    def test_heatmap_filters_by_provider_account(self, session):
        now = datetime.now(UTC)
        # Other provider should not appear
        session.add(
            _event(
                event_id="a1",
                provider_id="chatgpt",
                account_id="user@example.com",
                ts=now,
                tokens_input=9999,
                tokens_output=0,
            )
        )
        # Target provider: 100 input + 50 output (default) = 150 tokens total
        session.add(
            _event(
                event_id="a2",
                provider_id="anthropic",
                account_id="user@example.com",
                ts=now - timedelta(seconds=1),
                tokens_input=100,
                tokens_output=50,
            )
        )
        session.commit()

        r = _client().get(
            "/api/v1/usage/heatmap",
            params={"provider_id": "anthropic", "account_id": "user@example.com", "days": 90},
        )
        assert r.status_code == 200
        total = sum(c["tokens"] for c in r.json()["cells"])
        assert total == 150  # only anthropic event (100 input + 50 output)


# ===========================================================================
# Task 7.4 — /sessions
# ===========================================================================


class TestSessionsEndpoint:
    """Tests for GET /api/v1/usage/sessions."""

    def test_sessions_groups_by_session_id(self, session):
        now = datetime.now(UTC)
        # Two events in same session
        session.add(
            _event(
                event_id="e1",
                session_id="sess-A",
                ts=now - timedelta(minutes=5),
                tokens_input=100,
                tokens_output=50,
            )
        )
        session.add(
            _event(event_id="e2", session_id="sess-A", ts=now, tokens_input=200, tokens_output=100)
        )
        # One event in another session
        session.add(
            _event(
                event_id="e3",
                session_id="sess-B",
                ts=now - timedelta(minutes=3),
                tokens_input=50,
                tokens_output=25,
            )
        )
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200, r.text
        sessions = r.json()["sessions"]
        assert len(sessions) == 2

        # Find sess-A
        sess_a = next(s for s in sessions if s["session_id"] == "sess-A")
        assert sess_a["msgs"] == 2

    def test_sessions_returns_top_n_by_tokens(self, session):
        now = datetime.now(UTC)
        # Small session
        session.add(
            _event(event_id="s1", session_id="small", ts=now, tokens_input=10, tokens_output=5)
        )
        # Large session (should come first)
        session.add(
            _event(
                event_id="l1",
                session_id="large",
                ts=now - timedelta(minutes=1),
                tokens_input=1000,
                tokens_output=500,
            )
        )
        session.add(
            _event(
                event_id="l2",
                session_id="large",
                ts=now - timedelta(minutes=2),
                tokens_input=1000,
                tokens_output=500,
            )
        )
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={"provider_id": "anthropic", "account_id": "user@example.com", "limit": 2},
        )
        assert r.status_code == 200
        sessions = r.json()["sessions"]
        assert len(sessions) == 2
        # Large session first (more tokens)
        assert sessions[0]["session_id"] == "large"
        assert sessions[0]["tokens_total"] == 3000

    def test_sessions_includes_distinct_models(self, session):
        now = datetime.now(UTC)
        session.add(_event(event_id="m1", session_id="sess-X", model_id="sonnet", ts=now))
        session.add(
            _event(
                event_id="m2", session_id="sess-X", model_id="haiku", ts=now - timedelta(minutes=1)
            )
        )
        session.add(
            _event(
                event_id="m3", session_id="sess-X", model_id="sonnet", ts=now - timedelta(minutes=2)
            )
        )
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200
        sess = r.json()["sessions"][0]
        assert set(sess["models"]) == {"sonnet", "haiku"}

    def test_sessions_filters_by_since(self, session):
        # Old session (before cutoff)
        old_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        session.add(_event(event_id="old", session_id="old-sess", ts=old_ts, tokens_input=500))
        # Recent session
        new_ts = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
        session.add(_event(event_id="new", session_id="new-sess", ts=new_ts, tokens_input=100))
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "since": "2026-04-01T00:00:00Z",
            },
        )
        assert r.status_code == 200
        sessions = r.json()["sessions"]
        session_ids = {s["session_id"] for s in sessions}
        assert "new-sess" in session_ids
        assert "old-sess" not in session_ids

    def test_sessions_excludes_null_session_id(self, session):
        now = datetime.now(UTC)
        # Event with no session_id should be excluded
        session.add(_event(event_id="no-sess", session_id=None, ts=now))
        session.add(
            _event(event_id="with-sess", session_id="real-session", ts=now - timedelta(seconds=1))
        )
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200
        sessions = r.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "real-session"

    def test_sessions_duration_seconds(self, session):
        ts_start = datetime(2026, 5, 8, 14, 0, 0, tzinfo=UTC)
        ts_end = datetime(2026, 5, 8, 14, 36, 31, tzinfo=UTC)  # 2191 seconds
        session.add(_event(event_id="d1", session_id="dur-sess", ts=ts_start))
        session.add(_event(event_id="d2", session_id="dur-sess", ts=ts_end))
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200
        sess = r.json()["sessions"][0]
        assert sess["duration_seconds"] == 2191

    def test_sessions_includes_token_breakdown(self, session):
        now = datetime.now(UTC)
        session.add(_event(
            event_id="e1", session_id="s1", ts=now,
            tokens_input=1000, tokens_output=400,
            tokens_cache_read=200, tokens_cache_create=50,
        ))
        session.add(_event(
            event_id="e2", session_id="s1", ts=now - timedelta(minutes=1),
            tokens_input=500, tokens_output=200,
            tokens_cache_read=100, tokens_cache_create=0,
        ))
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200
        s = r.json()["sessions"][0]
        assert s["tokens_input"] == 1500
        assert s["tokens_output"] == 600
        assert s["tokens_cache"] == 350  # 200+50+100+0
        assert s["tokens_cache_read"] == 300   # 200+100
        assert s["tokens_cache_create"] == 50  # 50+0

    def test_sessions_cache_pct(self, session):
        now = datetime.now(UTC)
        # tokens: input=800, output=300, cache_read=200, cache_create=150
        # tokens_total = 800+300+200+150 = 1450
        # cache_pct = round((200+150) / 1450 * 100) = round(24.1) = 24
        session.add(_event(
            event_id="c1", session_id="cache-sess", ts=now,
            tokens_input=800, tokens_output=300,
            tokens_cache_read=200, tokens_cache_create=150,
        ))
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200
        s = r.json()["sessions"][0]
        assert s["cache_pct"] == 24
        assert s["tokens_cache_read"] == 200
        assert s["tokens_cache_create"] == 150

    def test_sessions_cache_pct_zero_when_no_cache(self, session):
        now = datetime.now(UTC)
        session.add(_event(
            event_id="nc1", session_id="no-cache", ts=now,
            tokens_input=500, tokens_output=200,
            tokens_cache_read=0, tokens_cache_create=0,
        ))
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200
        s = r.json()["sessions"][0]
        assert s["cache_pct"] == 0

    def test_sessions_includes_reasoning_tokens(self, session):
        now = datetime.now(UTC)
        session.add(_event(
            event_id="r1", session_id="reason-sess", ts=now,
            tokens_input=500, tokens_output=200, tokens_reasoning=100,
        ))
        session.commit()

        r = _client().get(
            "/api/v1/usage/sessions",
            params={"provider_id": "anthropic", "account_id": "user@example.com"},
        )
        assert r.status_code == 200
        s = r.json()["sessions"][0]
        assert s["tokens_reasoning"] == 100
        assert s["tokens_total"] == s["tokens_input"] + s["tokens_output"] + s["tokens_cache"] + s["tokens_reasoning"]
