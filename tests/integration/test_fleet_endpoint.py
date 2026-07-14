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
from app.models.db import LatestUsage, UsageEvent

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
    """Per-sidecar token totals for the current local month, aggregated live
    from usage_events, appear in sidecar_contributions."""
    _seed_card(session, provider_id="anthropic", account_id="u@x.com", pct_used=10.0)
    session.commit()

    _seed_event(
        session,
        "e1",
        datetime.now(UTC),
        provider_id="anthropic",
        account_id="u@x.com",
        sidecar_id="laptop-1",
        tokens_input=9234,
        tokens_output=1500,
        cost_usd=0.42,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    entry = resp.json()["fleet"][0]
    contrib = entry["sidecar_contributions"]
    assert "laptop-1" in contrib
    assert contrib["laptop-1"]["tokens_input"] == 9234
    assert contrib["laptop-1"]["tokens_output"] == 1500
    assert contrib["laptop-1"]["cost_usd"] == pytest.approx(0.42)
    assert contrib["laptop-1"]["msgs"] == 1


def test_fleet_excludes_other_periods(session: Session):
    """Only events in the current local month contribute; older events are excluded."""
    _seed_card(session, provider_id="anthropic", account_id="u@x.com", pct_used=10.0)
    session.commit()

    # Current-month event — should appear
    _seed_event(session, "this", datetime.now(UTC), sidecar_id="laptop-1", tokens_input=100)
    # Event ~40 days ago (prior month) — should NOT appear
    _seed_event(
        session,
        "old",
        datetime.now(UTC) - timedelta(days=40),
        sidecar_id="laptop-1",
        tokens_input=999999,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    contrib = resp.json()["fleet"][0]["sidecar_contributions"]
    # laptop-1 is present, but its value comes only from the current-month event
    assert "laptop-1" in contrib
    assert contrib["laptop-1"]["tokens_input"] == 100


def test_fleet_sidecar_contribution_sums_across_models(session: Session):
    """A sidecar's contribution sums every model's events under that sidecar."""
    _seed_card(session, provider_id="anthropic", account_id="u@x.com", pct_used=10.0)
    session.commit()

    now = datetime.now(UTC)
    _seed_event(session, "a", now, sidecar_id="laptop-1", model_id="sonnet", tokens_input=500)
    _seed_event(session, "b", now, sidecar_id="laptop-1", model_id="opus", tokens_input=300)

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    contrib = resp.json()["fleet"][0]["sidecar_contributions"]
    assert "laptop-1" in contrib
    assert contrib["laptop-1"]["tokens_input"] == 800  # 500 + 300 across models
    assert contrib["laptop-1"]["msgs"] == 2


def test_empty_db_returns_empty_fleet(session: Session):
    """No LatestUsage rows → fleet array is empty."""
    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fleet"] == []
    assert "generated_at" in body


def test_fleet_synthetic_card_carries_lifetime_token_totals(session: Session):
    """A passive provider (events, no LatestUsage card) gets a synthetic fleet entry
    whose critical_gauge is populated with lifetime token_usage, used_value, and msgs.

    This covers the "opencode-free shows 0" regression: before the fix the synthetic
    shell had token_usage=null/used_value=null/msgs=null, causing the dashboard token
    card to display '0' despite real usage in the event log.
    """
    now = datetime.now(UTC)
    # Seed two events — different sidecars / models, same passive provider
    _seed_event(
        session,
        "ev1",
        now,
        provider_id="opencode-free",
        account_id="user@example.com",
        sidecar_id="laptop",
        tokens_input=1_000,
        tokens_output=200,
        tokens_cache_read=5_000,
        tokens_cache_create=200,
        tokens_reasoning=100,
        cost_usd=0.05,
    )
    _seed_event(
        session,
        "ev2",
        now,
        provider_id="opencode-free",
        account_id="user@example.com",
        sidecar_id="desktop",
        tokens_input=500,
        tokens_output=80,
        tokens_cache_read=1_000,
        tokens_cache_create=100,
        tokens_reasoning=20,
        cost_usd=0.01,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    entries = resp.json()["fleet"]
    entry = next(e for e in entries if e["provider_id"] == "opencode-free")

    cg = entry["critical_gauge"]
    assert cg["is_unlimited"] is True
    assert cg["window_type"] == "lifetime"

    # token_usage must be present and summed across both events
    tu = cg["token_usage"]
    assert tu is not None
    assert tu["input"] == 1_500  # 1000 + 500
    assert tu["output"] == 280  # 200 + 80
    assert tu["reasoning"] == 120  # 100 + 20
    assert tu["cache_read"] == 6_000  # 5000 + 1000
    assert tu["cache_create"] == 300  # 200 + 100
    # total = input + output + reasoning (cache excluded, matches Go card convention)
    assert tu["total"] == 1_500 + 280 + 120

    # used_value must mirror total
    assert cg["used_value"] == tu["total"]

    # msgs must reflect the event count
    assert cg["msgs"] == 2


def test_fleet_surfaces_opencode_byok_as_its_own_synthetic_card(session: Session):
    """OpenCode bring-your-own-key events (issue #182) get their own passive
    provider — 'opencode-byok' — distinct from both the Go card and
    'opencode-free', instead of being folded into the Go tier's usage.
    """
    now = datetime.now(UTC)
    _seed_event(
        session,
        "byok1",
        now,
        provider_id="opencode-byok",
        account_id="default",
        tokens_input=300,
        tokens_output=50,
        cost_usd=0.0,
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    entries = resp.json()["fleet"]
    entry = next(e for e in entries if e["provider_id"] == "opencode-byok")

    cg = entry["critical_gauge"]
    assert cg["service_name"] == "Opencode Byok"
    assert cg["is_unlimited"] is True
    assert cg["token_usage"]["input"] == 300
    assert cg["token_usage"]["output"] == 50


def test_fleet_ignores_opencode_error_events(session: Session):
    """A failed openrouter/ollama request (kind='error') must not appear as
    usage on its sub-provider's card — it never actually incurred any tokens.
    """
    now = datetime.now(UTC)
    _seed_event(
        session,
        "err1",
        now,
        provider_id="opencode-openrouter",
        account_id="default",
        tokens_input=999,  # would be wrong if counted; error events carry none in practice
        kind="error",
    )

    resp = _client().get("/api/v1/usage/fleet")
    assert resp.status_code == 200

    entries = resp.json()["fleet"]
    assert not any(e["provider_id"] == "opencode-openrouter" for e in entries)


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
