"""Integration tests for the archive-providers feature.

Covers:
- Archived providers are filtered from GET /api/v1/usage/fleet
- GET /api/v1/usage/archived-providers returns lifetime stats
- PUT /api/v1/system/provider-config/{id} auto-disables on archive
- PUT re-enables on unarchive
- PUT via account-scoped endpoint for multi-account providers
"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import LatestUsage, ProviderConfig, UsageEvent
from tests.fixtures.test_client import TestClient

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

    def test_inactive_provider_hides_unmatched_stale_card_but_retains_row(self, session: Session):
        """A stale default card cannot warn after the provider's only account is archived."""
        stale = {
            "service_name": "Gemini",
            "provider_id": "gemini",
            "account_id": "default",
            "window_type": "daily",
            "variant": "default",
            "stale": True,
            "collection_failing": True,
        }
        session.add(
            LatestUsage(
                provider_id="gemini",
                account_id="default",
                sidecar_id="local",
                window_type="daily",
                variant="default",
                model_id="",
                card_json=json.dumps(stale),
            )
        )
        _add_provider_config(
            session, provider_id="gemini", account_id="old@example.com", archived=True
        )

        response = _client().get("/api/v1/usage/fleet")

        assert response.status_code == 200
        assert not any(entry["provider_id"] == "gemini" for entry in response.json()["fleet"])
        assert (
            session.exec(
                select(LatestUsage).where(
                    LatestUsage.provider_id == "gemini", LatestUsage.account_id == "default"
                )
            ).first()
            is not None
        )

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
            "/api/v1/system/provider-config/openrouter/default",
            json={"archived": True},
        )
        assert resp.status_code == 200, resp.text

        # Pin the PUT to the seeded row — account_id comes from the path now,
        # so changing the seed's account_id would create a fresh row instead.
        listing = _client().get("/api/v1/system/provider-configs").json()["providers"]
        openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
        assert openrouter["account_count"] == 0
        assert openrouter["archived_count"] == 1

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
            "/api/v1/system/provider-config/openrouter/default",
            json={"archived": False},
        )
        assert resp.status_code == 200, resp.text

        listing = _client().get("/api/v1/system/provider-configs").json()["providers"]
        openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
        assert openrouter["account_count"] == 1
        assert openrouter["archived_count"] == 0

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
            "/api/v1/system/provider-config/openrouter/default",
            json={"archived": True},
        )
        assert resp.status_code == 200, resp.text

        listing = _client().get("/api/v1/system/provider-configs").json()["providers"]
        openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
        assert openrouter["account_count"] == 0
        assert openrouter["archived_count"] == 1

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
            "/api/v1/system/provider-config/openrouter/default",
            json={"enabled": False, "archived": False},
        )
        assert resp.status_code == 200, resp.text

        listing = _client().get("/api/v1/system/provider-configs").json()["providers"]
        openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
        assert openrouter["account_count"] == 1

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
# Archiving evicts the retired account's cached credential (#891)
# ---------------------------------------------------------------------------

INGEST_SECRET = "test-sidecar-secret-for-archive"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _isolated_ingest_key(monkeypatch):
    """Pin INGEST_API_KEY — the sidecar push in the restore test needs it.

    The dotted-path form (not the bound ``settings`` import) survives a
    ``importlib.reload(app.core.config)`` from ``tests/unit/test_config.py``.
    Same shape as the fixture in ``test_fleet_credentials.py``.
    """
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", INGEST_SECRET)
    yield


@pytest.fixture
def clean_token_cache():
    """Give the test a clean cache, and leave none behind.

    The token cache is a process-wide singleton while the test's database is
    per test, so a seeded credential would otherwise leak into the next test.
    ``reset()`` is async, and awaiting it from the test body would bind the
    cache's lock to that throwaway loop — the same trap the DELETE test
    documents — so the dicts are cleared directly.
    """
    from app.services.token_cache import token_cache

    def _clear() -> None:
        token_cache._cache.clear()
        token_cache._token_timestamps.clear()
        token_cache._source_cache.clear()

    _clear()
    yield token_cache
    _clear()


def _seed_source_bundle(token_cache, provider: str, account_id: str, source_id: str) -> None:
    """Seed a source bundle without touching the cache's asyncio lock.

    ``seed_sync`` writes the merged account entry only; a source bundle is
    what the collectors read, and ``remove()`` drops both. The layout is the
    one ``store()`` uses: ``(tokens, metadata, last_seen)``.
    """
    accounts = token_cache._source_cache.setdefault(provider, {}).setdefault(account_id, {})
    accounts[source_id] = (
        {"api_key": "sk-or-test-alice"},  # pragma: allowlist secret
        {"source": "sidecar"},
        time.time(),
    )


def _signed_ingest(client: TestClient, sidecar_id: str, cards: list[dict]):
    """POST one sidecar heartbeat, signed the way the server expects."""
    body = json.dumps({"provider": "sidecar", "sidecar_id": sidecar_id, "metrics": cards}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(INGEST_SECRET.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/fleet/ingest",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
    )


def _token_card(provider: str, account_id: str, api_key: str, origin: str) -> dict:
    """A token-only card — the shape a sidecar reports a credential in."""
    return {
        "service_name": provider,
        "remaining": "Token",
        "unit": "api_key",
        "provider_id": provider,
        "account_id": account_id,
        "metadata": {
            "provider_id": provider,
            "api_key": api_key,
            "credential_origin": origin,
            "account_id": account_id,
        },
    }


class TestArchiveEvictsTheCachedCredential:
    """An archived account must not keep a live credential (#891).

    The Remove path has always evicted the account's cache entry; the archive
    write path did not, so a retired account kept a bundle that only reads
    "expired" and stays refreshable.
    """

    def test_archiving_an_account_evicts_its_cached_credential(
        self, session: Session, clean_token_cache
    ):
        _add_provider_config(
            session, provider_id="openrouter", account_id="alice@example.com", enabled=True
        )
        clean_token_cache.seed_sync(
            "openrouter",
            "alice@example.com",
            {"api_key": "sk-or-test-alice"},  # pragma: allowlist secret
            {"account_label": "Alice", "source": "sidecar"},
            time.time(),
        )
        _seed_source_bundle(clean_token_cache, "openrouter", "alice@example.com", "sidecar:dev-01")
        # Another account's credential must survive the eviction.
        clean_token_cache.seed_sync(
            "openrouter",
            "bob@example.com",
            {"api_key": "sk-or-test-bob"},  # pragma: allowlist secret
            {"account_label": "Bob", "source": "sidecar"},
            time.time(),
        )

        resp = _client().put(
            "/api/v1/system/provider-config/openrouter/alice%40example.com",
            json={"archived": True},
        )
        assert resp.status_code == 200, resp.text

        # The retired account holds neither the merged entry nor its bundles.
        assert clean_token_cache._cache.get("openrouter", {}).get("alice@example.com") is None
        assert (
            clean_token_cache._source_cache.get("openrouter", {}).get("alice@example.com") is None
        )
        # Another account's credential is untouched.
        assert clean_token_cache._cache.get("openrouter", {}).get("bob@example.com") is not None

    def test_archiving_an_account_evicts_a_durable_only_bundle(
        self, session: Session, clean_token_cache
    ):
        """A bundle without a merged entry must go too.

        An enrolled machine's credential is durable: ``store()`` writes it to
        the source cache only, so the account has no merged entry at all.
        Removing just the merged entry left the bundle behind.
        """
        _add_provider_config(
            session, provider_id="openrouter", account_id="alice@example.com", enabled=True
        )
        _seed_source_bundle(clean_token_cache, "openrouter", "alice@example.com", "sidecar:dev-01")
        assert "alice@example.com" in clean_token_cache._source_cache.get("openrouter", {})

        resp = _client().put(
            "/api/v1/system/provider-config/openrouter/alice%40example.com",
            json={"archived": True},
        )
        assert resp.status_code == 200, resp.text

        assert (
            clean_token_cache._source_cache.get("openrouter", {}).get("alice@example.com") is None
        )

    def test_archiving_an_account_that_also_stores_a_key_leaves_nothing_cached(
        self, session: Session, clean_token_cache
    ):
        """A request that archives *and* stores a key must not leave the
        credential it just archived in the cache.

        Storing a key mirrors it into the token cache, so the eviction has to
        run after that mirroring — otherwise the account ends up archived *and*
        holding the credential it was just retired with.
        """
        _add_provider_config(
            session, provider_id="gemini", account_id="alice@example.com", enabled=True
        )

        resp = _client().put(
            "/api/v1/system/provider-config/gemini/alice%40example.com",
            json={"archived": True, "api_key": "test-gemini-key"},  # pragma: allowlist secret
        )
        assert resp.status_code == 200, resp.text

        assert clean_token_cache._cache.get("gemini", {}).get("alice@example.com") is None
        assert clean_token_cache._source_cache.get("gemini", {}).get("alice@example.com") is None

    def test_archiving_an_account_keeps_its_stored_credential(
        self, session: Session, clean_token_cache
    ):
        """Archive is not Remove: the stored credential survives, so a restore
        is usable immediately."""
        row = _add_provider_config(
            session, provider_id="openrouter", account_id="alice@example.com", enabled=True
        )
        row.api_key = "sk-or-test-alice"  # pragma: allowlist secret
        session.add(row)
        session.commit()

        resp = _client().put(
            "/api/v1/system/provider-config/openrouter/alice%40example.com",
            json={"archived": True},
        )
        assert resp.status_code == 200, resp.text

        stored = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "openrouter",
                ProviderConfig.account_id == "alice@example.com",
            )
        ).first()
        assert stored is not None
        assert stored.archived is True
        assert stored.api_key is not None

    def test_restoring_an_account_lets_the_next_sidecar_push_bring_the_credential_back(
        self, session: Session, clean_token_cache
    ):
        """The machine still has the credential file: after a restore, its
        next push puts the credential back."""
        _add_provider_config(
            session, provider_id="openrouter", account_id="alice@example.com", enabled=True
        )
        client = _client()

        archived = client.put(
            "/api/v1/system/provider-config/openrouter/alice%40example.com",
            json={"archived": True},
        )
        assert archived.status_code == 200, archived.text
        assert clean_token_cache._cache.get("openrouter", {}).get("alice@example.com") is None

        restored = client.put(
            "/api/v1/system/provider-config/openrouter/alice%40example.com",
            json={"archived": False},
        )
        assert restored.status_code == 200, restored.text

        pushed = _signed_ingest(
            client,
            "dev-01",
            [
                _token_card(
                    "openrouter",
                    "alice@example.com",
                    "sk-or-test-alice",  # pragma: allowlist secret
                    "path:/home/u/.opencode/auth.json",
                )
            ],
        )
        assert pushed.status_code == 200, pushed.text

        assert clean_token_cache._cache.get("openrouter", {}).get("alice@example.com") is not None

    def test_merging_accounts_evicts_the_source_accounts_cached_credential(
        self, session: Session, clean_token_cache
    ):
        """The merge retires the source account too — it must not keep a live
        cached credential either."""
        _add_provider_config(
            session, provider_id="openrouter", account_id="alice@example.com", enabled=True
        )
        _add_provider_config(
            session, provider_id="openrouter", account_id="bob@example.com", enabled=True
        )
        clean_token_cache.seed_sync(
            "openrouter",
            "alice@example.com",
            {"api_key": "sk-or-test-alice"},  # pragma: allowlist secret
            {"account_label": "Alice", "source": "sidecar"},
            time.time(),
        )
        clean_token_cache.seed_sync(
            "openrouter",
            "bob@example.com",
            {"api_key": "sk-or-test-bob"},  # pragma: allowlist secret
            {"account_label": "Bob", "source": "sidecar"},
            time.time(),
        )

        resp = _client().post(
            "/api/v1/system/provider-account-merge/apply",
            json={
                "provider_id": "openrouter",
                "source_account_id": "alice@example.com",
                "destination_account_id": "bob@example.com",
            },
        )
        assert resp.status_code == 200, resp.text

        # The retired source holds nothing; the destination keeps its own.
        assert clean_token_cache._cache.get("openrouter", {}).get("alice@example.com") is None
        assert clean_token_cache._cache.get("openrouter", {}).get("bob@example.com") is not None

        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "openrouter",
                ProviderConfig.account_id == "alice@example.com",
            )
        ).first()
        assert row is not None
        assert row.archived is True


# ---------------------------------------------------------------------------
# Upgrade-path guard
# ---------------------------------------------------------------------------


def test_deferred_columns_includes_archived():
    """archived column must be in _DEFERRED_COLUMNS for existing DB upgrades."""
    from app.core.db import _DEFERRED_COLUMNS

    assert ("provider_configs", "archived", "BOOLEAN NOT NULL DEFAULT 0") in _DEFERRED_COLUMNS
