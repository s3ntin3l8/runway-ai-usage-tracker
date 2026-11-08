"""Integration tests for multi-account provider config endpoints, preview, and account merging."""

from __future__ import annotations

from sqlmodel import Session, select

from app.models.db import CredentialTag, LatestUsage
from tests.fixtures.test_client import TestClient
from tests.integration._multi_account_provider_config_helpers import (
    _admin_headers,
    _seed_latest_usage,
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)


def test_get_provider_configs_empty_accounts_field(client: TestClient):
    """No config rows: each provider's `accounts` list is empty + account_count=0."""
    r = client.get("/api/v1/system/provider-configs")
    assert r.status_code == 200
    providers = r.json()["providers"]
    assert providers  # registry has at least one entry
    xai = next(p for p in providers if p["provider_id"] == "xai")
    assert xai["supports_api_key"] is True
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


def test_provider_update_requires_explicit_account_id(client: TestClient):
    r = client.put(
        "/api/v1/system/provider-config/openrouter",
        json={"account_label": "ambiguous"},
        headers=_admin_headers(),
    )
    assert r.status_code == 405


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


def test_explicit_put_with_hash_account_id_and_email_label_adopts_email(client: TestClient):
    """When a new config is saved with a credential hash ID and an email label,
    the endpoint adopts the email as the authoritative account_id."""
    hash_id = "1692b86a1d20fa0877d379ee299ad1e90b06b2c0ef0c3021d2a75c5fb5bac2ef"
    resp = client.put(
        f"/api/v1/system/provider-config/openrouter/{hash_id}",
        json={"account_label": "User@Example.com"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["account_id"] == "user@example.com"
    assert resp.json()["original_account_id"] == hash_id

    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    assert any(row["account_id"] == "user@example.com" for row in openrouter["accounts"])
    assert not any(row["account_id"] == hash_id for row in openrouter["accounts"])


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


def test_explicit_put_with_default_account_id_creates_default_row(client: TestClient):
    """Saving via PUT /provider-config/{pid}/default produces the same row
    that single-account installations use for default credentials."""
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


def test_discovered_account_label_is_saved_independently(client: TestClient, session: Session):
    session.add(
        LatestUsage(
            provider_id="openrouter",
            account_id="opaque-account",
            card_json="{}",
        )
    )
    session.commit()
    response = client.put(
        "/api/v1/system/provider-account-label/openrouter/opaque-account",
        json={"account_label": "Work key"},
        headers=_admin_headers(),
    )
    assert response.status_code == 200, response.text

    provider = next(
        item
        for item in client.get("/api/v1/system/provider-configs").json()["providers"]
        if item["provider_id"] == "openrouter"
    )
    account = next(item for item in provider["accounts"] if item["account_id"] == "opaque-account")
    assert account["source"] == "discovered"
    assert account["account_label"] == "Work key"


def test_account_merge_requires_explicit_confirmation_for_gauge_collision(
    client: TestClient, session: Session
):
    session.add_all(
        [
            LatestUsage(
                provider_id="openrouter",
                account_id="source@example.com",
                window_type="weekly",
                variant="default",
                model_id="",
                card_json='{"remaining": 10}',
            ),
            LatestUsage(
                provider_id="openrouter",
                account_id="target@example.com",
                window_type="weekly",
                variant="default",
                model_id="",
                card_json='{"remaining": 8}',
            ),
        ]
    )
    session.commit()
    body = {
        "provider_id": "openrouter",
        "source_account_id": "source@example.com",
        "destination_account_id": "target@example.com",
    }
    preview = client.post(
        "/api/v1/system/provider-account-merge/preview", json=body, headers=_admin_headers()
    )
    assert preview.status_code == 200
    assert preview.json()["counts"]["latest_usage"]["collisions"] == 1

    apply_url = "/api/v1/system/provider-account-merge/apply"
    rejected = client.post(apply_url, json=body, headers=_admin_headers())
    assert rejected.status_code == 409

    applied = client.post(
        apply_url,
        json={**body, "confirm_collisions": True},
        headers=_admin_headers(),
    )
    assert applied.status_code == 200, applied.text
    rows = session.exec(select(LatestUsage).where(LatestUsage.provider_id == "openrouter")).all()
    assert [row.account_id for row in rows] == ["target@example.com"]


def test_account_merge_moves_redirect_tags_by_effective_provider(
    client: TestClient, session: Session
):
    session.add(
        CredentialTag(
            provider_id="gemini",
            credential_origin="provider:gemini",
            account_id="source@example.com",
            sidecar_id="laptop",
            target_provider_id="antigravity",
        )
    )
    session.commit()
    body = {
        "provider_id": "antigravity",
        "source_account_id": "source@example.com",
        "destination_account_id": "target@example.com",
    }

    preview = client.post(
        "/api/v1/system/provider-account-merge/preview", json=body, headers=_admin_headers()
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["counts"]["credential_tags"]["affected"] == 1

    applied = client.post(
        "/api/v1/system/provider-account-merge/apply", json=body, headers=_admin_headers()
    )
    assert applied.status_code == 200, applied.text
    session.expire_all()
    assert session.exec(select(CredentialTag)).one().account_id == "target@example.com"


def test_account_merge_warns_when_default_is_destination(client: TestClient, session: Session):
    session.add_all(
        [
            LatestUsage(
                provider_id="openrouter",
                account_id="source@example.com",
                window_type="weekly",
                variant="source-series",
                card_json='{"remaining": 10}',
            ),
            LatestUsage(
                provider_id="openrouter",
                account_id="default",
                window_type="weekly",
                variant="default-series",
                card_json='{"remaining": 8}',
            ),
        ]
    )
    session.commit()
    body = {
        "provider_id": "openrouter",
        "source_account_id": "source@example.com",
        "destination_account_id": "default",
    }
    preview = client.post(
        "/api/v1/system/provider-account-merge/preview", json=body, headers=_admin_headers()
    )
    assert preview.status_code == 200
    assert preview.json()["shared_default_warning"] is True

    apply_url = "/api/v1/system/provider-account-merge/apply"
    rejected = client.post(apply_url, json=body, headers=_admin_headers())
    assert rejected.status_code == 409
    applied = client.post(
        apply_url,
        json={**body, "confirm_shared_default": True},
        headers=_admin_headers(),
    )
    assert applied.status_code == 200, applied.text


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


def test_preview_account_does_not_fail_on_a_corrupt_quota(client, monkeypatch):
    """A quota the collector cannot read is no answer, not a 500.

    The wizard asks the server what the account behind the credential bills.
    A payload the collector cannot understand proves nothing, so the preview
    answers "no model" -- the same answer the cookie path gives when the
    organizations it read do not agree.
    """
    import app.services.claude_oauth_usage as oauth_usage

    async def _corrupt_usage(client, token):
        return {"five_hour": {"utilization": True}}

    monkeypatch.setattr(oauth_usage, "fetch_oauth_usage", _corrupt_usage)

    r = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic",
            "api_key": "sk-ant-oat-test",  # pragma: allowlist secret
            "detect_billing": True,
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["suggested_billing_type"] is None
