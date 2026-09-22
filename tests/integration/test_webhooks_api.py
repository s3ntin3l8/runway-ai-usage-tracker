from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import ProviderConfig


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session):
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_list_webhooks_empty(client):
    response = client.get("/api/v1/system/webhooks")
    assert response.status_code == 200
    assert response.json() == {"webhooks": []}


def test_create_webhook(client):
    payload = {
        "provider_id": "anthropic",
        "threshold_pct": 90.0,
        "url": "https://discord.example.com/hook",
        "channel": "discord",
    }
    response = client.post("/api/v1/system/webhooks", json=payload)
    assert response.status_code == 201
    assert "id" in response.json()


def test_list_webhooks_after_create(client):
    payload = {
        "provider_id": "openai",
        "threshold_pct": 85.0,
        "url": "https://hooks.slack.com/example",
        "channel": "slack",
    }
    client.post("/api/v1/system/webhooks", json=payload)
    response = client.get("/api/v1/system/webhooks")
    webhooks = response.json()["webhooks"]
    assert len(webhooks) == 1
    assert webhooks[0]["provider_id"] == "openai"
    assert webhooks[0]["threshold_pct"] == 85.0


def test_patch_webhook(client):
    create_resp = client.post(
        "/api/v1/system/webhooks",
        json={
            "provider_id": "anthropic",
            "threshold_pct": 90.0,
            "url": "https://discord.example.com/hook",
            "channel": "discord",
        },
    )
    webhook_id = create_resp.json()["id"]

    patch_resp = client.patch(f"/api/v1/system/webhooks/{webhook_id}", json={"threshold_pct": 75.0})
    assert patch_resp.status_code == 200

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"][0]["threshold_pct"] == 75.0


def test_delete_webhook(client):
    create_resp = client.post(
        "/api/v1/system/webhooks",
        json={
            "provider_id": "anthropic",
            "threshold_pct": 90.0,
            "url": "https://discord.example.com/hook",
            "channel": "discord",
        },
    )
    webhook_id = create_resp.json()["id"]

    del_resp = client.delete(f"/api/v1/system/webhooks/{webhook_id}")
    assert del_resp.status_code == 204

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"] == []


def test_patch_nonexistent_webhook(client):
    response = client.patch("/api/v1/system/webhooks/9999", json={"threshold_pct": 50.0})
    assert response.status_code == 404


def test_delete_nonexistent_webhook(client):
    response = client.delete("/api/v1/system/webhooks/9999")
    assert response.status_code == 404


def test_test_endpoint_sends_payload(client):
    """Test endpoint fires a webhook and returns status=sent."""
    create_resp = client.post(
        "/api/v1/system/webhooks",
        json={
            "provider_id": "anthropic",
            "threshold_pct": 90.0,
            "url": "https://discord.example.com/hook",
            "channel": "discord",
        },
    )
    webhook_id = create_resp.json()["id"]

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        response = client.post(f"/api/v1/system/webhooks/{webhook_id}/test")

    assert response.status_code == 200
    assert response.json() == {"status": "sent"}


def test_test_endpoint_returns_502_on_delivery_failure(client):
    """Test endpoint returns 502 when webhook delivery fails."""
    create_resp = client.post(
        "/api/v1/system/webhooks",
        json={
            "provider_id": "anthropic",
            "threshold_pct": 90.0,
            "url": "https://discord.example.com/hook",
            "channel": "discord",
        },
    )
    webhook_id = create_resp.json()["id"]

    import httpx as _httpx

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=_httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        )
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        response = client.post(f"/api/v1/system/webhooks/{webhook_id}/test")

    assert response.status_code == 502


def test_test_endpoint_404_on_nonexistent(client):
    """Test endpoint returns 404 for unknown webhook id."""
    response = client.post("/api/v1/system/webhooks/9999/test")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Per-account scoping (#274)
# ---------------------------------------------------------------------------


def _seed_account(session, provider_id="anthropic", account_id="work@example.com"):
    session.add(ProviderConfig(provider_id=provider_id, account_id=account_id))
    session.commit()


def _payload(**overrides):
    payload = {
        "provider_id": "anthropic",
        "threshold_pct": 90.0,
        "url": "https://discord.example.com/hook",
        "channel": "discord",
    }
    payload.update(overrides)
    return payload


def test_create_webhook_with_account_id(client, session):
    _seed_account(session)
    response = client.post(
        "/api/v1/system/webhooks",
        json=_payload(account_id="work@example.com"),
    )
    assert response.status_code == 201

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"][0]["account_id"] == "work@example.com"


def test_list_webhook_without_account_is_null(client):
    response = client.post("/api/v1/system/webhooks", json=_payload())
    assert response.status_code == 201

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"][0]["account_id"] is None


def test_create_webhook_unknown_account_400(client):
    response = client.post(
        "/api/v1/system/webhooks",
        json=_payload(account_id="nobody@example.com"),
    )
    assert response.status_code == 400
    assert "provider_configs" in response.json()["detail"]


def test_create_webhook_wildcard_provider_with_account_400(client):
    response = client.post(
        "/api/v1/system/webhooks",
        json=_payload(provider_id="*", account_id="work@example.com"),
    )
    assert response.status_code == 400
    assert "account_id" in response.json()["detail"]


def test_create_duplicate_all_accounts_409(client):
    """NULL account duplicates are rejected by the API (SQLite NULLs are distinct)."""
    assert client.post("/api/v1/system/webhooks", json=_payload()).status_code == 201
    dup = client.post("/api/v1/system/webhooks", json=_payload())
    assert dup.status_code == 409


def test_create_duplicate_scoped_account_409(client, session):
    _seed_account(session)
    assert (
        client.post(
            "/api/v1/system/webhooks", json=_payload(account_id="work@example.com")
        ).status_code
        == 201
    )
    dup = client.post("/api/v1/system/webhooks", json=_payload(account_id="work@example.com"))
    assert dup.status_code == 409


def test_patch_set_and_clear_account_id(client, session):
    _seed_account(session)
    create_resp = client.post("/api/v1/system/webhooks", json=_payload())
    webhook_id = create_resp.json()["id"]

    # Scope to an account.
    patch_resp = client.patch(
        f"/api/v1/system/webhooks/{webhook_id}",
        json={"account_id": "work@example.com"},
    )
    assert patch_resp.status_code == 200
    assert (
        client.get("/api/v1/system/webhooks").json()["webhooks"][0]["account_id"]
        == "work@example.com"
    )

    # Explicit null clears back to "All accounts".
    patch_resp = client.patch(
        f"/api/v1/system/webhooks/{webhook_id}",
        json={"account_id": None},
    )
    assert patch_resp.status_code == 200
    assert client.get("/api/v1/system/webhooks").json()["webhooks"][0]["account_id"] is None


def test_patch_account_omitted_leaves_scope_untouched(client, session):
    _seed_account(session)
    create_resp = client.post(
        "/api/v1/system/webhooks", json=_payload(account_id="work@example.com")
    )
    webhook_id = create_resp.json()["id"]

    patch_resp = client.patch(f"/api/v1/system/webhooks/{webhook_id}", json={"threshold_pct": 75.0})
    assert patch_resp.status_code == 200
    webhooks = client.get("/api/v1/system/webhooks").json()["webhooks"]
    assert webhooks[0]["account_id"] == "work@example.com"
    assert webhooks[0]["threshold_pct"] == 75.0


def test_patch_unknown_account_400(client):
    create_resp = client.post("/api/v1/system/webhooks", json=_payload())
    webhook_id = create_resp.json()["id"]

    patch_resp = client.patch(
        f"/api/v1/system/webhooks/{webhook_id}",
        json={"account_id": "nobody@example.com"},
    )
    assert patch_resp.status_code == 400


def test_patch_wildcard_provider_with_account_400(client):
    create_resp = client.post("/api/v1/system/webhooks", json=_payload(provider_id="*"))
    assert create_resp.status_code == 201
    webhook_id = create_resp.json()["id"]

    patch_resp = client.patch(
        f"/api/v1/system/webhooks/{webhook_id}",
        json={"account_id": "work@example.com"},
    )
    assert patch_resp.status_code == 400
    assert "account_id" in patch_resp.json()["detail"]


def test_list_webhooks_filters_by_account_id(client, session):
    _seed_account(session)
    session.add(ProviderConfig(provider_id="anthropic", account_id="personal@example.com"))
    session.commit()

    assert (
        client.post(
            "/api/v1/system/webhooks", json=_payload(account_id="work@example.com")
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/system/webhooks", json=_payload(account_id="personal@example.com")
        ).status_code
        == 201
    )
    assert client.post("/api/v1/system/webhooks", json=_payload()).status_code == 201

    filtered = client.get("/api/v1/system/webhooks", params={"account_id": "work@example.com"})
    assert filtered.status_code == 200
    webhooks = filtered.json()["webhooks"]
    assert [w["account_id"] for w in webhooks] == ["work@example.com"]

    unfiltered = client.get("/api/v1/system/webhooks")
    assert len(unfiltered.json()["webhooks"]) == 3


# ---------------------------------------------------------------------------
# Upgrade-path guard
# ---------------------------------------------------------------------------


def test_deferred_columns_includes_webhook_account_id():
    """account_id column must be in _DEFERRED_COLUMNS for existing DB upgrades."""
    from app.core.db import _DEFERRED_COLUMNS

    assert ("webhook_configs", "account_id", "VARCHAR") in _DEFERRED_COLUMNS


def test_migrate_webhook_uniqueness_upgrades_legacy_db():
    """Functional upgrade path: dedupe keeps MIN(id), unique index created, idempotent."""
    from sqlalchemy import text

    from app.core.db import _add_columns_if_missing, _migrate_webhook_uniqueness

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with engine.connect() as conn:
        # Rebuild webhook_configs as a pre-#274 legacy table: no account_id,
        # no unique constraint, and two duplicate (provider, NULL, url) rows.
        conn.execute(text("DROP TABLE webhook_configs"))
        conn.execute(
            text(
                "CREATE TABLE webhook_configs ("
                "id INTEGER PRIMARY KEY, provider_id VARCHAR NOT NULL, "
                "threshold_pct FLOAT NOT NULL, url VARCHAR NOT NULL, "
                "channel VARCHAR NOT NULL, active BOOLEAN, last_fired_at TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO webhook_configs (id, provider_id, threshold_pct, url, channel, active) "
                "VALUES (1, 'anthropic', 90.0, 'https://discord.example.com/hook', 'discord', 1),"
                "(2, 'anthropic', 80.0, 'https://discord.example.com/hook', 'discord', 1),"
                "(3, 'openai', 75.0, 'https://hooks.slack.com/example', 'slack', 1)"
            )
        )
        conn.commit()

        _add_columns_if_missing(conn)
        _migrate_webhook_uniqueness(conn)

        # Oldest duplicate (id=1) survives; newer same-key row (id=2) dropped;
        # the distinct openai row (id=3) is untouched. account_id backfilled as NULL.
        rows = conn.execute(
            text("SELECT id, account_id FROM webhook_configs ORDER BY id")
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [(1, None), (3, None)]

        # Named unique index covers the three columns.
        index_cols = [
            r[2] for r in conn.execute(text("PRAGMA index_info('uq_webhook_provider_account_url')"))
        ]
        assert index_cols == ["provider_id", "account_id", "url"]

        # Second run is a no-op: same rows, still exactly one covering unique index.
        _add_columns_if_missing(conn)
        _migrate_webhook_uniqueness(conn)
        rows_again = conn.execute(
            text("SELECT id, account_id FROM webhook_configs ORDER BY id")
        ).fetchall()
        assert [(r[0], r[1]) for r in rows_again] == [(1, None), (3, None)]
        covering = []
        for row in conn.execute(text("PRAGMA index_list(webhook_configs)")):
            name, unique = row[1], row[2]
            if not unique:
                continue
            cols = {r[2] for r in conn.execute(text(f"PRAGMA index_info('{name}')"))}
            if {"provider_id", "account_id", "url"} <= cols:
                covering.append(name)
        assert covering == ["uq_webhook_provider_account_url"]


def test_fresh_db_has_single_covering_unique_index():
    """create_all already enforces uniqueness via the named UniqueConstraint;
    the migration must not add a second covering index on top of it."""
    from sqlalchemy import text

    from app.core.db import _add_columns_if_missing, _migrate_webhook_uniqueness

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with engine.connect() as conn:
        _add_columns_if_missing(conn)
        _migrate_webhook_uniqueness(conn)

        covering = []
        for row in conn.execute(text("PRAGMA index_list(webhook_configs)")):
            name, unique = row[1], row[2]
            if not unique:
                continue
            cols = {r[2] for r in conn.execute(text(f"PRAGMA index_info('{name}')"))}
            if {"provider_id", "account_id", "url"} <= cols:
                covering.append(name)
        assert len(covering) == 1
        # Fresh DBs are backed by sqlite_autoindex_* — no redundant named index.
        assert "uq_webhook_provider_account_url" not in covering

        # Uniqueness is actually enforced (concrete-account duplicate rejected).
        from sqlalchemy.exc import IntegrityError

        conn.execute(
            text(
                "INSERT INTO webhook_configs (provider_id, account_id, threshold_pct, url, channel, active) "
                "VALUES ('anthropic', 'work@x.com', 90.0, 'https://example.com/h', 'discord', 1)"
            )
        )
        conn.commit()
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            conn.execute(
                text(
                    "INSERT INTO webhook_configs (provider_id, account_id, threshold_pct, url, channel, active) "
                    "VALUES ('anthropic', 'work@x.com', 80.0, 'https://example.com/h', 'discord', 1)"
                )
            )
            conn.commit()
