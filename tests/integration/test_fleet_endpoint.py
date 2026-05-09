"""Integration tests: GET /api/v1/usage/fleet — Fleet Commander aggregation.

Rewritten in Phase 9 to use LatestUsage + UsagePeriodRollup instead of
the deleted CumulativeUsage table.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import LatestUsage, UsageEvent, UsagePeriodRollup

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


def _seed_card(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str = "monthly",
    variant: str = "default",
    model_id: str = "",
    pct_used: float | None = None,
    service_name: str | None = None,
) -> None:
    card = {
        "service_name": service_name or f"{provider_id}-{window_type}",
        "provider_id": provider_id,
        "account_id": account_id,
        "window_type": window_type,
        "variant": variant,
        "pct_used": pct_used,
    }
    session.add(
        LatestUsage(
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id="local",
            window_type=window_type,
            variant=variant,
            model_id=model_id,
            card_json=json.dumps(card),
        )
    )


def _seed_rollup(
    session: Session,
    *,
    provider_id: str = "anthropic",
    account_id: str = "u@x.com",
    period_type: str = "month",
    period_key: str | None = None,
    model_id: str = "",
    sidecar_id: str = "",
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    cost_usd: float = 0.0,
    msgs: int = 0,
) -> UsagePeriodRollup:
    if period_key is None:
        period_key = datetime.now(UTC).strftime("%Y-%m") if period_type == "month" else "all"
    row = UsagePeriodRollup(
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
        last_updated=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fleet_returns_critical_gauge_per_account(session: Session):
    """When an account has multiple cards, the highest pct_used becomes critical_gauge."""
    _seed_card(
        session, provider_id="anthropic", account_id="acc1", window_type="weekly", pct_used=30.0
    )
    _seed_card(
        session, provider_id="anthropic", account_id="acc1", window_type="monthly", pct_used=85.0
    )
    session.commit()

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200, resp.text

    fleet = resp.json()["fleet"]
    assert len(fleet) == 1
    entry = fleet[0]
    assert entry["provider_id"] == "anthropic"
    assert entry["account_id"] == "acc1"
    assert entry["critical_gauge"]["pct_used"] == 85.0
    assert len(entry["secondary_limits"]) == 1
    assert entry["secondary_limits"][0]["pct_used"] == 30.0


def test_fleet_groups_by_provider_account(session: Session):
    """Each (provider_id, account_id) gets its own Fleet Commander entry."""
    _seed_card(session, provider_id="anthropic", account_id="acc1", pct_used=50.0)
    _seed_card(session, provider_id="chatgpt", account_id="acc1", pct_used=20.0)
    session.commit()

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    fleet = resp.json()["fleet"]
    assert len(fleet) == 2
    pids = {e["provider_id"] for e in fleet}
    assert pids == {"anthropic", "chatgpt"}


def test_fleet_includes_sidecar_contributions(session: Session):
    """Per-sidecar rollup rows for the current month appear in sidecar_contributions."""
    _seed_card(session, provider_id="anthropic", account_id="u@x.com", pct_used=10.0)
    session.commit()

    month_key = datetime.now(UTC).strftime("%Y-%m")
    _seed_rollup(
        session,
        provider_id="anthropic",
        account_id="u@x.com",
        period_type="month",
        period_key=month_key,
        model_id="",
        sidecar_id="laptop-1",
        tokens_input=9234,
        tokens_output=1500,
        cost_usd=0.42,
        msgs=7,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    entry = resp.json()["fleet"][0]
    contrib = entry["sidecar_contributions"]
    assert "laptop-1" in contrib
    assert contrib["laptop-1"]["tokens_input"] == 9234
    assert contrib["laptop-1"]["tokens_output"] == 1500
    assert contrib["laptop-1"]["cost_usd"] == pytest.approx(0.42)
    assert contrib["laptop-1"]["msgs"] == 7


def test_fleet_excludes_other_periods(session: Session):
    """Only current-month rollup rows appear in contributions; lifetime rows are excluded."""
    _seed_card(session, provider_id="anthropic", account_id="u@x.com", pct_used=10.0)
    session.commit()

    month_key = datetime.now(UTC).strftime("%Y-%m")

    # Current-month per-sidecar row — should appear
    _seed_rollup(
        session,
        period_type="month",
        period_key=month_key,
        sidecar_id="laptop-1",
        tokens_input=100,
    )

    # Lifetime per-sidecar row — should NOT appear
    _seed_rollup(
        session,
        period_type="lifetime",
        period_key="all",
        sidecar_id="laptop-1",
        tokens_input=999999,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    contrib = resp.json()["fleet"][0]["sidecar_contributions"]
    # laptop-1 is present, but its value comes only from the current-month row
    assert "laptop-1" in contrib
    assert contrib["laptop-1"]["tokens_input"] == 100


def test_fleet_skips_cross_product_rollup(session: Session):
    """Cross-product rows (model_id != '' AND sidecar_id != '') are excluded.

    Only pure per-sidecar rows (model_id='', sidecar_id!='') should appear.
    """
    _seed_card(session, provider_id="anthropic", account_id="u@x.com", pct_used=10.0)
    session.commit()

    month_key = datetime.now(UTC).strftime("%Y-%m")

    # Pure per-sidecar row (model_id='', sidecar_id set) — should appear
    _seed_rollup(
        session,
        period_type="month",
        period_key=month_key,
        model_id="",
        sidecar_id="laptop-1",
        tokens_input=500,
    )

    # Cross-product row (model_id AND sidecar_id both set) — must NOT appear
    _seed_rollup(
        session,
        period_type="month",
        period_key=month_key,
        model_id="claude-sonnet",
        sidecar_id="laptop-1",
        tokens_input=9999,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    contrib = resp.json()["fleet"][0]["sidecar_contributions"]
    assert "laptop-1" in contrib
    # tokens_input must be from the per-sidecar row (500), not the cross-product (9999)
    assert contrib["laptop-1"]["tokens_input"] == 500


def test_empty_db_returns_empty_fleet(session: Session):
    """No LatestUsage rows → fleet array is empty."""
    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fleet"] == []
    assert "generated_at" in body


# ---------------------------------------------------------------------------
# Phase 15.2 — window_aggregations field
# ---------------------------------------------------------------------------


def _seed_card_with_reset(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str = "weekly",
    variant: str = "default",
    model_id: str = "",
    pct_used: float | None = None,
    reset_at: str | None = None,
) -> None:
    """Seed a LatestUsage card that includes a reset_at field in the card_json."""
    card: dict = {
        "service_name": f"{provider_id}-{window_type}",
        "provider_id": provider_id,
        "account_id": account_id,
        "window_type": window_type,
        "variant": variant,
        "pct_used": pct_used,
    }
    if reset_at is not None:
        card["reset_at"] = reset_at
    session.add(
        LatestUsage(
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id="local",
            window_type=window_type,
            variant=variant,
            model_id=model_id,
            card_json=json.dumps(card),
        )
    )
    session.commit()


def _seed_event(
    session: Session,
    event_id: str,
    ts: datetime,
    *,
    provider_id: str = "anthropic",
    account_id: str = "u@x.com",
    model_id: str = "sonnet",
    sidecar_id: str = "dev-01",
    tokens_input: int = 100,
    tokens_output: int = 200,
    tokens_cache_read: int = 10,
    tokens_cache_create: int = 5,
    tokens_reasoning: int = 3,
    cost_usd: float = 0.01,
    kind: str = "message",
) -> None:
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id=sidecar_id,
            event_id=event_id,
            ts=ts,
            kind=kind,
            model_id=model_id,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_cache_read=tokens_cache_read,
            tokens_cache_create=tokens_cache_create,
            tokens_reasoning=tokens_reasoning,
            cost_usd=cost_usd,
        )
    )
    session.commit()


def test_fleet_includes_window_aggregations(session: Session):
    """Fleet entry has window_aggregations.longest with by_model, by_sidecar, total."""
    # reset_at for the weekly window: window covers [reset_at - 7d, reset_at)
    reset_at = datetime(2026, 5, 12, 18, 0, 0, tzinfo=UTC)
    mid_window = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)

    _seed_card_with_reset(
        session,
        provider_id="anthropic",
        account_id="u@x.com",
        window_type="weekly",
        pct_used=42.0,
        reset_at=reset_at.isoformat(),
    )

    # Two models, two sidecars
    _seed_event(
        session,
        "e1",
        mid_window,
        model_id="sonnet",
        sidecar_id="dev-01",
        tokens_input=100,
        tokens_output=200,
        tokens_cache_read=10,
        tokens_cache_create=5,
        tokens_reasoning=3,
    )
    _seed_event(
        session,
        "e2",
        mid_window + timedelta(hours=1),
        model_id="opus",
        sidecar_id="dev-01",
        tokens_input=50,
        tokens_output=100,
        tokens_cache_read=5,
        tokens_cache_create=2,
        tokens_reasoning=1,
    )
    _seed_event(
        session,
        "e3",
        mid_window + timedelta(hours=2),
        model_id="sonnet",
        sidecar_id="laptop",
        tokens_input=30,
        tokens_output=60,
        tokens_cache_read=3,
        tokens_cache_create=1,
        tokens_reasoning=0,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200, resp.text

    fleet = resp.json()["fleet"]
    assert len(fleet) == 1
    entry = fleet[0]

    assert "window_aggregations" in entry
    wa = entry["window_aggregations"]
    assert "longest" in wa

    longest = wa["longest"]
    assert longest["window_type"] == "weekly"
    assert "window_start" in longest
    assert "window_end" in longest

    # by_model: sonnet and opus should appear
    bm = longest["by_model"]
    assert set(bm.keys()) == {"sonnet", "opus"}
    assert bm["sonnet"]["tokens_input"] == 130  # 100 + 30
    assert bm["opus"]["tokens_input"] == 50

    # by_sidecar: dev-01 and laptop should appear
    bs = longest["by_sidecar"]
    assert set(bs.keys()) == {"dev-01", "laptop"}
    assert bs["dev-01"]["tokens_input"] == 150  # 100 + 50
    assert bs["laptop"]["tokens_input"] == 30

    # total token count
    tu = longest["token_usage"]
    expected_total = (100 + 200 + 10 + 5 + 3) + (50 + 100 + 5 + 2 + 1) + (30 + 60 + 3 + 1 + 0)
    assert tu["total"] == expected_total


def test_fleet_window_aggregations_empty_when_no_eligible_card(session: Session):
    """When no eligible card has reset_at, window_aggregations is empty dict {}."""
    # Card without reset_at (session window type, no reset_at field)
    _seed_card(
        session,
        provider_id="anthropic",
        account_id="u@x.com",
        window_type="session",
        variant="some-variant",
        pct_used=10.0,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    entry = resp.json()["fleet"][0]
    assert "window_aggregations" in entry
    assert entry["window_aggregations"] == {}


def test_fleet_window_aggregations_falls_back_to_model_card(session: Session):
    """When no model-agnostic card exists, _longest_window_card falls back to a
    model-specific card so per-model breakdown still populates. Reproduces the
    Gemini case (each model has its own daily quota; no overall card).
    """
    reset_at = datetime(2026, 5, 12, 18, 0, 0, tzinfo=UTC)
    mid_window = datetime(2026, 5, 12, 6, 0, 0, tzinfo=UTC)

    # Two per-model cards, no aggregate card with model_id=""
    _seed_card_with_reset(
        session,
        provider_id="gemini",
        account_id="u@x.com",
        window_type="daily",
        model_id="flash",
        pct_used=15.0,
        reset_at=reset_at.isoformat(),
    )
    _seed_card_with_reset(
        session,
        provider_id="gemini",
        account_id="u@x.com",
        window_type="daily",
        model_id="pro",
        pct_used=42.0,
        reset_at=reset_at.isoformat(),
    )

    # Events that should aggregate inside the daily window
    _seed_event(
        session,
        "ev-flash-1",
        mid_window,
        provider_id="gemini",
        account_id="u@x.com",
        model_id="flash",
        tokens_input=100,
        tokens_output=50,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
        cost_usd=0.005,
    )
    _seed_event(
        session,
        "ev-pro-1",
        mid_window,
        provider_id="gemini",
        account_id="u@x.com",
        model_id="pro",
        tokens_input=200,
        tokens_output=80,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
        cost_usd=0.02,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    entry = next(e for e in resp.json()["fleet"] if e["provider_id"] == "gemini")
    longest = entry["window_aggregations"]["longest"]
    assert longest["window_type"] == "daily"
    assert set(longest["by_model"].keys()) == {"flash", "pro"}
    assert longest["by_model"]["flash"]["tokens_input"] == 100
    assert longest["by_model"]["pro"]["tokens_input"] == 200
