"""Integration tests for the archive-providers feature.

Covers:
- Archived providers are filtered from GET /api/v1/usage/fleet
- GET /api/v1/usage/archived-providers returns lifetime stats
- PUT /api/v1/system/provider-config/{id} auto-disables on archive
- PUT re-enables on unarchive
- PUT via account-scoped endpoint for multi-account providers
"""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import LatestUsage, ProviderConfig, UsageEvent

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

NOW = datetime.now(UTC)


def _seed_card(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    window_type: str = "monthly",
    variant: str = "default",
    pct_used: float | None = None,
) -> None:
    card = {
        "service_name": f"{provider_id}-{window_type}",
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
            model_id="",
            card_json=json.dumps(card),
        )
    )
    session.commit()


def _seed_event(
    session: Session,
    event_id: str,
    *,
    provider_id: str = "anthropic",
    account_id: str = "u@x.com",
    ts: datetime = NOW,
    tokens_input: int = 100,
    tokens_output: int = 200,
    tokens_cache_read: int = 10,
    tokens_cache_create: int = 5,
    tokens_reasoning: int = 3,
    cost_usd: float = 0.01,
) -> None:
    session.add(
        UsageEvent(
            provider_id=provider_id,
            account_id=account_id,
            sidecar_id="dev-01",
            event_id=event_id,
            ts=ts,
            kind="message",
            model_id="sonnet",
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_cache_read=tokens_cache_read,
            tokens_cache_create=tokens_cache_create,
            tokens_reasoning=tokens_reasoning,
            cost_usd=cost_usd,
        )
    )
    session.commit()


def _add_provider_config(
    session: Session,
    *,
    provider_id: str,
    account_id: str = "default",
    enabled: bool = True,
    archived: bool = False,
) -> ProviderConfig:
    row = ProviderConfig(
        provider_id=provider_id,
        account_id=account_id,
        enabled=enabled,
        archived=archived,
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Fleet filter tests
# ---------------------------------------------------------------------------


class TestFleetFilterArchived:
    """Archived providers must not appear in the fleet view."""

    def test_archived_card_excluded_from_fleet(self, session: Session):
        _seed_card(session, provider_id="anthropic", account_id="u@x.com", pct_used=0.5)
        _add_provider_config(session, provider_id="anthropic", account_id="u@x.com", archived=True)

        resp = _client().get("/api/v1/usage/fleet")
        assert resp.status_code == 200
        fleet = resp.json()["fleet"]
        provider_ids = [e["provider_id"] for e in fleet]
        assert "anthropic" not in provider_ids

    def test_non_archived_card_still_in_fleet(self, session: Session):
        _seed_card(session, provider_id="anthropic", account_id="u@x.com", pct_used=0.5)
        _add_provider_config(session, provider_id="anthropic", account_id="u@x.com", archived=False)

        resp = _client().get("/api/v1/usage/fleet")
        assert resp.status_code == 200
        fleet = resp.json()["fleet"]
        provider_ids = [e["provider_id"] for e in fleet]
        assert "anthropic" in provider_ids

    def test_archived_synthetic_entry_excluded(self, session: Session):
        """A passive provider (events but no card) that is archived gets no synthetic entry."""
        _seed_event(session, "evt-1", provider_id="ollama", account_id="local")
        _add_provider_config(session, provider_id="ollama", account_id="local", archived=True)

        resp = _client().get("/api/v1/usage/fleet")
        assert resp.status_code == 200
        fleet = resp.json()["fleet"]
        provider_ids = [e["provider_id"] for e in fleet]
        assert "ollama" not in provider_ids

    def test_mixed_archived_and_active(self, session: Session):
        _seed_card(session, provider_id="anthropic", account_id="a", pct_used=0.3)
        _seed_card(session, provider_id="gemini", account_id="b", pct_used=0.7)
        _add_provider_config(session, provider_id="anthropic", account_id="a", archived=True)
        _add_provider_config(session, provider_id="gemini", account_id="b", archived=False)

        resp = _client().get("/api/v1/usage/fleet")
        assert resp.status_code == 200
        fleet = resp.json()["fleet"]
        provider_ids = [e["provider_id"] for e in fleet]
        assert "anthropic" not in provider_ids
        assert "gemini" in provider_ids


# ---------------------------------------------------------------------------
# Archived-providers endpoint tests
# ---------------------------------------------------------------------------


class TestArchivedProvidersEndpoint:
    """GET /api/v1/usage/archived-providers returns lifetime stats."""

    def test_empty_when_no_archived(self, session: Session):
        resp = _client().get("/api/v1/usage/archived-providers")
        assert resp.status_code == 200
        assert resp.json()["archived"] == []

    def test_returns_lifetime_stats(self, session: Session):
        _add_provider_config(session, provider_id="anthropic", account_id="u@x.com", archived=True)
        _seed_event(
            session,
            "evt-1",
            provider_id="anthropic",
            account_id="u@x.com",
            tokens_input=100,
            tokens_output=200,
            tokens_cache_read=10,
            tokens_cache_create=5,
            tokens_reasoning=3,
            cost_usd=0.05,
        )
        _seed_event(
            session,
            "evt-2",
            provider_id="anthropic",
            account_id="u@x.com",
            tokens_input=50,
            tokens_output=80,
            tokens_cache_read=5,
            tokens_cache_create=2,
            tokens_reasoning=1,
            cost_usd=0.02,
        )

        resp = _client().get("/api/v1/usage/archived-providers")
        assert resp.status_code == 200
        archived = resp.json()["archived"]
        assert len(archived) == 1
        item = archived[0]
        assert item["provider_id"] == "anthropic"
        assert item["account_id"] == "u@x.com"
        life = item["lifetime"]
        assert life is not None
        assert life["tokens_input"] == 150
        assert life["tokens_output"] == 280
        assert life["tokens_cache_read"] == 15
        assert life["tokens_cache_create"] == 7
        assert life["tokens_reasoning"] == 4
        assert life["msgs"] == 2
        assert life["cost_usd"] == pytest.approx(0.07)

    def test_last_activity_timestamp(self, session: Session):
        _add_provider_config(session, provider_id="anthropic", account_id="u@x.com", archived=True)
        ts = datetime(2026, 3, 15, 10, 0, 0, tzinfo=UTC)
        _seed_event(session, "evt-1", provider_id="anthropic", account_id="u@x.com", ts=ts)

        resp = _client().get("/api/v1/usage/archived-providers")
        assert resp.status_code == 200
        item = resp.json()["archived"][0]
        # SQLite may drop the tz offset, so compare the datetime without tz.
        assert item["last_activity_ts"] is not None
        returned = datetime.fromisoformat(item["last_activity_ts"])
        assert returned.replace(tzinfo=None) == ts.replace(tzinfo=None)

    def test_multiple_archived_providers(self, session: Session):
        _add_provider_config(session, provider_id="anthropic", account_id="a", archived=True)
        _add_provider_config(session, provider_id="gemini", account_id="b", archived=True)
        _seed_event(session, "evt-1", provider_id="anthropic", account_id="a")
        _seed_event(session, "evt-2", provider_id="gemini", account_id="b")

        resp = _client().get("/api/v1/usage/archived-providers")
        assert resp.status_code == 200
        pids = {i["provider_id"] for i in resp.json()["archived"]}
        assert pids == {"anthropic", "gemini"}

    def test_active_providers_not_in_response(self, session: Session):
        _add_provider_config(session, provider_id="anthropic", account_id="a", archived=False)
        _seed_event(session, "evt-1", provider_id="anthropic", account_id="a")

        resp = _client().get("/api/v1/usage/archived-providers")
        assert resp.status_code == 200
        assert resp.json()["archived"] == []


# ---------------------------------------------------------------------------
# Archive/unarchive side effects
# ---------------------------------------------------------------------------


class TestArchiveSideEffects:
    """Archiving must disable collection; unarchiving must re-enable it."""

    def test_archive_disables_collection(self, session: Session):
        _add_provider_config(session, provider_id="openrouter", account_id="default", enabled=True)

        resp = _client().put(
            "/api/v1/system/provider-config/openrouter",
            json={"archived": True},
        )
        assert resp.status_code == 200, resp.text

        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "openrouter",
                ProviderConfig.account_id == "default",
            )
        ).first()
        assert row is not None
        assert row.archived is True
        assert row.enabled is False

    def test_unarchive_reenables_collection(self, session: Session):
        _add_provider_config(
            session,
            provider_id="openrouter",
            account_id="default",
            enabled=False,
            archived=True,
        )

        resp = _client().put(
            "/api/v1/system/provider-config/openrouter",
            json={"archived": False},
        )
        assert resp.status_code == 200, resp.text

        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "openrouter",
                ProviderConfig.account_id == "default",
            )
        ).first()
        assert row is not None
        assert row.archived is False
        assert row.enabled is True

    def test_archive_preserves_already_disabled(self, session: Session):
        """If a provider was already disabled, archiving keeps it disabled."""
        _add_provider_config(
            session,
            provider_id="openrouter",
            account_id="default",
            enabled=False,
            archived=False,
        )

        resp = _client().put(
            "/api/v1/system/provider-config/openrouter",
            json={"archived": True},
        )
        assert resp.status_code == 200, resp.text

        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "openrouter",
                ProviderConfig.account_id == "default",
            )
        ).first()
        assert row is not None
        assert row.enabled is False

    def test_explicit_disable_not_overridden_by_unarchive(self, session: Session):
        """Settings sends {enabled: false, archived: false} — disable must stick."""
        _add_provider_config(
            session,
            provider_id="openrouter",
            account_id="default",
            enabled=True,
            archived=True,
        )

        resp = _client().put(
            "/api/v1/system/provider-config/openrouter",
            json={"enabled": False, "archived": False},
        )
        assert resp.status_code == 200, resp.text

        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "openrouter",
                ProviderConfig.account_id == "default",
            )
        ).first()
        assert row is not None
        assert row.archived is False
        assert row.enabled is False  # explicit disable wins over auto-re-enable


# ---------------------------------------------------------------------------
# Multi-account archive via account-scoped endpoint
# ---------------------------------------------------------------------------


class TestMultiAccountArchive:
    """Archive/restore via PUT /provider-config/{id}/{account_id}."""

    def test_account_scoped_archive(self, session: Session):
        _add_provider_config(session, provider_id="anthropic", account_id="a@x.com", enabled=True)
        _add_provider_config(session, provider_id="anthropic", account_id="b@x.com", enabled=True)

        resp = _client().put(
            "/api/v1/system/provider-config/anthropic/a%40x.com",
            json={"archived": True},
        )
        assert resp.status_code == 200, resp.text

        row_a = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "anthropic",
                ProviderConfig.account_id == "a@x.com",
            )
        ).first()
        row_b = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "anthropic",
                ProviderConfig.account_id == "b@x.com",
            )
        ).first()
        assert row_a is not None and row_a.archived is True and row_a.enabled is False
        assert row_b is not None and row_b.archived is False and row_b.enabled is True


# ---------------------------------------------------------------------------
# Upgrade-path guard
# ---------------------------------------------------------------------------


def test_deferred_columns_includes_archived():
    """archived column must be in _DEFERRED_COLUMNS for existing DB upgrades."""
    from app.core.db import _DEFERRED_COLUMNS

    assert ("provider_configs", "archived", "BOOLEAN NOT NULL DEFAULT 0") in _DEFERRED_COLUMNS
