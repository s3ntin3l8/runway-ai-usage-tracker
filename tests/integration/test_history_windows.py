"""Integration tests for window-first history endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import QuotaSnapshot, UsageWindow
from app.services.pricing_seed import seed_pricing_table


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


def _window(
    *,
    provider_id="anthropic",
    account_id="user@example.com",
    window_type="weekly",
    window_start,
    window_end,
    msgs=10,
    tokens_input=100_000,
    tokens_output=50_000,
    tokens_cache_read=0,
    tokens_cache_create=0,
    tokens_reasoning=0,
    cost_usd=1.50,
    limit_value=1_000_000.0,
    pct_used=15.0,
    model_id="",
    sidecar_id="",
) -> UsageWindow:
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


# ---------------------------------------------------------------------------
# Task 1: QuotaSnapshot model
# ---------------------------------------------------------------------------


def test_quota_snapshot_model_creates(session):
    """QuotaSnapshot can be inserted and retrieved."""
    ts = datetime.now(UTC)
    snap = QuotaSnapshot(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        model_id="",
        ts=ts,
        pct_used=42.5,
        reset_at=ts + timedelta(days=7),
    )
    session.add(snap)
    session.commit()
    session.refresh(snap)
    assert snap.id is not None
    assert snap.pct_used == 42.5


# ---------------------------------------------------------------------------
# Task 2: record_quota_snapshot / upsert_latest_usage write path
# ---------------------------------------------------------------------------


def test_upsert_latest_usage_creates_quota_snapshot(session):
    """upsert_latest_usage writes a QuotaSnapshot when pct_used is present."""
    from sqlmodel import select

    from app.services.accumulator import upsert_latest_usage

    card = {
        "provider_id": "anthropic",
        "account_id": "user@example.com",
        "window_type": "weekly",
        "model_id": "",
        "pct_used": 55.0,
        "used_value": 550000,
        "limit_value": 1000000,
        "unit_type": "tokens",
        "service_name": "Claude",
        "data_source": "api",
    }
    upsert_latest_usage(session, card)
    session.commit()

    snaps = session.exec(
        select(QuotaSnapshot).where(QuotaSnapshot.provider_id == "anthropic")
    ).all()
    assert len(snaps) == 1
    assert snaps[0].pct_used == 55.0
    assert snaps[0].window_type == "weekly"


def test_upsert_latest_usage_skips_snapshot_when_no_pct_used(session):
    """upsert_latest_usage does NOT write a QuotaSnapshot when pct_used is None."""
    from sqlmodel import select

    from app.services.accumulator import upsert_latest_usage

    card = {
        "provider_id": "anthropic",
        "account_id": "user@example.com",
        "window_type": "session",
        "model_id": "sonnet",
        "service_name": "Claude",
        "data_source": "local",
    }
    upsert_latest_usage(session, card)
    session.commit()

    snaps = session.exec(select(QuotaSnapshot)).all()
    assert len(snaps) == 0


# ---------------------------------------------------------------------------
# Task 3: query_windows
# ---------------------------------------------------------------------------


def test_query_windows_returns_closed_windows(session):
    """query_windows includes closed windows from usage_windows."""
    from app.services.event_query import query_windows

    now = datetime.now(UTC)
    w = _window(
        window_start=now - timedelta(days=14),
        window_end=now - timedelta(days=7),
        pct_used=82.0,
        tokens_input=800_000,
        tokens_output=200_000,
        cost_usd=12.50,
    )
    session.add(w)
    session.commit()

    result = query_windows(session, days=30)
    assert len(result["windows"]) == 1
    row = result["windows"][0]
    assert row["provider_id"] == "anthropic"
    assert row["window_type"] == "weekly"
    assert row["pct_used"] == 82.0
    assert row["is_open"] is False
    assert row["tokens_total"] == 1_000_000
    assert row["cost_usd"] == 12.50


def test_query_windows_respects_days_filter(session):
    """query_windows excludes windows outside the requested day range."""
    from app.services.event_query import query_windows

    now = datetime.now(UTC)
    old = _window(
        window_start=now - timedelta(days=60),
        window_end=now - timedelta(days=53),
    )
    session.add(old)
    session.commit()

    result = query_windows(session, days=30)
    assert len(result["windows"]) == 0


def test_query_windows_open_row_falls_back_to_live_usage_events(session):
    """An open window whose card has no token_usage/cost_usd (e.g. the
    percent-only Anthropic quota card) still gets tokens/cost/top_model
    filled in from a live aggregation over usage_events, instead of
    staying blank."""
    import json

    from app.models.db import LatestUsage, UsageEvent
    from app.services.event_query import query_windows

    now = datetime.now(UTC)
    reset_at = now + timedelta(days=2)

    card = {
        "provider_id": "anthropic",
        "account_id": "user@example.com",
        "window_type": "weekly",
        "model_id": "",
        "pct_used": 90.0,
        "unit_type": "percent",
        "reset_at": reset_at.isoformat(),
        "service_name": "Claude",
        # No token_usage / cost_usd — mirrors the real Anthropic card.
    }
    session.add(
        LatestUsage(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            card_json=json.dumps(card),
        )
    )
    session.add(
        UsageEvent(
            provider_id="anthropic",
            account_id="user@example.com",
            sidecar_id="dev-01",
            event_id="e1",
            ts=now - timedelta(days=1),
            kind="message",
            model_id="sonnet",
            tokens_input=1000,
            tokens_output=500,
            cost_usd=0.05,
        )
    )
    session.commit()

    result = query_windows(session, days=30)
    open_rows = [r for r in result["windows"] if r["is_open"]]
    assert len(open_rows) == 1
    row = open_rows[0]
    assert row["pct_used"] == 90.0
    assert row["tokens_total"] == 1500
    assert row["cost_usd"] == 0.05
    assert row["top_model"] == "sonnet"


def test_query_windows_open_row_unknown_window_type_does_not_error(session):
    """An open window with a provider-specific window_type not present in
    WINDOW_DURATION (e.g. a per-model weekly quota) must not raise — it
    just keeps the card-derived (possibly None) token/cost fields."""
    import json

    from app.models.db import LatestUsage
    from app.services.event_query import query_windows

    now = datetime.now(UTC)
    reset_at = now + timedelta(days=2)

    card = {
        "provider_id": "gemini",
        "account_id": "user@example.com",
        "window_type": "weekly_opus",
        "model_id": "",
        "pct_used": 40.0,
        "unit_type": "percent",
        "reset_at": reset_at.isoformat(),
        "service_name": "Gemini",
    }
    session.add(
        LatestUsage(
            provider_id="gemini",
            account_id="user@example.com",
            window_type="weekly_opus",
            model_id="",
            card_json=json.dumps(card),
        )
    )
    session.commit()

    result = query_windows(session, days=30)
    open_rows = [r for r in result["windows"] if r["is_open"]]
    assert len(open_rows) == 1
    row = open_rows[0]
    assert row["pct_used"] == 40.0
    assert row["tokens_total"] is None
    assert row["cost_usd"] is None
    assert row["top_model"] is None


# ---------------------------------------------------------------------------
# Task 4: query_chart
# ---------------------------------------------------------------------------


def test_query_chart_percent_returns_fill_curves(session):
    """query_chart with metric=percent reads from quota_snapshots."""
    from app.services.event_query import query_chart

    now = datetime.now(UTC)
    for i, pct in enumerate([10.0, 25.0, 45.0]):
        session.add(
            QuotaSnapshot(
                provider_id="anthropic",
                account_id="user@example.com",
                window_type="weekly",
                model_id="",
                ts=now - timedelta(days=6 - i),
                pct_used=pct,
            )
        )
    session.commit()

    result = query_chart(session, metric="percent", days=30)
    assert "series" in result
    assert len(result["series"]) == 1
    s = result["series"][0]
    assert s["provider_id"] == "anthropic"
    assert s["window_type"] == "weekly"
    assert len(s["points"]) == 3
    assert s["points"][0]["pct_used"] == 10.0


def test_query_chart_tokens_returns_hourly_bars(session):
    """query_chart with metric=tokens uses hourly rollups for days<=7."""
    from app.models.db import UsagePeriodRollup
    from app.services.event_query import query_chart

    now = datetime.now(UTC)
    session.add(
        UsagePeriodRollup(
            provider_id="anthropic",
            account_id="user@example.com",
            period_type="hour",
            period_key=(now - timedelta(hours=2)).strftime("%Y-%m-%dT%H"),
            model_id="",
            sidecar_id="",
            tokens_input=300_000,
            tokens_output=100_000,
            cost_usd=2.50,
            msgs=15,
        )
    )
    session.commit()

    result = query_chart(session, metric="tokens", days=7)
    assert "bars" in result
    assert len(result["bars"]) >= 1
    bar = result["bars"][0]
    assert "date" in bar
    assert "ts" in bar
    total = sum(s["value"] for s in bar["segments"])
    assert total == 400_000


def test_query_chart_tokens_hourly_for_short_window(session):
    """query_chart for days=1 returns one bar per hourly rollup row (regression for 'only 2 bars' bug)."""
    from app.models.db import UsagePeriodRollup
    from app.services.event_query import query_chart

    now = datetime.now(UTC)
    for hours_ago in [1, 3, 5]:
        session.add(
            UsagePeriodRollup(
                provider_id="anthropic",
                account_id="user@example.com",
                period_type="hour",
                period_key=(now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H"),
                model_id="",
                sidecar_id="",
                tokens_input=10_000,
                tokens_output=5_000,
                cost_usd=0.10,
                msgs=3,
            )
        )
    session.commit()

    result = query_chart(session, metric="tokens", days=1)
    assert "bars" in result
    assert len(result["bars"]) == 3
    # Each bar must carry a full ISO timestamp
    for bar in result["bars"]:
        assert "ts" in bar
        # ts should be parseable and not just a date
        assert "T" in bar["ts"]


# ---------------------------------------------------------------------------
# Task 5: query_window_detail
# ---------------------------------------------------------------------------


def test_query_window_detail_returns_fill_and_models(session):
    """query_window_detail returns fill_series + by_model for a window."""
    from app.models.db import UsagePeriodRollup
    from app.services.event_query import query_window_detail

    now = datetime.now(UTC)
    window_start = now - timedelta(days=7)

    for i, pct in enumerate([5.0, 20.0, 40.0]):
        session.add(
            QuotaSnapshot(
                provider_id="anthropic",
                account_id="user@example.com",
                window_type="weekly",
                model_id="",
                ts=window_start + timedelta(days=i + 1),
                pct_used=pct,
            )
        )

    for model, inp, out in [("sonnet", 400_000, 150_000), ("haiku", 50_000, 20_000)]:
        session.add(
            UsagePeriodRollup(
                provider_id="anthropic",
                account_id="user@example.com",
                period_type="day",
                period_key=(window_start + timedelta(days=2)).strftime("%Y-%m-%d"),
                model_id=model,
                sidecar_id="",
                tokens_input=inp,
                tokens_output=out,
                cost_usd=0.5,
                msgs=5,
            )
        )
    session.commit()

    result = query_window_detail(
        session,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=window_start,
        window_end=now,
    )
    assert len(result["fill_series"]) == 3
    assert result["fill_series"][1]["pct_used"] == 20.0
    assert len(result["by_model"]) == 2
    model_ids = {m["model_id"] for m in result["by_model"]}
    assert "sonnet" in model_ids


# ---------------------------------------------------------------------------
# Task 6: Endpoint smoke tests
# ---------------------------------------------------------------------------


def test_windows_endpoint_returns_200(session):
    now = datetime.now(UTC)
    w = _window(
        window_start=now - timedelta(days=7),
        window_end=now,
    )
    session.add(w)
    session.commit()

    r = _client().get("/api/v1/usage/history/windows?days=30")
    assert r.status_code == 200
    data = r.json()
    assert "windows" in data
    assert data["total"] >= 1


def test_chart_endpoint_percent_returns_200(session):
    now = datetime.now(UTC)
    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            ts=now - timedelta(days=1),
            pct_used=30.0,
        )
    )
    session.commit()

    r = _client().get("/api/v1/usage/history/chart?metric=percent&days=7")
    assert r.status_code == 200
    assert "series" in r.json()


def test_window_detail_endpoint_returns_200(session):
    now = datetime.now(UTC)
    w_start = now - timedelta(days=7)
    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="user@example.com",
            window_type="weekly",
            model_id="",
            ts=now - timedelta(days=3),
            pct_used=55.0,
        )
    )
    session.commit()

    r = _client().get(
        f"/api/v1/usage/history/window-detail"
        f"?provider_id=anthropic&account_id=user%40example.com"
        f"&window_type=weekly&window_start={w_start.isoformat()}&window_end={now.isoformat()}"
    )
    assert r.status_code == 200
    body = r.json()
    assert "fill_series" in body
    assert "by_model" in body
