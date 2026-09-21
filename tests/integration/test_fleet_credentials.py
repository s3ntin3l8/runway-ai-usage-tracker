"""Integration tests for the sidecar credential pipeline (issuer side).

Covers:
- ``/fleet/config`` issuing ``credential_token`` per account when ``INGEST_API_KEY``
  is configured and the row has at least one credential field.
- Token wire format decodes back to the (provider, account) pair it scopes
  with version + expiry fields populated.

The companion ``POST /api/v1/fleet/credentials/redeem`` endpoint lands in
the follow-up PR — it ships with the first production caller; today there
is none, so we don't yet exercise redeem against the live stack.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.core.config import settings
from app.core.db import get_session
from app.main import app
from app.models.db import ProviderConfig

SECRET = "test-sidecar-secret-for-redeem"


@pytest.fixture(autouse=True)
def _isolated_ingest_key(monkeypatch):
    """Pin INGEST_API_KEY for the test session — restore on teardown.

    autouse because every test in this module depends on a configured
    ingest key, and accidentally leaving the test value behind would
    break the rest of the suite.
    """
    monkeypatch.setattr(settings, "INGEST_API_KEY", SECRET)
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
# /fleet/config: token issuance
# ---------------------------------------------------------------------------


def test_config_emits_credential_token_per_account_with_creds(client: TestClient, session: Session):
    """When a row has credentials, ``accounts[*].credential_token`` is set."""
    _add_provider_config(
        session,
        provider_id="openrouter",
        account_id="alice@example.com",
        api_key="sk-test-123",
    )
    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    openrouter = r.json()["config"]["providers"]["openrouter"]
    assert len(openrouter["accounts"]) == 1
    assert openrouter["accounts"][0]["credential_token"]
    # Token is opaque base64url + "." + hex — pin the wire format.
    token = openrouter["accounts"][0]["credential_token"]
    assert token.count(".") == 1
    payload, _, _ = token.rpartition(".")
    # Payload is base64url-encoded JSON; should be decodable and parseable.
    import base64

    padded = payload + "=" * (-len(payload) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    assert decoded["pid"] == "openrouter"
    assert decoded["aid"] == "alice@example.com"
    assert decoded["v"] == 1


def test_config_omits_token_when_no_credential_field(client: TestClient, session: Session):
    """A row with no api_key / session_cookie / oai_sc_cookie gets no token.

    There's no point handing the sidecar a token for an empty row — the
    redeem endpoint would return an empty ``credentials`` object.
    """
    _add_provider_config(session, provider_id="github", account_id="default")
    r = client.get("/api/v1/fleet/config")
    github = r.json()["config"]["providers"]["github"]
    assert github["accounts"][0].get("credential_token") is None


def test_config_omits_token_for_disabled_account(client: TestClient, session: Session):
    """Disabled rows never issue tokens — no point redeeming creds we'll reject."""
    _add_provider_config(
        session,
        provider_id="ollama",
        account_id="default",
        enabled=False,
        api_key="disabled-key",
    )
    r = client.get("/api/v1/fleet/config")
    ollama = r.json()["config"]["providers"]["ollama"]
    assert ollama["accounts"][0].get("credential_token") is None


def test_config_omits_tokens_when_ingest_key_unconfigured(
    client: TestClient, session: Session, monkeypatch
):
    """With ``INGEST_API_KEY`` empty, no tokens are issued."""
    monkeypatch.setattr(settings, "INGEST_API_KEY", "")
    _add_provider_config(session, provider_id="openrouter", account_id="default", api_key="k")
    r = client.get("/api/v1/fleet/config")
    openrouter = r.json()["config"]["providers"]["openrouter"]
    assert openrouter["accounts"][0].get("credential_token") is None


def test_config_token_format_decodes_to_expected_claims(client: TestClient, session: Session):
    """The token issued in /fleet/config decodes to the (provider, account)
    pair it scopes, with version + TTL fields populated. (The redeem
    endpoint lands in the follow-up PR; today we only verify the wire
    format is well-formed.)"""
    from app.services.credential_token import TOKEN_VERSION, verify_credential_token

    _add_provider_config(
        session,
        provider_id="openrouter",
        account_id="alice@example.com",
        api_key="sk-test-123",
        session_cookie="session-cookie-value",
        account_label="Alice",
    )
    cfg = client.get("/api/v1/fleet/config").json()["config"]["providers"]["openrouter"]
    token = cfg["accounts"][0]["credential_token"]

    claims = verify_credential_token(SECRET, token)
    assert claims.provider_id == "openrouter"
    assert claims.account_id == "alice@example.com"
    assert claims.version == TOKEN_VERSION
    assert claims.exp > time.time()

    # Decoded payload has the right shape: {v, pid, aid, exp}.
    encoded, _, _ = token.rpartition(".")
    padded = encoded + "=" * (-len(encoded) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    assert decoded == {
        "v": TOKEN_VERSION,
        "pid": "openrouter",
        "aid": "alice@example.com",
        "exp": claims.exp,
    }


def test_config_emits_distinct_tokens_per_account(client: TestClient, session: Session):
    """Two accounts for the same provider get two distinct tokens."""
    _add_provider_config(session, provider_id="openrouter", account_id="default", api_key="k1")
    _add_provider_config(session, provider_id="openrouter", account_id="alice@x.com", api_key="k2")
    r = client.get("/api/v1/fleet/config")
    accounts = r.json()["config"]["providers"]["openrouter"]["accounts"]
    tokens = {a["account_id"]: a["credential_token"] for a in accounts}
    assert set(tokens) == {"default", "alice@x.com"}
    assert tokens["default"] != tokens["alice@x.com"]


# ---------------------------------------------------------------------------
# Note on redeem endpoint coverage: POST /api/v1/fleet/credentials/redeem is
# intentionally not implemented in this PR. Adding the endpoint without a
# production caller would ship unused authenticated surface — see the PR
# description and code review on #283. The redeem handler lands with the
# first real caller in the follow-up PR; until then this file asserts only
# the issuer (`GET /fleet/config`) and the token verify contract above.
# ---------------------------------------------------------------------------
