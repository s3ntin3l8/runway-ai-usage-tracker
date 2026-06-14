"""Integration tests: GET /api/v1/usage/cumulative reads from usage_period_rollup.

Replaces the old CumulativeUsage-based test file (deleted in Phase 8).
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import SystemConfig, UsageEvent, UsagePeriodRollup

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


def _client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rollup(
    session: Session,
    *,
    provider_id: str = "anthropic",
    account_id: str = "u@x.com",
    period_type: str = "lifetime",
    period_key: str = "all",
    model_id: str = "",
    sidecar_id: str = "",
    msgs: int = 0,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    cost_usd: float = 0.0,
) -> UsagePeriodRollup:
    """Insert and return a UsagePeriodRollup row."""
    row = UsagePeriodRollup(
        provider_id=provider_id,
        account_id=account_id,
        period_type=period_type,
        period_key=period_key,
        model_id=model_id,
        sidecar_id=sidecar_id,
        msgs=msgs,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        cost_usd=cost_usd,
        last_updated=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    return row


def _event(
    session: Session,
    event_id: str,
    ts: datetime,
    *,
    provider_id: str = "anthropic",
    account_id: str = "u@x.com",
    model_id: str = "sonnet",
    sidecar_id: str = "dev-01",
    tokens_input: int = 0,
    cost_usd: float = 0.0,
) -> None:
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
            event_id=event_id,
            ts=ts,
            kind="message",
            model_id=model_id,
            tokens_input=tokens_input,
            cost_usd=cost_usd,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_db_returns_empty_cumulative(session):
    """No rollup rows → empty cumulative list with generated_at."""
    resp = _client().get("/api/v1/usage/cumulative")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cumulative"] == []
    assert "generated_at" in body


def test_one_period_one_grain_returns_lifetime_bucket(session):
    """Single all-up lifetime row → entry with lifetime bucket, empty by_model / by_sidecar."""
    _rollup(session, period_type="lifetime", period_key="all", msgs=10, tokens_input=100)

    resp = _client().get("/api/v1/usage/cumulative")
    assert resp.status_code == 200
    cumulative = resp.json()["cumulative"]
    assert len(cumulative) == 1

    entry = cumulative[0]
    assert entry["provider_id"] == "anthropic"
    assert entry["account_id"] == "u@x.com"

    lifetime = entry["lifetime"]
    assert lifetime["msgs"] == 10
    assert lifetime["tokens_input"] == 100
    assert lifetime["by_model"] == {}
    assert lifetime["by_sidecar"] == {}


def test_per_model_grain_lands_in_by_model(session):
    """All-up + per-model row → by_model has the model entry with correct values."""
    # Top-level totals
    _rollup(session, period_type="lifetime", period_key="all", msgs=15, tokens_input=500)
    # Per-model row (model_id set, sidecar_id empty)
    _rollup(
        session,
        period_type="lifetime",
        period_key="all",
        model_id="sonnet",
        msgs=10,
        tokens_input=300,
        cost_usd=0.50,
    )

    resp = _client().get("/api/v1/usage/cumulative")
    assert resp.status_code == 200
    entry = resp.json()["cumulative"][0]
    lifetime = entry["lifetime"]

    assert lifetime["msgs"] == 15
    assert lifetime["tokens_input"] == 500
    assert "sonnet" in lifetime["by_model"]
    assert lifetime["by_model"]["sonnet"]["msgs"] == 10
    assert lifetime["by_model"]["sonnet"]["tokens_input"] == 300
    assert lifetime["by_model"]["sonnet"]["cost_usd"] == pytest.approx(0.50)
    assert lifetime["by_sidecar"] == {}


def test_per_sidecar_grain_lands_in_by_sidecar(session):
    """All-up + per-sidecar row → by_sidecar has the sidecar entry with correct values."""
    _rollup(session, period_type="lifetime", period_key="all", msgs=20, tokens_input=800)
    # Per-sidecar row (model_id empty, sidecar_id set)
    _rollup(
        session,
        period_type="lifetime",
        period_key="all",
        sidecar_id="dev-01",
        msgs=12,
        tokens_input=400,
        cost_usd=1.20,
    )

    resp = _client().get("/api/v1/usage/cumulative")
    assert resp.status_code == 200
    entry = resp.json()["cumulative"][0]
    lifetime = entry["lifetime"]

    assert lifetime["msgs"] == 20
    assert lifetime["tokens_input"] == 800
    assert lifetime["by_model"] == {}
    assert "dev-01" in lifetime["by_sidecar"]
    assert lifetime["by_sidecar"]["dev-01"]["msgs"] == 12
    assert lifetime["by_sidecar"]["dev-01"]["tokens_input"] == 400
    assert lifetime["by_sidecar"]["dev-01"]["cost_usd"] == pytest.approx(1.20)


def test_full_breakdown_row_skipped(session):
    """Cross-product row (model_id != '' AND sidecar_id != '') does not appear in by_model or by_sidecar."""
    _rollup(session, period_type="lifetime", period_key="all", msgs=5, tokens_input=200)
    # Per-model row — should appear in by_model
    _rollup(
        session,
        period_type="lifetime",
        period_key="all",
        model_id="opus",
        msgs=5,
        tokens_input=200,
    )
    # Cross-product row — should be silently ignored
    _rollup(
        session,
        period_type="lifetime",
        period_key="all",
        model_id="opus",
        sidecar_id="dev-01",
        msgs=5,
        tokens_input=200,
    )

    resp = _client().get("/api/v1/usage/cumulative")
    assert resp.status_code == 200
    lifetime = resp.json()["cumulative"][0]["lifetime"]

    # by_model has exactly the per-model grain row, not the cross-product
    assert list(lifetime["by_model"].keys()) == ["opus"]
    # by_sidecar must be empty (no pure per-sidecar row was inserted)
    assert lifetime["by_sidecar"] == {}


def test_multiple_periods_returns_all_three_buckets(session):
    """lifetime comes from the rollup; year + month are aggregated live from
    usage_events at the user-local period boundary."""
    now = datetime.now(UTC)

    # Lifetime stays sourced from the (tz-independent) rollup.
    _rollup(session, period_type="lifetime", period_key="all", msgs=100, tokens_input=5000)
    # This-month event → counts toward both month and year.
    _event(session, "m", now, tokens_input=1000)
    expected_year_tokens, expected_year_msgs = 1000, 1
    if now.month > 1:
        # Earlier this year but before this month → counts toward year only.
        _event(session, "y", now.replace(month=1, day=15, hour=12), tokens_input=2000)
        expected_year_tokens, expected_year_msgs = 3000, 2

    resp = _client().get("/api/v1/usage/cumulative")
    assert resp.status_code == 200
    body = resp.json()
    entry = body["cumulative"][0]
    # Index by the server-resolved (tz-aware) keys instead of recomputing.
    month_bucket = entry[body["current_month_key"]]
    year_bucket = entry[body["current_year_key"]]

    assert entry["lifetime"]["msgs"] == 100
    assert entry["lifetime"]["tokens_input"] == 5000
    assert month_bucket["msgs"] == 1
    assert month_bucket["tokens_input"] == 1000
    assert year_bucket["msgs"] == expected_year_msgs
    assert year_bucket["tokens_input"] == expected_year_tokens


def test_query_param_filters_period_type(session):
    """?period_type=month → only month bucket populated; lifetime/year are empty stubs.

    The "stable shape" rule: the entry still has lifetime and current-year keys
    but they carry zero values because no matching rows exist for those buckets.
    """
    now = datetime.now(UTC)
    month_key = now.strftime("%Y-%m")
    year_key = now.strftime("%Y")

    # Insert all three period types, but only month should survive the filter
    _rollup(session, period_type="lifetime", period_key="all", msgs=100, tokens_input=5000)
    _rollup(session, period_type="year", period_key=year_key, msgs=60, tokens_input=3000)
    _rollup(session, period_type="month", period_key=month_key, msgs=20, tokens_input=1000)

    resp = _client().get("/api/v1/usage/cumulative?period_type=month")
    assert resp.status_code == 200
    cumulative = resp.json()["cumulative"]

    # At least one entry should exist (month row matched)
    assert len(cumulative) == 1
    entry = cumulative[0]

    # Month bucket populated
    assert entry[f"month_{month_key}"]["msgs"] == 20
    assert entry[f"month_{month_key}"]["tokens_input"] == 1000

    # Lifetime bucket is the zero-stub (no matching row survived the filter)
    assert entry["lifetime"]["msgs"] == 0
    assert entry["lifetime"]["tokens_input"] == 0

    # Year bucket is the zero-stub
    assert entry[f"year_{year_key}"]["msgs"] == 0


def test_month_live_aggregates_from_events_not_rollup(session):
    """?period_type=month&period_key=YYYY-MM takes the tz-correct live path:
    the month bucket is aggregated from usage_events, not the UTC rollup."""
    # A stale/wrong rollup row for the month — must be ignored by the live path.
    _rollup(session, period_type="month", period_key="2026-04", msgs=999, tokens_input=999_999)
    # Real events in April and a neighbouring month.
    _event(session, "apr1", datetime(2026, 4, 5, 12, 0, tzinfo=UTC), tokens_input=100)
    _event(session, "apr2", datetime(2026, 4, 20, 12, 0, tzinfo=UTC), tokens_input=50)
    _event(session, "may1", datetime(2026, 5, 1, 12, 0, tzinfo=UTC), tokens_input=7)

    resp = _client().get("/api/v1/usage/cumulative?period_type=month&period_key=2026-04")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current_month_key"] == "month_2026-04"
    entry = body["cumulative"][0]
    bucket = entry["month_2026-04"]
    assert bucket["tokens_input"] == 150  # apr1 + apr2, not the rollup's 999_999
    assert bucket["msgs"] == 2


def test_month_live_respects_user_local_boundary(session):
    """An event after local-month midnight but before UTC midnight lands in the
    new local month — the crux of the tz-correct path."""
    session.add(SystemConfig(id=1, user_timezone="Europe/Berlin"))
    session.commit()
    # 23:30 UTC on Apr 30 == 01:30 May 1 in Berlin (UTC+2) → belongs to May.
    _event(session, "boundary", datetime(2026, 4, 30, 23, 30, tzinfo=UTC), tokens_input=11)
    # Clearly-April event.
    _event(session, "april", datetime(2026, 4, 10, 12, 0, tzinfo=UTC), tokens_input=5)

    april = _client().get("/api/v1/usage/cumulative?period_type=month&period_key=2026-04").json()
    may = _client().get("/api/v1/usage/cumulative?period_type=month&period_key=2026-05").json()

    assert april["cumulative"][0]["month_2026-04"]["tokens_input"] == 5
    assert may["cumulative"][0]["month_2026-05"]["tokens_input"] == 11


def test_query_param_filters_provider_id(session):
    """?provider_id=anthropic → only anthropic entries returned."""
    _rollup(session, provider_id="anthropic", account_id="a@x.com", msgs=10, tokens_input=100)
    _rollup(session, provider_id="chatgpt", account_id="a@x.com", msgs=5, tokens_input=50)

    resp = _client().get("/api/v1/usage/cumulative?provider_id=anthropic")
    assert resp.status_code == 200
    cumulative = resp.json()["cumulative"]

    assert len(cumulative) == 1
    assert cumulative[0]["provider_id"] == "anthropic"
    assert cumulative[0]["lifetime"]["msgs"] == 10
