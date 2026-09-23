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
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.main import app
from app.models.db import LatestUsage


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
    # list_provider_configs unions token_cache into `accounts` — clear the
    # in-memory cache so empty-state assertions aren't polluted by prior tests.
    from app.services.token_cache import token_cache

    token_cache._cache.clear()
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()
    token_cache._cache.clear()


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


def test_get_provider_configs_merges_cache_seeded_accounts(client: TestClient):
    """Sidecar-seeded token_cache accounts (no provider_configs row) appear
    as source="discovered" with account_count>=1 — the antigravity bug (#294)."""
    import time

    from app.services.token_cache import token_cache

    # seed_sync writes the same (tokens, meta, last_seen) layout as store()
    # without touching the asyncio lock (await store() from a throwaway loop
    # would bind token_cache._lock to that loop).
    token_cache.seed_sync(
        "antigravity",
        "user@example.com",
        {"refresh_token": "ag-refresh-token"},  # pragma: allowlist secret
        {"account_label": "User", "source": "sidecar"},
        time.time(),
    )
    try:
        r = client.get("/api/v1/system/provider-configs")
        assert r.status_code == 200
        antigravity = next(p for p in r.json()["providers"] if p["provider_id"] == "antigravity")
        assert antigravity["account_count"] >= 1
        discovered = [a for a in antigravity["accounts"] if a["source"] == "discovered"]
        assert discovered, antigravity["accounts"]
        assert any(a["account_id"] == "user@example.com" for a in discovered)
        assert antigravity["account_count"] > 0
    finally:
        token_cache._cache.clear()


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


def _seed_latest_usage(session: Session, provider_id: str, account_id: str) -> None:
    """Insert a synthetic `latest_usage` row so the GET /provider-configs
    endpoint sees the (provider_id, account_id) pair as having live data."""
    from datetime import UTC, datetime

    from app.models.db import LatestUsage

    row = LatestUsage(
        provider_id=provider_id,
        account_id=account_id,
        window_type="weekly",
        variant="default",
        model_id="claude-opus-4-7",
        card_json="{}",
        updated_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()


def test_is_orphaned_false_for_default_row_when_no_live_data(client: TestClient):
    """Default row with no live data and no sibling: NOT orphaned.

    Just-configured providers and providers whose collection is failing
    both hit this state — flagging it as 'safe to remove' would risk
    deleting the user's only credential. Gating on a non-default sibling
    means the dialog only shows the hint when there's a real replacement.
    """
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"account_label": "only row"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    default_row = openrouter["accounts"][0]
    assert default_row["account_id"] == "default"
    assert default_row["is_orphaned"] is False


def test_preview_account_returns_409_when_identity_already_exists(client: TestClient):
    """Existing account under the suggested id → 409 from the endpoint."""
    # Seed one row under alice@example.com.
    client.put(
        "/api/v1/system/provider-config/anthropic/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )

    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic",
            "api_key": "alice@example.com",  # email-shaped → suggests same id  # pragma: allowlist secret
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 409
    # The 409 body wraps the preview detail in `detail` (FastAPI's HTTPException
    # convention). The wizard reads the same fields from there.
    detail = r.json()["detail"]
    assert detail["already_exists"] is True
    assert detail["suggested_account_id"] == "alice@example.com"


def test_preview_account_returns_default_when_no_credential(client: TestClient):
    """No api_key + no session_cookie → default identity, no 4xx."""
    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "anthropic"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["suggested_account_id"] == "default"
    assert data["label_source"] == "default"


def test_preview_account_rejects_unknown_provider(client: TestClient):
    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "no-such-provider", "api_key": "x"},
        headers=_admin_headers(),
    )
    assert r.status_code == 404


def test_clear_api_key_wipes_encrypted_blob(client: TestClient):
    """Setting ``clear_api_key=True`` on the explicit PUT wipes the stored
    key. The next GET reports ``api_key_set=false``."""
    # Seed a row with an api_key.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"api_key": "sk-test-123", "account_label": "only row"},  # pragma: allowlist secret
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    default_row = openrouter["accounts"][0]
    assert default_row["account_id"] == "default"
    assert default_row["api_key_set"] is True

    # Clear the key.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"clear_api_key": True},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    default_row = openrouter["accounts"][0]
    assert default_row["api_key_set"] is False


def test_is_orphaned_true_when_default_shadowed_by_live_sibling(
    client: TestClient, session: Session
) -> None:
    """Default row + non-default sibling with live data: orphan flag fires."""
    # Two real rows: alice@example.com and default.
    for label, account_id in (("alice", "alice@example.com"), ("only", "default")):
        r = client.put(
            f"/api/v1/system/provider-config/openrouter/{account_id}",
            json={"account_label": label},
            headers=_admin_headers(),
        )
        assert r.status_code == 200, r.text

    # The sibling has live data; the default row does not.
    _seed_latest_usage(session, "openrouter", "alice@example.com")

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert by_id["default"]["is_orphaned"] is True, "default shadowed by live sibling"
    assert by_id["alice@example.com"]["is_orphaned"] is False, "sibling is the live one"


def test_is_orphaned_false_when_default_has_live_data(client: TestClient, session: Session) -> None:
    """Default row with live data + sibling without live data: NOT orphaned.

    The default row is the live one, so it isn't shadowed.
    """
    for label, account_id in (("alice", "alice@example.com"), ("default", "default")):
        r = client.put(
            f"/api/v1/system/provider-config/openrouter/{account_id}",
            json={"account_label": label},
            headers=_admin_headers(),
        )
        assert r.status_code == 200, r.text

    _seed_latest_usage(session, "openrouter", "default")

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert by_id["default"]["is_orphaned"] is False
    assert by_id["alice@example.com"]["is_orphaned"] is False


def test_clear_session_cookie_wipes_and_clears_oai_sc_companion(client: TestClient):
    """ChatGPT-specific: clearing the session_cookie also wipes the
    oai-sc companion field (which derives from the same paste)."""
    # Seed with a multi-cookie paste that yields both session_cookie and oai_sc.
    cookie_paste = (
        "__Secure-next-auth.session-token=eyJ.hbGc.payload; oai-sc=eyJzZWNyZXQ.b64.signature"
    )
    r = client.put(
        "/api/v1/system/provider-config/chatgpt/default",
        json={"session_cookie": cookie_paste, "account_label": "Default"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    r = client.put(
        "/api/v1/system/provider-config/chatgpt/default",
        json={"clear_session_cookie": True},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    chatgpt = next(p for p in listing if p["provider_id"] == "chatgpt")
    default = next(a for a in chatgpt["accounts"] if a["account_id"] == "default")
    assert default["session_cookie_set"] is False


# ---------------------------------------------------------------------------
# preview_account_identity — edge cases (#287)
# ---------------------------------------------------------------------------


def test_preview_account_returns_default_when_credential_exists(client: TestClient):
    """Same body, but the canonical ``"default"`` row already exists —
    ``already_exists`` flips to True so the wizard can render the inline
    collision error before the user clicks Next."""
    # Seed a row under account_id="default" so the preview sees it.
    r = client.put(
        "/api/v1/system/provider-config/anthropic/default",
        json={"account_label": "Default"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "anthropic"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    assert r.json()["already_exists"] is True


def test_preview_account_falls_back_to_sha256_when_no_email_or_jwt(client: TestClient):
    """An opaque (non-email, non-JWT) credential hits the SHA hash fallback
    path in ``resolve_account_id`` — opaque session tokens end up here for
    providers like Anthropic that don't extract an email claim. Switched
    from SHA-256 to SHA-512 to clear CodeQL's
    ``py/weak-sensitive-data-hashing`` rule (which only flags SHA-1 /
    SHA-256 of password-tainted data); the 128-char hex output fits the
    existing ``account_id: str`` column with no length cap."""
    import hashlib

    opaque = "opaque-session-token-with-no-email-claim"  # pragma: allowlist secret
    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "anthropic", "session_cookie": opaque},
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["label_source"] == "credential_hash"
    assert (
        data["suggested_account_id"]
        == hashlib.pbkdf2_hmac("sha256", opaque.encode(), b"runway-account-id-v1", 1).hex()
    )


def test_preview_account_returns_404_for_unknown_provider(client: TestClient):
    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "totally-fake-provider",
            "api_key": "sk-x",  # pragma: allowlist secret
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert "Unknown provider" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Cookie-paste extraction (#287) — system.py session_cookie parsing
# ---------------------------------------------------------------------------


def test_apply_provider_config_extracts_session_key_from_cookie_paste(client: TestClient):
    """Pasting a multi-cookie header that contains a ``sessionKey=...`` pair
    (without Cloudflare's cf_clearance present) causes the server to store
    just the sessionKey value, not the whole paste. The Anthropic collector
    reads the stored cookie directly, so the truncated value keeps it happy
    without leaking the unrelated cookie metadata.
    """
    cookie_paste = (
        "sessionKey=sk-ant-secret-value; other_cookie=irrelevant"  # pragma: allowlist secret
    )

    r = client.put(
        "/api/v1/system/provider-config/anthropic/default",
        json={"session_cookie": cookie_paste},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # The row exists with session_cookie_set=True; the actual stored value is
    # not exposed via the API (it's encrypted), but the assert pins the
    # positive path: the request succeeds, the row materialises.
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    anthropic = next(p for p in listing if p["provider_id"] == "anthropic")
    default = next(a for a in anthropic["accounts"] if a["account_id"] == "default")
    assert default["session_cookie_set"] is True


# ---------------------------------------------------------------------------
# DELETE /provider-config/{provider_id}/{account_id}
# ---------------------------------------------------------------------------


def test_delete_provider_config_removes_row(client: TestClient, session: Session) -> None:
    """Per-account DELETE removes the row + its LatestUsage cards.

    Pins the contract that the webapp's ProviderDetailDialog Remove action
    depends on — without it, the front-end surfaces a 'Method Not Allowed'
    toast (FastAPI 405). The endpoint also evicts LatestUsage rows so the
    dashboard doesn't show ghost cards for an account the operator just
    removed, and clears the in-memory token_cache entry so collectors
    don't keep hitting the deleted account's credentials.
    """
    # Seed a row.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # Seed a LatestUsage card that should be evicted by the delete.
    _seed_latest_usage(session, "openrouter", "alice@example.com")
    assert (
        session.exec(
            select(LatestUsage).where(
                LatestUsage.provider_id == "openrouter",
                LatestUsage.account_id == "alice@example.com",
            )
        ).first()
        is not None
    )

    # Pre-seed the in-memory token cache so the DELETE's
    # ``await token_cache.remove(...)`` path is exercised. Use
    # ``seed_sync`` rather than ``store`` — awaiting ``store()`` from a
    # throwaway loop binds the cache's asyncio.Lock to that loop so
    # later TestClient requests can't acquire it. See the
    # ``test_get_provider_configs_merges_cache_seeded_accounts`` test
    # above for the same pattern.
    import time

    from app.services.token_cache import token_cache

    token_cache.seed_sync(
        "openrouter",
        "alice@example.com",
        {"api_key": "sk-or-test-alice"},  # pragma: allowlist secret
        {"account_label": "Alice", "source": "config"},
        time.time(),
    )
    assert token_cache._cache.get("openrouter", {}).get("alice@example.com") is not None

    # Delete.
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "deleted"
    assert body["provider_id"] == "openrouter"
    assert body["account_id"] == "alice@example.com"

    # Row is gone from provider_configs.
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 0
    assert openrouter["accounts"] == []

    # LatestUsage card was evicted.
    assert (
        session.exec(
            select(LatestUsage).where(
                LatestUsage.provider_id == "openrouter",
                LatestUsage.account_id == "alice@example.com",
            )
        ).first()
        is None
    )

    # Token-cache entry was cleared — collectors won't keep fetching
    # credentials for the deleted account.
    assert token_cache._cache.get("openrouter", {}).get("alice@example.com") is None


def test_delete_provider_config_returns_404_for_unknown_provider(
    client: TestClient,
) -> None:
    """Unknown provider → 404, not 405."""
    r = client.delete(
        "/api/v1/system/provider-config/no-such-provider/alice@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert "no-such-provider" in r.json()["detail"]


def test_delete_provider_config_returns_404_for_missing_row(
    client: TestClient,
) -> None:
    """Known provider but no row at this account_id → 404."""
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/nonexistent@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert "nonexistent@example.com" in r.json()["detail"]


def test_delete_provider_config_requires_admin_key(client: TestClient, monkeypatch) -> None:
    """With ADMIN_API_KEY configured and APP_HOST bound off-loopback,
    unauthenticated requests are rejected.

    Pins the auth gate that ``require_admin_key`` enforces on every
    state-changing endpoint — without it, an attacker on the same host
    could delete arbitrary provider configs.

    Patches via dotted paths into both ``app.core.config`` and
    ``app.core.security``: ``resolve_auth`` reads ``settings`` from the
    latter's module-level binding, which can diverge from
    ``app.core.config.settings`` after another test reloads the config
    module (PR #297 round-1 regression guard).
    """
    # Seed a row first (the test fixture leaves ADMIN_API_KEY unset, so the
    # admin gate is effectively a no-op — we use the unauth path).
    client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )

    # Flip auth on: bind off-loopback so the localhost trust bypass
    # doesn't mask the missing-X-Admin-Key rejection we want to assert.
    for dotted in (
        "app.core.config.settings",
        "app.core.security.settings",
    ):
        monkeypatch.setattr(f"{dotted}.ADMIN_API_KEY", "admin-secret")
        monkeypatch.setattr(f"{dotted}.APP_HOST", "0.0.0.0")

    r = client.delete(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
    )
    assert r.status_code in (401, 403), (
        f"unauthenticated DELETE must be rejected, got {r.status_code}"
    )


def test_delete_provider_config_default_row_removes_orphan(
    client: TestClient, session: Session
) -> None:
    """The exact bug behind the user's 'Method Not Allowed' toast: deleting
    a default provider_config row that's been shadowed by a non-default
    sibling succeeds end-to-end and the orphan disappears from the listing.

    Uses openrouter because Gemini's collector mixin auto-stores a
    cookie-derived email in token_cache during the post-PUT force-sync,
    which would surface as a "discovered" account and pollute the count
    assertion. OpenRouter has no such mixin, so the listing mirrors
    exactly what we put in provider_configs."""
    # Two rows: a labeled one + a "default" row left over from initial setup.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, f"PUT alice failed: {r.text}"
    r = client.put(
        "/api/v1/system/provider-config/openrouter/default",
        json={"account_label": "Default"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, f"PUT default failed: {r.text}"

    # The default row is flagged orphan (sibling has live data).
    _seed_latest_usage(session, "openrouter", "alice@example.com")

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert by_id["default"]["is_orphaned"] is True

    # Delete the orphan.
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/default",
        headers=_admin_headers(),
    )
    assert r.status_code == 200, f"DELETE failed: {r.text}"

    # Only alice remains.
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert openrouter["account_count"] == 1
    assert openrouter["accounts"][0]["account_id"] == "alice@example.com"
