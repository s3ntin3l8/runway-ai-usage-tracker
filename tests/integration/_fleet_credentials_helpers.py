"""Shared helpers and fixtures for fleet credentials integration tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.db import get_session
from app.main import app
from app.models.db import ProviderConfig
from tests.fixtures.test_client import TestClient

SECRET = "test-sidecar-secret-for-redeem"


@pytest.fixture(autouse=True)
def _isolated_ingest_key(monkeypatch):
    """Pin INGEST_API_KEY for the test session — restore on teardown.

    autouse because every test in this module depends on a configured
    ingest key, and accidentally leaving the test value behind would
    break the rest of the suite.

    Uses the dotted-path ``monkeypatch.setattr`` form (not the bound
    ``settings`` import) so the patch survives
    ``importlib.reload(app.core.config)`` from ``tests/unit/test_config.py``.
    After a reload, the bound ``settings`` object is the OLD module's;
    only the string-path patch re-resolves through ``sys.modules`` to
    the live settings instance that ``validate_ingest_auth`` reads
    at call time (PR #290 round-2 review, Hermes body suggestion
    #1, issue #291).
    """
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", SECRET)
    yield


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


def _sign(body_bytes: bytes, timestamp: str | None = None) -> tuple[str, str]:
    """Compute the HMAC headers the server expects."""
    ts = timestamp or str(int(time.time()))
    sig = hmac.new(SECRET.encode(), ts.encode() + body_bytes, hashlib.sha256).hexdigest()
    return ts, sig


def _add_provider_config(
    session: Session,
    *,
    provider_id: str,
    account_id: str = "default",
    enabled: bool = True,
    api_key: str | None = None,
    session_cookie: str | None = None,
    oai_sc_cookie: str | None = None,
    account_label: str | None = None,
) -> ProviderConfig:
    row = ProviderConfig(
        provider_id=provider_id,
        account_id=account_id,
        enabled=enabled,
        account_label=account_label,
    )
    if api_key is not None:
        row.api_key = api_key
    if session_cookie is not None:
        row.session_cookie = session_cookie
    if oai_sc_cookie is not None:
        row.oai_sc_cookie = oai_sc_cookie
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Silent-listener flow (PR #288):
#   POST /fleet/credentials/manifest           — sidecar writes origins
#   POST /fleet/credentials/tags               — operator resolves pending → tag
#   GET  /fleet/credentials/tags/pending       — UI panel
#   GET  /fleet/config account_tag_hints key   — sidecar reads hints on next cycle
# ---------------------------------------------------------------------------


def _sign_manifest(body: bytes, ts: str | None = None) -> tuple[str, str]:
    """Compute HMAC headers the manifest endpoint expects."""
    from scripts.sidecar_pkg.transport import signing_timestamp

    ts = ts or signing_timestamp()
    sig = hmac.new(SECRET.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    return ts, sig


def _post_manifest(client: TestClient, body: dict, ts: str | None = None):
    raw = json.dumps(body).encode()
    ts, sig = _sign_manifest(raw, ts=ts)
    return client.post(
        "/api/v1/fleet/credentials/manifest",
        content=raw,
        headers={"Content-Type": "application/json", "X-Signature": sig, "X-Timestamp": ts},
    )


# ---------------------------------------------------------------------------
# Per-sidecar hint scoping (#319) — /fleet/config?sidecar_id= +
# multi-host auto-hint delivery gate
# ---------------------------------------------------------------------------


def _add_live_sidecar(session: Session, sidecar_id: str, hostname: str) -> None:
    from app.models.db import SidecarRegistry

    session.add(SidecarRegistry(sidecar_id=sidecar_id, hostname=hostname))
    session.commit()


# --- #443: key-scoped env origins, legacy tags and rows --------------------------------


def _token_card(provider: str, origin: str, api_key: str, account_id: str | None = None) -> dict:
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
            **({"account_id": account_id} if account_id else {}),
        },
    }


def _ingest(client: TestClient, sidecar_id: str, cards: list[dict]):
    body = json.dumps({"provider": "sidecar", "sidecar_id": sidecar_id, "metrics": cards}).encode()
    ts, sig = _sign(body)
    return client.post(
        "/api/v1/fleet/ingest",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
    )


def _file_source(
    session: Session,
    *,
    provider_id: str,
    account_id: str,
    source_id: str,
    origin: str,
    sidecar_id: str = "laptop",
) -> None:
    from app.services.credential_sources import touch_source

    touch_source(
        session,
        provider_id=provider_id,
        account_id=account_id,
        source_id=source_id,
        source_type="path",
        source_label="oauth_creds.json",
        credential_origin=origin,
        sidecar_id=sidecar_id,
    )


# ---------------------------------------------------------------------------
# Parked mappings — a credential bound to an archived account.
#
# An archived account can no longer collect (no collector runs for a provider
# whose accounts are all archived), but the operator still needs to get rid of
# the "credential unmapped" warning for a credential of a provider they
# retired. Mapping such a credential onto the archived account *parks* it: the
# binding resolves the origin — it never becomes pending again and the
# operator is not nagged — while staying inert (no hint ships, nothing is
# stamped on the archived account) until the account is restored.
# ---------------------------------------------------------------------------


def _add_archived_provider_config(
    session: Session, *, provider_id: str, account_id: str
) -> ProviderConfig:
    """A retired account: archived, and therefore never enabled."""
    row = ProviderConfig(
        provider_id=provider_id,
        account_id=account_id,
        enabled=False,
        archived=True,
    )
    session.add(row)
    session.commit()
    return row
