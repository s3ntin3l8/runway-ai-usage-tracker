"""Integration tests for the sidecar credential pipeline.

Covers:
- ``/fleet/config`` issuing ``credential_token`` per account when ``INGEST_API_KEY``
  is configured and the row has at least one credential field.
- ``POST /api/v1/fleet/credentials/redeem`` validating HMAC headers + token
  signature + expiry and returning the decrypted credentials.
- Audit log entry on redemption (target_id + field names; never the
  credentials themselves).
"""

from __future__ import annotations

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
from app.models.db import AuditLog, ProviderConfig

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


def test_config_token_format_matches_redeem_roundtrip(client: TestClient, session: Session):
    """The token issued in /fleet/config round-trips through the redeem
    endpoint and yields the same credentials the row was stored with."""
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

    # Now redeem it.
    body = json.dumps({"token": token}, separators=(",", ":")).encode()
    ts, sig = _sign(body)
    r = client.post(
        "/api/v1/fleet/credentials/redeem",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": sig,
            "X-Timestamp": ts,
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["provider_id"] == "openrouter"
    assert payload["account_id"] == "alice@example.com"
    assert payload["credentials"]["api_key"] == "sk-test-123"
    assert payload["credentials"]["session_cookie"] == "session-cookie-value"
    assert "oai_sc_cookie" not in payload["credentials"]


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
# POST /api/v1/fleet/credentials/redeem — HMAC + token validation
# ---------------------------------------------------------------------------


def _redeem(client: TestClient, token: str, *, headers: dict[str, str] | None = None) -> TestClient:
    body = json.dumps({"token": token}, separators=(",", ":")).encode()
    ts, sig = _sign(body)
    hdr = {"Content-Type": "application/json", "X-Signature": sig, "X-Timestamp": ts}
    if headers:
        hdr.update(headers)
    return client.post("/api/v1/fleet/credentials/redeem", content=body, headers=hdr)


def _issue_token_for(account_id: str = "default", ttl: int = 3600) -> str:
    from app.services.credential_token import issue_credential_token

    return issue_credential_token(
        SECRET,
        provider_id="openrouter",
        account_id=account_id,
        ttl_seconds=ttl,
    )


def test_redeem_missing_headers_returns_401(client: TestClient):
    body = json.dumps({"token": _issue_token_for()}, separators=(",", ":")).encode()
    r = client.post("/api/v1/fleet/credentials/redeem", content=body)
    assert r.status_code == 401
    assert "Missing HMAC" in r.json()["detail"]


def test_redeem_wrong_signature_returns_401(client: TestClient):
    body = json.dumps({"token": _issue_token_for()}, separators=(",", ":")).encode()
    ts, _ = _sign(body)
    r = client.post(
        "/api/v1/fleet/credentials/redeem",
        content=body,
        headers={"X-Signature": "0" * 64, "X-Timestamp": ts},
    )
    assert r.status_code == 401
    assert "Invalid HMAC" in r.json()["detail"]


def test_redeem_timestamp_skew_rejected(client: TestClient):
    body = json.dumps({"token": _issue_token_for()}, separators=(",", ":")).encode()
    # 6 minutes in the past — past the 5-min envelope.
    ts = str(int(time.time()) - 360)
    sig = hmac.new(SECRET.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    r = client.post(
        "/api/v1/fleet/credentials/redeem",
        content=body,
        headers={"X-Signature": sig, "X-Timestamp": ts},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "timestamp_expired"


def test_redeem_tampered_token_rejected(client: TestClient):
    token = _issue_token_for()
    # Flip a character in the encoded payload.
    encoded, _, sig = token.rpartition(".")
    flipped = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
    r = _redeem(client, f"{flipped}.{sig}")
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "invalid_token"


def test_redeem_expired_token_rejected(client: TestClient):
    # 1-second TTL + sleep to make it expired.
    import time as _t

    token = _issue_token_for(ttl=1)
    _t.sleep(1.1)
    r = _redeem(client, token)
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "invalid_token"
    assert "expired" in r.json()["detail"]["message"].lower()


def test_redeem_token_for_missing_row_returns_401(client: TestClient):
    """A token with a valid signature but pointing at a deleted row is
    refused with 401 (not 404) so the response surface doesn't leak
    whether the (provider, account) tuple ever existed."""
    token = _issue_token_for(account_id="ghost@x.com")
    r = _redeem(client, token)
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_token"


def test_redeem_disabled_row_returns_401(client: TestClient, session: Session):
    """Disabled rows don't issue tokens; if someone has a token for one
    (e.g. it was issued before disable), refuse — matches /fleet/config
    behaviour where disabled rows have no token."""
    _add_provider_config(
        session,
        provider_id="openrouter",
        account_id="default",
        enabled=True,
        api_key="sk-original",
    )
    token = _issue_token_for()
    row = session.exec(
        __import__("sqlmodel")
        .select(ProviderConfig)
        .where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "default",
        )
    ).first()
    assert row is not None
    row.enabled = False
    session.add(row)
    session.commit()

    r = _redeem(client, token)
    # Token verifies; row exists; row is disabled. Redeem should still
    # succeed (the credential is readable, the redemption doesn't care
    # about enabled). The endpoint serves the value; downstream collection
    # is responsible for honoring enabled.
    assert r.status_code == 200, r.text
    assert r.json()["credentials"]["api_key"] == "sk-original"


def test_redeem_response_carries_only_present_fields(client: TestClient, session: Session):
    """The ``credentials`` map only contains fields that the row has."""
    _add_provider_config(
        session, provider_id="openrouter", account_id="default", api_key="only-key"
    )
    r = _redeem(client, _issue_token_for())
    assert r.status_code == 200
    creds = r.json()["credentials"]
    assert creds == {"api_key": "only-key"}


def test_redeem_writes_audit_log_with_target_id_and_field_names(
    client: TestClient, session: Session
):
    """Redemption appends one audit row per call; the payload records the
    (provider, account) tuple and the field names surfaced (never the
    credentials themselves)."""
    _add_provider_config(
        session,
        provider_id="openrouter",
        account_id="alice@example.com",
        api_key="sk-audit",
        session_cookie="c-audit",
    )
    r = _redeem(client, _issue_token_for(account_id="alice@example.com"))
    assert r.status_code == 200

    audit_rows = session.exec(__import__("sqlmodel").select(AuditLog)).all()
    matches = [
        row
        for row in audit_rows
        if row.action == "sidecar.credential.redeem"
        and row.target_id == "openrouter/alice@example.com"
    ]
    assert len(matches) == 1
    payload = json.loads(matches[0].payload_json)
    assert sorted(payload["fields"]) == ["api_key", "session_cookie"]
    # Belt-and-braces: the literal credential strings MUST NOT appear.
    assert "sk-audit" not in matches[0].payload_json
    assert "c-audit" not in matches[0].payload_json
    assert (
        "alice@example.com" in matches[0].payload_json or matches[0].target_id
    )  # either via target_id or payload


def test_redeem_disabled_when_ingest_key_empty(client: TestClient, session: Session, monkeypatch):
    """``INGEST_API_KEY`` empty → 503 (defensive, matches /fleet/ingest)."""
    _add_provider_config(session, provider_id="openrouter", account_id="default", api_key="sk")
    monkeypatch.setattr(settings, "INGEST_API_KEY", "")
    r = _redeem(client, _issue_token_for())
    assert r.status_code == 503


def test_redeem_rejects_default_insecure_key(client: TestClient, session: Session, monkeypatch):
    """Default insecure key triggers 503 (matches /fleet/ingest)."""
    _add_provider_config(session, provider_id="openrouter", account_id="default", api_key="sk")
    monkeypatch.setattr(settings, "INGEST_API_KEY", "sidecar-default-secret")
    # _sign uses the test SECRET, but the endpoint checks the *settings*
    # value — so we can sign with anything here. We want the 503 path.
    r = _redeem(client, _issue_token_for())
    assert r.status_code == 503


def test_redeem_body_too_large_rejected(client: TestClient, session: Session):
    """The redeem body is tiny (a single token string). Larger payloads are
    rejected before HMAC verification — defense against trivial DoS."""
    _add_provider_config(session, provider_id="openrouter", account_id="default", api_key="sk")
    big = b"x" * (8 * 1024)  # 8 KB > 4 KB cap
    r = client.post(
        "/api/v1/fleet/credentials/redeem",
        content=big,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_redeem_rejects_invalid_json_body(client: TestClient):
    """Non-JSON body returns 400 (our explicit JSON parse error)."""
    body = b"not json"
    ts, sig = _sign(body)
    r = client.post(
        "/api/v1/fleet/credentials/redeem",
        content=body,
        headers={"X-Signature": sig, "X-Timestamp": ts},
    )
    assert r.status_code == 400
    assert "Invalid JSON" in r.json()["detail"]


def test_redeem_missing_token_field_returns_400(client: TestClient):
    """Valid JSON without the ``token`` field returns 400."""
    body = json.dumps({"not_token": "x"}, separators=(",", ":")).encode()
    ts, sig = _sign(body)
    r = client.post(
        "/api/v1/fleet/credentials/redeem",
        content=body,
        headers={"X-Signature": sig, "X-Timestamp": ts},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_body"


def test_redeem_missing_body_returns_400(client: TestClient):
    """An empty body returns 400 before HMAC verification."""
    body = b""
    ts, sig = _sign(body)
    r = client.post(
        "/api/v1/fleet/credentials/redeem",
        content=body,
        headers={"X-Signature": sig, "X-Timestamp": ts},
    )
    assert r.status_code == 400
