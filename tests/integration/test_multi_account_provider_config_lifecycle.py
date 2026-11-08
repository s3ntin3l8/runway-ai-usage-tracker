"""Integration tests for provider account deletion, archiving, orphan cleanup, and lifecycle management."""

from __future__ import annotations

import json

from sqlmodel import Session, select

from app.models.db import AuditLog, CredentialSource, LatestUsage
from tests.fixtures.test_client import TestClient
from tests.integration._multi_account_provider_config_helpers import (
    _admin_headers,
    _seed_latest_usage,
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)


def test_archived_account_does_not_inflate_active_account_count(client: TestClient):
    client.put(
        "/api/v1/system/provider-config/openrouter/old@example.com",
        json={"archived": True},
        headers=_admin_headers(),
    )
    client.put(
        "/api/v1/system/provider-config/openrouter/live@example.com",
        json={},
        headers=_admin_headers(),
    )
    provider = next(
        p
        for p in client.get("/api/v1/system/provider-configs").json()["providers"]
        if p["provider_id"] == "openrouter"
    )
    assert provider["account_count"] == 1
    assert provider["archived_count"] == 1
    assert {a["account_id"] for a in provider["accounts"]} == {
        "old@example.com",
        "live@example.com",
    }


def test_archived_account_usage_flags_ignore_unrendered_provider_rows(
    session: Session, monkeypatch
):
    from app.api.endpoints.system import _archived_account_usage_flags
    from app.models.db import ProviderConfig
    from app.services.collector_manager import manager

    monkeypatch.setattr(manager, "collector_registry", {})
    row = ProviderConfig(
        provider_id="opencode-free", account_id="archived@example.com", archived=True
    )

    assert _archived_account_usage_flags(session, [row]) == {}


# ---------------------------------------------------------------------------
# DELETE /provider-config/{provider_id}/{account_id}
# ---------------------------------------------------------------------------


def test_delete_provider_config_archives_row(client: TestClient, session: Session) -> None:
    """Per-account DELETE soft-archives the row + evicts LatestUsage cards.

    Pins the contract that the webapp's ProviderDetailDialog Remove action
    depends on — without it, the front-end surfaces a 'Method Not Allowed'
    toast (FastAPI 405). The endpoint evicts LatestUsage rows so the
    dashboard doesn't show ghost cards for an account the operator just
    removed, and clears the in-memory token_cache entry so collectors
    don't keep hitting the removed account's credentials.

    PR #317 round-2 review note: we soft-archive (``row.archived=True``,
    ``row.enabled=False``) rather than hard-delete because ``usage_events``
    for the pair survives the operation, and the synthetic loop in
    ``_fetch_fleet_view_sync`` (``app/api/endpoints/usage_overview.py:240``) would
    re-create the card from those events if the row weren't in the
    ``archived_pairs`` skip-set. Soft-archive keeps the pair in the
    skip-set and the dashboard filters it out cleanly.
    """
    # Seed a row with a stored credential — the DELETE must wipe it
    # (PR #317 round-2 re-review warning: the dialog's confirm copy
    # promises "deletes the configuration row and its stored
    # credentials"; a kept credential could be re-cached on re-enable).
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={
            "account_label": "Alice",
            "api_key": "sk-or-test-alice",  # pragma: allowlist secret
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # session_cookie isn't accepted for openrouter via PUT (no cookie
    # support), so set it + the ChatGPT oai-sc companion directly to pin
    # that branch of the wipe too (round-3 approve nit: companion parity).
    from app.models.db import ProviderConfig

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "alice@example.com",
        )
    ).one()
    assert row.api_key is not None
    row.session_cookie = "sessionKey=stale"  # pragma: allowlist secret
    row.oai_sc_cookie = "oai-sc=stale"  # pragma: allowlist secret
    session.add(row)
    session.commit()

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

    # Row is gone from the operator-facing listing (soft-archive hides it).
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    # PR #317 round-2 review: we soft-archive (``archived=True``) rather
    # than hard-delete — the repo convention keeps archived rows visible
    # in this listing so the operator can un-archive if they change
    # their mind. The synthetic loop in /usage/fleet filters via the
    # ``archived_pairs`` skip-set (app/api/endpoints/usage_overview.py:236,260).
    assert "alice@example.com" in by_id
    assert by_id["alice@example.com"]["archived"] is True
    assert by_id["alice@example.com"]["enabled"] is False

    # PR #317 round-2 re-review warning: stored credentials are wiped on
    # Remove — the dialog's confirm copy promises it, and a kept blob
    # could be re-cached if the row were ever re-enabled.
    session.refresh(
        session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "openrouter",
                ProviderConfig.account_id == "alice@example.com",
            )
        ).one()
    )
    wiped = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "alice@example.com",
        )
    ).one()
    assert wiped.api_key is None, "DELETE must clear the stored api_key"
    assert wiped.session_cookie is None, "DELETE must clear the stored session_cookie"
    assert wiped.oai_sc_cookie is None, (
        "DELETE must clear the ChatGPT oai_sc companion cookie "
        "(parity with clear_session_cookie, round-3 approve nit)"
    )
    assert wiped.oai_sc_cookie_encrypted is None

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
    # credentials for the removed account.
    assert token_cache._cache.get("openrouter", {}).get("alice@example.com") is None

    # PR #317 round-2 review: even with usage_events still holding the
    # pair, _fetch_fleet_view_sync's synthetic loop must NOT re-create a
    # card — that's the whole reason we soft-archive instead of
    # hard-delete. Pin it end-to-end.
    from datetime import UTC, datetime

    from app.models.db import UsageEvent

    session.add(
        UsageEvent(
            provider_id="openrouter",
            account_id="alice@example.com",
            event_id="evt-still-in-history-1",
            kind="message",
            ts=datetime.now(UTC),
            session_id="sess-after-delete",
        )
    )
    session.commit()

    r = client.get("/api/v1/usage/fleet")
    fleet = r.json().get("fleet", r.json())  # accept both shapes
    if isinstance(fleet, dict) and "entries" in fleet:
        fleet_entries = fleet["entries"]
    else:
        fleet_entries = fleet
    archived_alive = [
        e
        for e in fleet_entries
        if e.get("provider_id") == "openrouter" and e.get("account_id") == "alice@example.com"
    ]
    assert archived_alive == [], f"archived pair resurfaced in fleet view: {archived_alive}"


def test_permanent_delete_removes_empty_archived_account_and_source_state(
    client: TestClient, session: Session, monkeypatch
):
    from app.models.db import LatestUsageContribution, ProviderConfig
    from app.services.collector_manager import manager
    from app.services.credential_tags import CredentialTagRepo
    from app.services.token_cache import token_cache

    account_id = "duplicate@example.com"
    row = ProviderConfig(
        provider_id="opencode",
        account_id=account_id,
        enabled=False,
        archived=True,
        account_label="Duplicate",
        api_key="oc_sk_duplicate",  # pragma: allowlist secret
    )
    session.add(row)
    session.add(
        LatestUsage(
            provider_id="opencode",
            account_id=account_id,
            sidecar_id="dev-01",
            window_type="weekly",
            variant="default",
            model_id="",
            card_json="{}",
        )
    )
    session.add(
        LatestUsageContribution(
            provider_id="opencode",
            account_id=account_id,
            source_id="collector:opencode",
            window_type="weekly",
            variant="default",
            model_id="",
            card_json="{}",
        )
    )
    session.add(
        CredentialSource(
            provider_id="opencode",
            account_id=account_id,
            source_id="sidecar:old-auth",
            source_type="file",
            source_label="auth.json",
            sidecar_id="dev-01",
        )
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin="path:/old/auth.json#123456789abc",
        account_id=account_id,
        sidecar_id="dev-01",
    )
    session.commit()
    token_cache.seed_sync(
        "opencode",
        account_id,
        {"api_key": "oc_sk_duplicate"},  # pragma: allowlist secret
        {"source": "sidecar"},
    )
    preferences = {("opencode", account_id): {"sidecar:old-auth": (True, 0)}}
    monkeypatch.setattr(manager, "_credential_source_preferences", preferences)

    response = client.delete(
        f"/api/v1/system/provider-config/opencode/{account_id}?permanent=true",
        headers=_admin_headers(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "permanently_deleted"
    assert (
        session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "opencode", ProviderConfig.account_id == account_id
            )
        ).first()
        is None
    )
    assert (
        session.exec(
            select(LatestUsage).where(
                LatestUsage.provider_id == "opencode", LatestUsage.account_id == account_id
            )
        ).first()
        is None
    )
    assert (
        session.exec(
            select(LatestUsageContribution).where(
                LatestUsageContribution.provider_id == "opencode",
                LatestUsageContribution.account_id == account_id,
            )
        ).first()
        is None
    )
    assert (
        session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == "opencode",
                CredentialSource.account_id == account_id,
            )
        ).first()
        is None
    )
    assert (
        CredentialTagRepo.get(
            session,
            provider_id="opencode",
            credential_origin="path:/old/auth.json#123456789abc",
            sidecar_id="dev-01",
        )
        is None
    )
    assert token_cache._cache.get("opencode", {}).get(account_id) is None
    assert ("opencode", account_id) not in manager._credential_source_preferences

    audit_entry = session.exec(
        select(AuditLog).where(
            AuditLog.action == "provider_config.purge",
            AuditLog.target_id == f"opencode/{account_id}",
        )
    ).one()
    assert json.loads(audit_entry.payload_json or "{}") == {
        "tags_cleared": 1,
        "permanent": True,
    }


def test_permanent_delete_rejects_open_code_account_with_tier_usage(
    client: TestClient, session: Session
):
    from datetime import UTC, datetime

    from app.models.db import ProviderConfig, UsageEvent

    account_id = "archived@example.com"
    session.add(
        ProviderConfig(provider_id="opencode", account_id=account_id, enabled=False, archived=True)
    )
    session.add(
        UsageEvent(
            provider_id="opencode-free",
            account_id=account_id,
            event_id="free-tier-history",
            kind="message",
            ts=datetime.now(UTC),
        )
    )
    session.commit()

    listed = client.get("/api/v1/system/provider-configs")
    opencode = next(p for p in listed.json()["providers"] if p["provider_id"] == "opencode")
    archived_account = next(a for a in opencode["accounts"] if a["account_id"] == account_id)
    assert archived_account["has_usage_events"] is True

    response = client.delete(
        f"/api/v1/system/provider-config/opencode/{account_id}?permanent=true",
        headers=_admin_headers(),
    )
    assert response.status_code == 409
    assert (
        session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "opencode", ProviderConfig.account_id == account_id
            )
        ).first()
        is not None
    )


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
    unauthenticated requests are rejected AND no mutation occurs.

    Pins the auth gate that ``require_admin_key`` enforces on every
    state-changing endpoint — without it, an attacker on the same host
    could delete arbitrary provider configs. The "no mutation on
    rejection" half follows the pattern at
    ``tests/integration/test_audit_log.py:124`` (Hermes suggestion #6).

    Patches via dotted paths into both ``app.core.config`` and
    ``app.core.security``: ``resolve_auth`` reads ``settings`` from the
    latter's module-level binding, which can diverge from
    ``app.core.config.settings`` after another test reloads the config
    module (PR #297 round-1 regression guard).
    """
    # Seed a row first (the test fixture leaves ADMIN_API_KEY unset, so the
    # admin gate is effectively a no-op — we use the unauth path).
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

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

    # Pin: the rejected request must NOT archive the row.
    listing = client.get(
        "/api/v1/system/provider-configs", headers={"X-Admin-Key": "admin-secret"}
    ).json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert "alice@example.com" in by_id
    assert by_id["alice@example.com"]["archived"] is False


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

    # Alice is still active; the default row is now soft-archived
    # (archived=True, enabled=False) per the repo's hide-from-dashboard
    # convention. The dashboard's /usage/fleet view filters archived
    # pairs via ``archived_pairs`` so the ghost card never re-appears.
    listing = client.get("/api/v1/system/provider-configs").json()["providers"]
    openrouter = next(p for p in listing if p["provider_id"] == "openrouter")
    by_id = {row["account_id"]: row for row in openrouter["accounts"]}
    assert by_id["alice@example.com"]["archived"] is False
    assert by_id["alice@example.com"]["enabled"] is True
    assert by_id["default"]["archived"] is True
    assert by_id["default"]["enabled"] is False


def test_delete_provider_config_clears_dangling_credential_tags(
    client: TestClient, session: Session
) -> None:
    """PR #317 round-2 review warning: hard-deleting a provider_configs row
    leaves ``credential_tags.account_id`` rows pointing at the removed
    account, which the sidecar picks up via ``list_pending_payload`` and
    re-asserts via ``/fleet/ingest``. The handler must clear any matching
    tags atomically with the row removal.

    Pins:
      - ``CredentialTagRepo.delete_by_account`` is called for the pair.
      - The row count is reported on the response payload so the operator
        sees what was cleaned up.
      - A second tag for the SAME provider but a DIFFERENT account stays
        untouched (the bulk delete must not over-reach).
    """
    from app.models.db import CredentialTag
    from app.services.credential_tags import CredentialTagRepo

    # Seed the provider_config + a few CredentialTag rows.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    CredentialTagRepo.set_tag(
        session,
        provider_id="openrouter",
        credential_origin="env:OPENROUTER_API_KEY",
        account_id="alice@example.com",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="openrouter",
        credential_origin="file:/etc/openrouter-cookie",
        account_id="alice@example.com",
    )
    # Tag for a different account on the same provider — must survive.
    CredentialTagRepo.set_tag(
        session,
        provider_id="openrouter",
        credential_origin="env:OPENROUTER_API_KEY_BOB",
        account_id="bob@example.com",
    )
    session.commit()

    before = session.exec(
        select(CredentialTag).where(
            CredentialTag.provider_id == "openrouter",
            CredentialTag.account_id == "alice@example.com",
        )
    ).all()
    assert len(before) == 2

    r = client.delete(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tags_cleared"] == 2

    # The alice tags are gone.
    after = session.exec(
        select(CredentialTag).where(
            CredentialTag.provider_id == "openrouter",
            CredentialTag.account_id == "alice@example.com",
        )
    ).all()
    assert after == []

    # The bob tag survives — bulk delete must not over-reach.
    survivor = session.exec(
        select(CredentialTag).where(
            CredentialTag.provider_id == "openrouter",
            CredentialTag.account_id == "bob@example.com",
        )
    ).first()
    assert survivor is not None
    assert survivor.credential_origin == "env:OPENROUTER_API_KEY_BOB"


def test_delete_provider_config_writes_audit_row(client: TestClient, session: Session) -> None:
    """PR #317 round-2 review suggestion: pin the audit-log row.

    Every other admin mutation has an audit assertion
    (tests/integration/test_audit_log.py:62-124); the new DELETE should
    too. Also pins the negative case — a 404 does NOT leave a row,
    matching ``test_failed_mutation_does_not_write_audit_row``.
    """
    from app.models.db import AuditLog

    def _rows() -> list[AuditLog]:
        return list(session.exec(select(AuditLog).order_by(AuditLog.ts)).all())

    # Seed.
    r = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"account_label": "Alice"},
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # 404 path: a request for a non-existent pair must NOT write an audit row.
    pre_404 = _rows()
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/nonexistent@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 404
    assert _rows() == pre_404, "failed DELETE must not write an audit row"

    # 200 path: a successful DELETE writes exactly one row.
    r = client.delete(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    rows = _rows()
    assert len(rows) == 1
    assert rows[0].action == "provider_config.delete"
    assert rows[0].target_id == "openrouter/alice@example.com"
    # Composite-key payload schema documented in PR #317.
    assert rows[0].payload_json is not None
    assert "tags_cleared" in rows[0].payload_json


def test_delete_provider_config_drops_smart_collector(client: TestClient, session: Session) -> None:
    """PR #317 round-2 re-review suggestion: pin the ``_sync_collectors(force=True)``
    call with the manager's real pair key shape.

    Without the sync, ``manager.smart_collectors`` keeps a SmartCollector
    for the removed pair alive and the next poll re-writes a
    ``LatestUsage`` card, undoing the eviction. Round-2 review noted the
    earlier version registered under the bare provider id (``"openrouter"``),
    which passes via step 3's generic prune of any key not in
    ``active_keys`` — it pinned the presence of a sync call, not the pair
    semantics. This version:
      - registers under the real ``f"{pid}:{aid}"`` key,
      - seeds the token cache for the pair so before-DELETE the pair is
        genuinely active (cache entry + collector both present),
      - after DELETE asserts BOTH are gone (cache cleared by the handler,
        collector pruned because the pair left ``active_keys``),
      - restores the manager singleton so later tests see prior state.
    """
    import time

    from app.services.collector_manager import manager
    from app.services.smart_collector import SmartCollector
    from app.services.token_cache import token_cache

    pid = "openrouter"
    aid = "alice@example.com"
    pair_key = f"{pid}:{aid}"

    # Isolate the singleton: snapshot and restore around the test.
    saved_collectors = dict(manager.smart_collectors)
    try:
        # Seed config + token cache so the pair is a genuinely active
        # dynamic collector before the DELETE.
        r = client.put(
            f"/api/v1/system/provider-config/{pid}/{aid}",
            json={"account_label": "Alice"},
            headers=_admin_headers(),
        )
        assert r.status_code == 200

        token_cache.seed_sync(
            pid,
            aid,
            {"api_key": "sk-or-test-pair"},  # pragma: allowlist secret
            {"account_label": "Alice", "source": "config"},
            time.time(),
        )
        assert token_cache._cache.get(pid, {}).get(aid) is not None

        collector = SmartCollector.__new__(SmartCollector)
        collector.provider_id = pid
        collector.account_id = aid
        manager.smart_collectors[pair_key] = collector

        # Before DELETE: pair active in both cache and collectors.
        assert pair_key in manager.smart_collectors
        assert token_cache._cache.get(pid, {}).get(aid) is not None

        # Delete.
        r = client.delete(
            f"/api/v1/system/provider-config/{pid}/{aid}",
            headers=_admin_headers(),
        )
        assert r.status_code == 200

        # The DELETE handler must trigger a sync that prunes the removed
        # pair from manager.smart_collectors (it left active_keys once the
        # handler dropped its cache entry) — otherwise the next poll would
        # re-write a LatestUsage card and undo the eviction.
        assert pair_key not in manager.smart_collectors, (
            f"DELETE must drop the SmartCollector for the removed pair; "
            f"manager.smart_collectors={list(manager.smart_collectors)}"
        )
        # And the cache entry the pair was active on is gone.
        assert token_cache._cache.get(pid, {}).get(aid) is None
    finally:
        manager.smart_collectors.clear()
        manager.smart_collectors.update(saved_collectors)


def test_delete_provider_config_reenable_put_stays_disabled(
    client: TestClient, session: Session
) -> None:
    """PR #317 round-2 re-review warning: an archived row must never re-enable.

    The dialog's master toggle sends ``{"enabled": true}`` PUTs for every
    disabled account. On a Remove'd (archived) row that used to leave
    ``archived=True, enabled=True`` — the manual-cache sync gate
    (``collector_manager.py:113``) would re-cache the credential and
    step-2 would respawn a collector while ``archived_pairs`` kept hiding
    the pair from the fleet view: invisible collection.

    Pins the invariant ``row.archived ⇒ row.enabled is False`` enforced in
    ``_apply_provider_config_update``, and that an explicit un-archive
    (``archived: false``) is still the recovery path.
    """
    from app.models.db import ProviderConfig
    from app.services.token_cache import token_cache

    pid = "openrouter"
    aid = "alice@example.com"

    r = client.put(
        f"/api/v1/system/provider-config/{pid}/{aid}",
        json={
            "account_label": "Alice",
            "api_key": "sk-or-test-reenable",  # pragma: allowlist secret
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    r = client.delete(
        f"/api/v1/system/provider-config/{pid}/{aid}",
        headers=_admin_headers(),
    )
    assert r.status_code == 200

    # Master-switch style enable PUT — the exact payload the dialog sends.
    r = client.put(
        f"/api/v1/system/provider-config/{pid}/{aid}",
        json={"enabled": True},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == pid,
            ProviderConfig.account_id == aid,
        )
    ).one()
    assert row.archived is True
    assert row.enabled is False, (
        "enabling an archived row must be a no-op (archived ⇒ enabled=False)"
    )
    # Credentials were wiped by DELETE and must not be re-cached by the
    # PUT's post-commit collector sync.
    assert token_cache._cache.get(pid, {}).get(aid) is None

    # Recovery path: explicit un-archive re-enables the row (existing
    # flow — ProviderPage archive toggle / settings dialog).
    r = client.put(
        f"/api/v1/system/provider-config/{pid}/{aid}",
        json={"archived": False},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    session.expire_all()
    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == pid,
            ProviderConfig.account_id == aid,
        )
    ).one()
    assert row.archived is False
    assert row.enabled is True
