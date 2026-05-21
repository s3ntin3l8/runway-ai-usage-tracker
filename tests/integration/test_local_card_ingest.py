"""Integration test: sidecar-pushed LimitCards via /fleet/ingest land in LatestUsage."""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import LatestUsage
from app.services.pricing_seed import seed_pricing_table

TEST_KEY = "test-local-card-ingest-key"


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


def _signed(payload: dict, key: str = TEST_KEY) -> tuple[bytes, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(time.time())
    sig = hmac.new(key.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    return body, {"X-Signature": sig, "X-Timestamp": ts}


def _ingest(client: TestClient, payload: dict) -> dict:
    body, headers = _signed(payload)
    r = client.post("/api/v1/fleet/ingest", content=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_sidecar_pushed_card_lands_in_latest_usage(session):
    """A LimitCard pushed via /fleet/ingest metrics[] should appear in LatestUsage."""
    payload = {
        "provider": "anthropic-sidecar",
        "sidecar_id": "test-host-01",
        "metrics": [
            {
                "provider_id": "anthropic",
                "account_id": "user@example.com",
                "account_label": "user@example.com",
                "service_name": "Claude",
                "window_type": "weekly",
                "variant": "default",
                "model_id": "",
                "icon": "🤖",
                "remaining": "80%",
                "unit": "capacity",
                "reset": "weekly",
                "health": "good",
                "pace": "Stable",
                "detail": "Test card from sidecar",
                "used_value": 20.0,
                "limit_value": 100.0,
                "pct_used": 20.0,
                "unit_type": "percent",
                "data_source": "web",
            }
        ],
        "events": [],
    }

    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)
        resp = _ingest(client, payload)

    assert resp["metrics_stored"] == 1

    # LatestUsage should have a row for this card
    rows = session.exec(
        select(LatestUsage).where(
            LatestUsage.provider_id == "anthropic",
            LatestUsage.account_id == "user@example.com",
            LatestUsage.window_type == "weekly",
        )
    ).all()
    assert len(rows) == 1, f"Expected 1 LatestUsage row, got {len(rows)}"
    row = rows[0]
    assert row.sidecar_id == "test-host-01"
    card_data = json.loads(row.card_json)
    assert card_data["provider_id"] == "anthropic"
    assert card_data["window_type"] == "weekly"
    assert card_data["used_value"] == 20.0


def test_sidecar_pushed_card_merges_on_second_push(session):
    """Second ingest for the same key updates the existing row rather than inserting a new one."""
    base_card = {
        "provider_id": "chatgpt",
        "account_id": "gpt-user@example.com",
        "account_label": "gpt-user@example.com",
        "service_name": "ChatGPT",
        "window_type": "monthly",
        "variant": "default",
        "model_id": "",
        "icon": "💬",
        "remaining": "60%",
        "unit": "capacity",
        "reset": "monthly",
        "health": "good",
        "pace": "Stable",
        "detail": "Initial push",
        "used_value": 40.0,
        "limit_value": 100.0,
        "pct_used": 40.0,
        "unit_type": "percent",
        "data_source": "web",
    }
    updated_card = {**base_card, "used_value": 55.0, "pct_used": 55.0, "detail": "Updated push"}

    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)

        _ingest(
            client,
            {
                "provider": "chatgpt-sidecar",
                "sidecar_id": "host-x",
                "metrics": [base_card],
                "events": [],
            },
        )
        _ingest(
            client,
            {
                "provider": "chatgpt-sidecar",
                "sidecar_id": "host-x",
                "metrics": [updated_card],
                "events": [],
            },
        )

    rows = session.exec(
        select(LatestUsage).where(
            LatestUsage.provider_id == "chatgpt",
            LatestUsage.window_type == "monthly",
        )
    ).all()
    assert len(rows) == 1, f"Expected 1 row after two pushes, got {len(rows)}"
    card_data = json.loads(rows[0].card_json)
    # Merge should have the latest used_value
    assert card_data["used_value"] == 55.0
    assert card_data["detail"] == "Updated push"


def test_sidecar_pushed_card_skipped_without_provider_id(session):
    """Cards without provider_id should be skipped (not crash and not insert)."""
    payload = {
        "provider": "unknown-sidecar",
        "sidecar_id": "host-y",
        "metrics": [
            {
                "provider_id": None,
                "account_id": "some-user",
                "service_name": "Mystery",
                "window_type": "weekly",
                "icon": "?",
                "remaining": "50%",
                "unit": "capacity",
                "reset": "weekly",
                "health": "good",
                "pace": "Stable",
                "detail": "No provider",
                "data_source": "web",
            }
        ],
        "events": [],
    }

    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)
        resp = _ingest(client, payload)

    # The card had no provider_id — upsert_latest_usage skips it silently
    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 0


def test_two_sidecars_different_accounts_produce_separate_rows(session):
    """Two sidecars pushing cards for different accounts produce 2 LatestUsage rows.

    This is the DB-backed equivalent of the old external_metrics
    test_keeps_different_accounts_separate: the UNIQUE constraint on
    (provider_id, account_id, window_type, variant, model_id) ensures
    different account_ids never collapse into one row.
    """

    def _card(account_id: str) -> dict:
        return {
            "provider_id": "antigravity",
            "account_id": account_id,
            "account_label": account_id,
            "service_name": "claude-sonnet-4-5",
            "window_type": "session",
            "variant": "default",
            "model_id": "",
            "icon": "🛸",
            "remaining": "75%",
            "unit": "capacity",
            "reset": "Dynamic",
            "health": "good",
            "pace": "Continuous",
            "detail": "Pro | test [LSP]",
            "data_source": "lsp",
            "used_value": 25.0,
            "limit_value": 100.0,
            "pct_used": 25.0,
            "unit_type": "percent",
        }

    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)

        # Sidecar A pushes alice's card
        _ingest(
            client,
            {
                "provider": "antigravity-sidecar",
                "sidecar_id": "sidecar-a",
                "metrics": [_card("alice@example.com")],
                "events": [],
            },
        )
        # Sidecar B pushes bob's card
        _ingest(
            client,
            {
                "provider": "antigravity-sidecar",
                "sidecar_id": "sidecar-b",
                "metrics": [_card("bob@example.com")],
                "events": [],
            },
        )

    rows = session.exec(select(LatestUsage).where(LatestUsage.provider_id == "antigravity")).all()
    assert len(rows) == 2, f"Expected 2 rows (one per account), got {len(rows)}"
    account_ids = {r.account_id for r in rows}
    assert "alice@example.com" in account_ids
    assert "bob@example.com" in account_ids


def test_ingest_prunes_ghost_model_rows_with_past_reset_at(session):
    """A latest_usage row the sidecar stops reporting AND whose reset_at is in
    the past is pruned on the next push for the same (provider, account).
    Otherwise it lingers as a 'RESETS JUST NOW' ghost on the dashboard.
    """
    import datetime as _dt

    past = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=2)).isoformat()
    future = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=4)).isoformat()

    def _card(model_id: str, reset_at: str) -> dict:
        return {
            "provider_id": "antigravity",
            "account_id": "alice@example.com",
            "account_label": "alice@example.com",
            "service_name": model_id or "default",
            "window_type": "session",
            "variant": "default",
            "model_id": model_id,
            "icon": "🛸",
            "remaining": "60.0%",
            "unit": "capacity",
            "reset": "Dynamic",
            "health": "good",
            "pace": "Continuous",
            "detail": "Pro | alice [LSP]",
            "data_source": "local",
            "input_source": "sidecar",
            "used_value": 40.0,
            "limit_value": 100.0,
            "pct_used": 40.0,
            "unit_type": "percent",
            "reset_at": reset_at,
        }

    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)

        # First push: 3 models. Two will go stale (past reset_at), one stays current.
        _ingest(
            client,
            {
                "provider": "antigravity-sidecar",
                "sidecar_id": "sidecar-a",
                "metrics": [
                    _card("gemini-3-flash", past),
                    _card("gemini-3-5-flash-low", past),
                    _card("gemini-3-1-pro-high", future),
                ],
                "events": [],
            },
        )
        assert (
            len(
                session.exec(
                    select(LatestUsage).where(LatestUsage.provider_id == "antigravity")
                ).all()
            )
            == 3
        )

        # Second push: LSP now only returns the surviving model.
        _ingest(
            client,
            {
                "provider": "antigravity-sidecar",
                "sidecar_id": "sidecar-a",
                "metrics": [_card("gemini-3-1-pro-high", future)],
                "events": [],
            },
        )

    rows = session.exec(select(LatestUsage).where(LatestUsage.provider_id == "antigravity")).all()
    model_ids = {r.model_id for r in rows}
    assert model_ids == {"gemini-3-1-pro-high"}, f"Ghost rows not pruned: {model_ids}"


def test_ingest_does_not_prune_other_provider_rows(session):
    """The prune is scoped per (provider, account) — pushing one provider's
    cards must not delete another provider's rows even if their reset_at
    happens to be in the past."""
    import datetime as _dt

    past = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=2)).isoformat()
    future = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=4)).isoformat()

    def _card(provider_id: str, model_id: str, reset_at: str) -> dict:
        return {
            "provider_id": provider_id,
            "account_id": "alice@example.com",
            "account_label": "alice@example.com",
            "service_name": model_id or "default",
            "window_type": "session",
            "variant": "default",
            "model_id": model_id,
            "icon": "🛸",
            "remaining": "60.0%",
            "unit": "capacity",
            "reset": "Dynamic",
            "health": "good",
            "pace": "Continuous",
            "detail": "Pro | alice [LSP]",
            "data_source": "local",
            "input_source": "sidecar",
            "used_value": 40.0,
            "limit_value": 100.0,
            "pct_used": 40.0,
            "unit_type": "percent",
            "reset_at": reset_at,
        }

    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)

        # Seed a stale openai row for the same account.
        _ingest(
            client,
            {
                "provider": "openai-sidecar",
                "sidecar_id": "sidecar-a",
                "metrics": [_card("openai", "gpt-4", past)],
                "events": [],
            },
        )

        # Push fresh antigravity data — the openai stale row must survive.
        _ingest(
            client,
            {
                "provider": "antigravity-sidecar",
                "sidecar_id": "sidecar-a",
                "metrics": [_card("antigravity", "gemini", future)],
                "events": [],
            },
        )

    openai_rows = session.exec(select(LatestUsage).where(LatestUsage.provider_id == "openai")).all()
    assert len(openai_rows) == 1, "Cross-provider prune leaked"


def test_passive_provider_added_to_poll_providers(session):
    """A provider that exists in latest_usage but has no provider_configs row
    (the antigravity / opencode-free shape) must still show up in the server's
    poll_providers instruction — otherwise it's stuck refreshing only on
    cold-start or manual trigger.
    """
    from app.services.fleet_registry import fleet_registry

    sidecar_id = "sidecar-passive-test"
    # Make sure no prior test leaked cadence state for this id.
    fleet_registry._last_provider_polls.pop(sidecar_id, None)
    fleet_registry._pending_triggers.discard(sidecar_id)

    # Seed a latest_usage row for antigravity with no provider_configs row.
    session.add(
        LatestUsage(
            provider_id="antigravity",
            account_id="alice@example.com",
            sidecar_id=sidecar_id,
            window_type="session",
            variant="default",
            model_id="gemini-3-1-pro-high",
            card_json=json.dumps(
                {
                    "provider_id": "antigravity",
                    "account_id": "alice@example.com",
                    "window_type": "session",
                    "variant": "default",
                    "pct_used": 40.0,
                }
            ),
        )
    )
    session.commit()

    payload = {
        "provider": f"sidecar-{sidecar_id}",
        "sidecar_id": sidecar_id,
        "metrics": [],
        "events": [],
    }

    with (
        patch("app.api.endpoints.fleet.settings") as mock_settings,
        patch("app.api.endpoints.fleet.token_cache") as mock_tc,
    ):
        mock_settings.INGEST_API_KEY = TEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        mock_tc.store = AsyncMock()
        client = TestClient(app)
        resp = _ingest(client, payload)

    assert "antigravity" in resp["poll_providers"], (
        f"Passive provider missing from cadence: {resp['poll_providers']}"
    )
