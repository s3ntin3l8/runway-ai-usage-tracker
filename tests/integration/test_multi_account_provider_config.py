"""Multi-account hardening tests for the provider config endpoints.

The webapp and dashboard continue to use the existing
``PUT /api/v1/system/provider-config/{provider_id}`` route; the new
``PUT /api/v1/system/provider-config/{provider_id}/{account_id}`` is the
multi-account canonical. These tests pin the contract of both routes plus
the GET response's new ``accounts`` field.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.core.db import get_session
from app.main import app


@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _admin_headers() -> dict[str, str]:
    """Set the X-Admin-Key the PUT endpoint requires."""
    from app.core.config import settings

    if settings.ADMIN_API_KEY:
        return {"X-Admin-Key": settings.ADMIN_API_KEY}
    return {}


def test_get_provider_configs_empty_accounts_field(client: TestClient):
    """No config rows: each provider's `accounts` list is empty + account_count=0."""
    r = client.get("/api/v1/system/provider-configs")
    assert r.status_code == 200
    providers = r.json()["providers"]
    assert providers  # registry has at least one entry
    for p in providers:
        assert p["accounts"] == []
        assert p["account_count"] == 0


def test_legacy_put_creates_default_row(client: TestClient):
    """Legacy PUT with no existing rows creates a row at account_id='default'."""
    r = client.put(
        "/api/v1/system/provider-config/openrouter",
        json={"account_label": "default test"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    assert openrouter["accounts"][0]["account_id"] == "default"
    assert openrouter["accounts"][0]["account_label"] == "default test"


def test_legacy_put_updates_only_row(client: TestClient):
    """Single existing row → legacy PUT updates it in place."""
    r = client.put(
        "/api/v1/system/provider-config/openrouter",
        json={"account_label": "v1"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    r = client.put(
        "/api/v1/system/provider-config/openrouter",
        json={"account_label": "v2"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    assert openrouter["accounts"][0]["account_label"] == "v2"


def test_legacy_put_returns_409_when_multi_account(client: TestClient):
    """Two existing rows → legacy PUT returns 409 pointing at the per-account route."""
    # Create two distinct accounts via the new explicit endpoint.
    a = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"account_label": "default"},
        headers=_admin_headers(),
    )
    assert a.status_code == 200
    b = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "alice"},
        headers=_admin_headers(),
    )
    assert b.status_code == 200

    # Legacy shortcut now refuses to guess.
    r = client.put(
        "/api/v1/system/provider-config/openrouter",
        json={"account_label": "v3"},
        headers=_admin_headers(),
    )
    assert r.status_code == 409
    body = r.json()
    assert "openrouter" in body["detail"]
    assert "account_id" in body["detail"]


def test_explicit_put_creates_account_id(client: TestClient):
    """The per-account endpoint creates the row at the supplied account_id."""
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["account_id"] == "alice@example.com"

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    assert openrouter["accounts"][0]["account_id"] == "alice@example.com"


def test_explicit_put_creates_second_account_for_same_provider(client: TestClient):
    """A second per-account PUT adds a second row, doesn't overwrite."""
    a = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    b = client.put(
        "/api/v1/system/provider-config/openrouter/bob@example.com",
        json={"account_label": "Bob"},
        headers=_admin_headers(),
    )
    assert a.status_code == 200
    assert b.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 2
    account_ids = {row["account_id"] for row in openrouter["accounts"]}
    assert account_ids == {"alice@example.com", "bob@example.com"}


def test_explicit_put_preserves_canonical_field_derivation(client: TestClient):
    """The top-level (legacy) fields come from the 'default' row when present,
    or the first row when no 'default' row exists."""
    # Two accounts, neither at 'default'.
    client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    client.put(
        "/api/v1/system/provider-config/openrouter/bob@example.com",
        json={"account_label": "Bob"},
        headers=_admin_headers(),
    )

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")

    # account_count = 2
    assert openrouter["account_count"] == 2
    # The legacy top-level account_label comes from the FIRST row (no 'default'
    # row to anchor on). The webapp is expected to migrate to consuming the
    # `accounts` field; this assertion only pins the canonical-row rule.
    canonical_account_ids = [r["account_id"] for r in openrouter["accounts"]]
    assert openrouter["account_label"] == {"alice@example.com": "Alice"}.get(
        canonical_account_ids[0]
    )


def test_explicit_put_unknown_provider_returns_404(client: TestClient):
    r = client.put(
        "/api/v1/system/provider-config/no-such-provider/alice@example.com",
        json={"account_label": "x"},
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert "no-such-provider" in r.json()["detail"]


def test_legacy_put_unknown_provider_returns_404(client: TestClient):
    r = client.put(
        "/api/v1/system/provider-config/no-such-provider",
        json={"account_label": "x"},
        headers=_admin_headers(),
    )
    assert r.status_code == 404


def test_explicit_put_with_default_account_id_matches_legacy_first_save(client: TestClient):
    """Saving via PUT /provider-config/{pid}/default produces the same row
    that the legacy shortcut would have created on first save."""
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"account_label": "via explicit"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    assert openrouter["accounts"][0]["account_id"] == "default"
    assert openrouter["accounts"][0]["account_label"] == "via explicit"
