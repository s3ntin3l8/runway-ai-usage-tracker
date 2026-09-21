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
# Silent-listener flow (PR #288):
#   POST /fleet/credentials/manifest           — sidecar writes origins
#   POST /fleet/credentials/tags               — operator resolves pending → tag
#   GET  /fleet/credentials/tags/pending       — UI panel
#   GET  /fleet/config account_tag_hints key   — sidecar reads hints on next cycle
# ---------------------------------------------------------------------------


def _sign_manifest(body: bytes, ts: str | None = None) -> tuple[str, str]:
    """Compute HMAC headers the manifest endpoint expects."""
    ts = ts or str(int(time.time()))
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


def test_manifest_503_when_ingest_key_missing(monkeypatch):
    """Same gate as /fleet/ingest: INGEST_API_KEY must be configured."""
    monkeypatch.setattr(settings, "INGEST_API_KEY", "")
    client = TestClient(app)
    r = _post_manifest(client, {"sidecar_id": "alpha", "entries": []})
    assert r.status_code == 503


def test_manifest_401_on_bad_signature(client: TestClient):
    """A signature that doesn't match the HMAC scheme returns 401 (not 400
    for skew). Use a valid timestamp so we exercise the signature-comparison
    branch, not the timestamp-skew branch.
    """
    r = client.post(
        "/api/v1/fleet/credentials/manifest",
        content=b'{"sidecar_id":"x"}',
        headers={
            "X-Signature": "deadbeef",
            "X-Timestamp": str(int(time.time())),
        },
    )
    assert r.status_code == 401


def test_manifest_upserts_pending_and_returns_resolved_for_tagged_origins(
    client: TestClient, session: Session
):
    """A tag pre-existing for one origin is echoed in the response; untagged origins get pending rows."""
    from app.services.credential_tags import CredentialTagRepo

    # Pre-tag one origin to verify the response shape.
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/tagged/.claude/.credentials.json",
        account_id="alice@example.com",
    )
    session.commit()

    body = {
        "sidecar_id": "alpha-host",
        "entries": [
            {
                "provider_id": "anthropic",
                "credential_origin": "path:/tagged/.claude/.credentials.json",
            },
            {
                "provider_id": "anthropic",
                "credential_origin": "path:/untagged/.claude/.credentials.json",
            },
            {"provider_id": "chatgpt", "credential_origin": "path:/untagged2/.codex/auth.json"},
        ],
    }
    r = _post_manifest(client, body)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["sidecar_id"] == "alpha-host"
    assert payload["entries_received"] == 3
    # Tagged entry resolves; untagged entries are absent from resolved but
    # recorded in pending_credential_tags.
    assert payload["resolved"] == {
        "anthropic": {"path:/tagged/.claude/.credentials.json": "alice@example.com"}
    }


def test_manifest_prunes_pending_entries_not_re_reported(client: TestClient, session: Session):
    """Sidecar's manifest is authoritative — credentials it stops reporting
    for get removed from the pending table."""
    from app.services.credential_tags import PendingCredentialTagRepo

    body_full = {
        "sidecar_id": "alpha-host",
        "entries": [
            {"provider_id": "anthropic", "credential_origin": "path:/a"},
            {"provider_id": "anthropic", "credential_origin": "path:/b"},
        ],
    }
    r1 = _post_manifest(client, body_full)
    assert r1.status_code == 200
    pending_a = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert {p.credential_origin for p in pending_a} == {"path:/a", "path:/b"}

    body_partial = {
        "sidecar_id": "alpha-host",
        "entries": [
            {"provider_id": "anthropic", "credential_origin": "path:/a"},
            # /b dropped from disk since the last cycle.
        ],
    }
    r2 = _post_manifest(client, body_partial)
    assert r2.status_code == 200
    assert r2.json()["entries_pruned"] == 1
    pending_a = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert {p.credential_origin for p in pending_a} == {"path:/a"}


def test_manifest_does_not_prune_other_sidecar(client: TestClient, session: Session):
    """Sidecar A's manifest only prunes A's pending rows, never B's."""
    from app.services.credential_tags import PendingCredentialTagRepo

    for sidecar in ("alpha", "beta"):
        PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=sidecar,
            provider_id="anthropic",
            credential_origin="path:/shared",
        )
        session.commit()

    r = _post_manifest(client, {"sidecar_id": "alpha", "entries": []})  # alpha now empty
    assert r.status_code == 200
    assert r.json()["entries_pruned"] == 1

    assert [
        (r.sidecar_id, r.credential_origin) for r in PendingCredentialTagRepo.list_all(session)
    ] == [("beta", "path:/shared")]


def test_manifest_normalizes_fqdn_sidecar_id(client: TestClient, session: Session):
    """PR #290 round-2 review (Hermes suggestion #7): the manifest
    endpoint must mirror /fleet/ingest's ``normalize_sidecar_id`` so a
    reporter that sends ``alpha.example.com`` doesn't get a pending row
    keyed on a string no card can surface in the per-sidecar badge
    query (``?sidecar_id=<registry id>``). The row's
    ``sidecar_id`` column should be the normalized form.

    Today's sidecar normalizes client-side (so this is defensive), but
    a future scripted reporter (custom integration, curl) might not.
    """
    from app.services.account_identity import normalize_sidecar_id
    from app.services.credential_tags import PendingCredentialTagRepo

    body = {
        # FQDN — must normalize to its short hostname before storage.
        "sidecar_id": "alpha.example.com",
        "entries": [
            {"provider_id": "anthropic", "credential_origin": "path:/x"},
        ],
    }
    r = _post_manifest(client, body)
    assert r.status_code == 200, r.text
    assert r.json()["sidecar_id"] == normalize_sidecar_id("alpha.example.com")

    # The pending row uses the normalized sidecar_id — same string the
    # client will pass in ``?sidecar_id=`` to surface it.
    rows = PendingCredentialTagRepo.list_all(session)
    assert len(rows) == 1
    assert rows[0].sidecar_id == normalize_sidecar_id("alpha.example.com")


def test_tag_endpoint_creates_tag_and_clears_pending(client: TestClient, session: Session):
    """Operator's POST creates a CredentialTag and deletes the matching pending row."""
    from app.services.credential_tags import (
        CredentialTagRepo,
        PendingCredentialTagRepo,
    )

    _add_provider_config(
        session, provider_id="anthropic", account_id="alice@example.com", api_key="sk-test"
    )

    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="anthropic",
        credential_origin="path:/.claude/.credentials.json",
    )
    session.commit()

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "alpha-host",
            "provider_id": "anthropic",
            "credential_origin": "path:/.claude/.credentials.json",
            "account_id": "alice@example.com",
        },
    )
    assert resp.status_code == 200, resp.text

    # Tag persisted; pending row cleared.
    assert (
        CredentialTagRepo.get_account_id(
            session,
            provider_id="anthropic",
            credential_origin="path:/.claude/.credentials.json",
        )
        == "alice@example.com"
    )
    assert (
        PendingCredentialTagRepo.get(
            session,
            sidecar_id="alpha-host",
            provider_id="anthropic",
            credential_origin="path:/.claude/.credentials.json",
        )
        is None
    )


def test_tag_endpoint_404_when_provider_account_missing(client: TestClient):
    """Selecting a provider+account pair that has no provider_configs row fails."""
    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "alpha",
            "provider_id": "anthropic",
            "credential_origin": "path:/x",
            "account_id": "ghost@example.com",
        },
    )
    assert resp.status_code == 404
    assert "provider_configs" in resp.text


def test_pending_endpoint_lists_all(client: TestClient, session: Session):
    """Aggregation view for the fleet view banner."""
    from app.services.credential_tags import PendingCredentialTagRepo

    PendingCredentialTagRepo.upsert(
        session, sidecar_id="alpha", provider_id="anthropic", credential_origin="path:/a"
    )
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="alpha", provider_id="anthropic", credential_origin="path:/b"
    )
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="beta", provider_id="chatgpt", credential_origin="path:/c"
    )
    session.commit()

    resp = client.get("/api/v1/fleet/credentials/tags/pending")
    assert resp.status_code == 200
    items = resp.json()["items"]
    sides = {(i["sidecar_id"], i["provider_id"], i["credential_origin"]) for i in items}
    assert sides == {
        ("alpha", "anthropic", "path:/a"),
        ("alpha", "anthropic", "path:/b"),
        ("beta", "chatgpt", "path:/c"),
    }
    counts = resp.json()["counts_by_sidecar"]
    assert counts == {"alpha": 2, "beta": 1}


def test_pending_endpoint_filters_by_sidecar(client: TestClient, session: Session):
    """Per-sidecar query for the per-card badge."""
    from app.services.credential_tags import PendingCredentialTagRepo

    for sidecar in ("alpha", "beta"):
        PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=sidecar,
            provider_id="anthropic",
            credential_origin=f"path:/{sidecar}/x",
        )
    session.commit()

    resp = client.get("/api/v1/fleet/credentials/tags/pending?sidecar_id=alpha")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert {i["credential_origin"] for i in items} == {"path:/alpha/x"}


def test_config_response_carries_account_tag_hints(client: TestClient, session: Session):
    """/fleet/config exposes the per-provider hint map for the sidecar's next cycle."""
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/.claude/.credentials.json",
        account_id="alice@example.com",
    )
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    assert hints == {
        "anthropic": {"path:/.claude/.credentials.json": "alice@example.com"},
    }


def test_config_response_account_tag_hints_empty_when_no_tags(client: TestClient):
    """No tags → empty map (not absent), so the sidecar sees the key safely."""
    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"] == {}


# ---------------------------------------------------------------------------
# Note on redeem endpoint coverage: POST /api/v1/fleet/credentials/redeem is
# intentionally not implemented in this PR. Adding the endpoint without a
# production caller would ship unused authenticated surface — see the PR
# description and code review on #283. The redeem handler lands with the
# first real caller in the follow-up PR; until then this file asserts only
# the issuer (`GET /fleet/config`) and the token verify contract above.
# ---------------------------------------------------------------------------
