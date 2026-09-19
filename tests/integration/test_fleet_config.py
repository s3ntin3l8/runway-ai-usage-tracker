"""Integration tests for the sidecar ``/fleet/config`` endpoint.

The endpoint returns per-provider collection config. It carries two parallel
shapes: the legacy OR-merged top-level ``enabled``/``strategies`` plus a new
``accounts: [...]`` array for per-account sidecars (Issue #272).

Pins the byte-identical behavior for the single-account case plus the
multi-account invariants the hardening introduced.
"""

from __future__ import annotations

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
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _add(session: Session, provider_id: str, account_id: str = "default", **kwargs):
    enabled = kwargs.pop("enabled", True)
    row = ProviderConfig(provider_id=provider_id, account_id=account_id, enabled=enabled, **kwargs)
    session.add(row)
    session.commit()
    return row


def test_config_no_rows_carries_accounts_empty_list(client: TestClient):
    """No provider_configs rows: every provider has an empty `accounts` list."""
    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    providers = r.json()["config"]["providers"]
    assert providers
    for entry in providers.values():
        assert entry["accounts"] == []


def test_config_single_row_seeds_strategies_from_that_row_even_if_disabled(
    client: TestClient, session: Session
):
    """Multi-account hardening corner case: the FIRST row's `strategies` are
    seeded to the legacy top-level field regardless of `enabled`. Pre-fix
    PR left a hole where a single-disabled-row-with-strategies would surface
    `strategies: null`; this test pins the original byte-identical behavior."""
    _add(
        session,
        "openrouter",
        "default",
        enabled=False,
        collection_strategies_json='[{"id":"web","enabled":true}]',
    )
    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    openrouter = r.json()["config"]["providers"]["openrouter"]
    # Legacy top-level: strategies seeded from the first (disabled) row.
    assert openrouter["strategies"] == [{"id": "web", "enabled": True}]
    # Per-account entry preserves enabled=False verbatim.
    assert openrouter["accounts"][0]["enabled"] is False
    assert openrouter["accounts"][0]["account_id"] == "default"


def test_config_multi_row_or_merges_enabled_and_last_writer_wins_strategies(
    client: TestClient, session: Session
):
    """Two enabled rows with different strategies: top-level is OR-merged +
    last-writer-wins for strategies; the new accounts[] is the full
    per-account breakdown. Sidecars see the legacy shape; future per-account
    sidecars iterate accounts[]."""
    _add(
        session,
        "openrouter",
        "default",
        collection_strategies_json='[{"id":"api","enabled":true}]',
    )
    _add(
        session,
        "openrouter",
        "alice@example.com",
        collection_strategies_json='[{"id":"web","enabled":true}]',
    )
    r = client.get("/api/v1/fleet/config")
    openrouter = r.json()["config"]["providers"]["openrouter"]

    # OR-merge enabled
    assert openrouter["enabled"] is True

    # First row's strategies seeded (per the disabled-row review fix)
    # — last-writer-wins for subsequent enabled rows overrides only when
    # the row is enabled.
    # Here both rows are enabled; the second row's "web" wins.
    assert openrouter["strategies"] == [{"id": "web", "enabled": True}]

    # accounts[] exposes both
    account_ids = {a["account_id"] for a in openrouter["accounts"]}
    assert account_ids == {"default", "alice@example.com"}


def test_config_disabled_first_row_keeps_first_strategies(client: TestClient, session: Session):
    """Specifically the byte-identical invariant from the review: when the
    first row is disabled but has strategies, the legacy top-level keeps
    those strategies (later enabled rows may overwrite)."""
    _add(
        session,
        "openrouter",
        "default",
        enabled=False,
        collection_strategies_json='[{"id":"web","label":"Web"}]',
    )
    _add(
        session,
        "openrouter",
        "work@example.com",
        enabled=True,
        # No strategies on the enabled row — should NOT clobber first row's.
    )
    r = client.get("/api/v1/fleet/config")
    openrouter = r.json()["config"]["providers"]["openrouter"]
    assert openrouter["strategies"] == [{"id": "web", "label": "Web"}]
    assert openrouter["enabled"] is True  # OR-merge from second row
