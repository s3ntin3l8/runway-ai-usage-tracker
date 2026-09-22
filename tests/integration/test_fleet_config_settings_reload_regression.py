"""Regression guard for PR #297 (issue #291).

The PR shipped two fixes that are load-bearing together:

1. **Test-side**: dotted-path patching
   (``monkeypatch.setattr("app.core.config.settings.X", v)``)
   instead of bound-name patching
   (``monkeypatch.setattr(settings, "X", v)`` where ``settings``
   is bound at module top). Reload-proof because pytest
   re-resolves the attribute name on every lookup.

2. **API-side**: ``/fleet/config`` switched from a module-level
   ``from app.core.config import settings`` (which holds a stale
   snapshot after ``importlib.reload(app.core.config)``) to a
   late ``from app.core.config import settings as _settings`` at
   call time (which always reads the LIVE module).

Hermes's round-1 review flagged that this guard is **invisible to CI**:
the bug only surfaces under unit-first collection ordering, which
CI doesn't run. A single test in this file proves the late-import
fix is actually in place — under any collection order, with an
explicit ``importlib.reload`` to force the snapshot-bind branch.

This file lives in its own module (no autouse ``_isolated_ingest_key``
fixture, unlike ``test_fleet_credentials.py``) so the regression
manifests cleanly.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import ProviderConfig

REGRESSION_KEY = "regression-guard-pr297-key"


@pytest.fixture(name="session")
def session_fixture():
    """Fresh in-memory DB so the test provider-config row doesn't leak."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture(name="client")
def client_fixture(session):
    return TestClient(app)


def _add_provider_config(
    session: Session,
    *,
    provider_id: str,
    account_id: str = "default",
    api_key: str = "k",
) -> None:
    """Mirror of ``_add_provider_config`` in test_fleet_credentials.py."""
    cfg = ProviderConfig(
        provider_id=provider_id,
        account_id=account_id,
        api_key_encrypted=api_key,
        session_cookie_encrypted=None,
        oai_sc_cookie_encrypted=None,
        enabled=True,
    )
    session.add(cfg)
    session.commit()


def test_fleet_config_issues_token_after_explicit_settings_reload(monkeypatch, client, session):
    """Regression guard for the PR #297 late-import fix.

    1. Pin ``INGEST_API_KEY`` via dotted-path (reload-proof pattern).
    2. Force ``importlib.reload(app.core.config)`` mid-test to
       snapshot-bind any consumer that does
       ``from app.core.config import settings`` at module top.
    3. Re-pin via dotted-path (lands on the LIVE module post-reload).
    4. Assert ``/fleet/config`` issues a ``credential_token``.

    If anyone reverts the late-import fix in
    ``app/api/endpoints/fleet.py``'s ``/fleet/config`` handler, the
    endpoint reads the bound snapshot (which has ``INGEST_API_KEY = ""``
    after the reload because ``importlib.reload`` re-instantiates
    ``settings`` with default values), and ``can_issue_tokens`` is
    False — no ``credential_token`` in the response. This test fails
    under any collection order, so the regression is observable in
    default CI ordering too (PR #297 round-1 Hermes body-level
    suggestion).
    """
    # Step 1: snapshot-bind any consumer that did
    # ``from app.core.config import settings`` at module top by
    # reloading the module BEFORE patching. ``importlib.reload``
    # re-uses the module object but re-instantiates its ``settings``
    # attribute, so a future endpoint that does top-of-file
    # ``settings = ...`` holds a stale reference to the pre-reload
    # instance. Note: we must NOT patch BEFORE the reload — that
    # would land on the pre-reload instance and survive the reload,
    # masking the regression.
    import app.core.config as config_module

    importlib.reload(config_module)

    # Step 2: pin via dotted-path. Pytest resolves the dotted target
    # through ``sys.modules`` on every lookup, so this lands on the
    # LIVE (post-reload) module's settings instance. Using a
    # non-default value automatically yields
    # ``INGEST_API_KEY_IS_INSECURE_DEFAULT=False`` via the computed
    # property on Settings.
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", REGRESSION_KEY)

    # Step 3: hit the endpoint with a known provider_config row.
    _add_provider_config(session, provider_id="regression-guard-pr297")
    r = client.get("/api/v1/fleet/config")

    assert r.status_code == 200, r.text
    providers = r.json()["config"]["providers"]
    assert "regression-guard-pr297" in providers, (
        f"regression provider missing from /fleet/config response: {sorted(providers)}"
    )
    accounts = providers["regression-guard-pr297"]["accounts"]
    assert accounts, "regression provider has no accounts"
    token = accounts[0].get("credential_token")
    assert token, (
        "endpoint read stale settings — late-import in "
        "app/api/endpoints/fleet.py regressed; "
        "INGEST_API_KEY was patched via dotted-path on the LIVE "
        "module but the endpoint read the bound snapshot from the "
        "pre-reload module. Restore the late-import pattern in "
        "app/api/endpoints/fleet.py's /fleet/config handler "
        "(PR #297 fix)."
    )
