"""Integration tests for the fleet credential tag and pending endpoints."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.models.db import CredentialSource
from tests.fixtures.test_client import TestClient
from tests.integration._fleet_credentials_helpers import (
    _add_live_sidecar,
    _add_provider_config,
    _file_source,
    _isolated_ingest_key,  # noqa: F401 — fixture
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)


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

    # Tag persisted (default scope = this machine → scoped row);
    # pending row cleared.
    assert (
        CredentialTagRepo.get_account_id(
            session,
            provider_id="anthropic",
            credential_origin="path:/.claude/.credentials.json",
            sidecar_id="alpha-host",
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


def test_tag_endpoint_canonicalizes_target_and_refreshes_colliding_source(
    client: TestClient, session: Session, monkeypatch
):
    from app.services.collector_manager import manager

    _add_provider_config(
        session, provider_id="anthropic", account_id="alice@example.com", api_key="sk-test"
    )
    origin = "path:/home/alice/.claude/.credentials.json"
    source_id = "sidecar:alpha:path:/auth.json"
    monkeypatch.setitem(
        manager.credential_source_preferences,
        ("anthropic", "default"),
        {source_id: (False, 7)},
    )
    monkeypatch.setitem(
        manager.credential_source_preferences,
        ("anthropic", "alice@example.com"),
        {},
    )
    session.add_all(
        [
            CredentialSource(
                provider_id="anthropic",
                account_id="default",
                source_id=source_id,
                source_type="file",
                source_label="new machine label",
                credential_origin=origin,
                sidecar_id="alpha",
            ),
            CredentialSource(
                provider_id="anthropic",
                account_id="alice@example.com",
                source_id=source_id,
                source_type="server",
                source_label="stale label",
                credential_origin="path:/old.json",
                sidecar_id="old-host",
            ),
        ]
    )
    session.commit()

    response = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "alpha",
            "provider_id": "anthropic",
            "credential_origin": origin,
            "account_id": "Alice@Example.com",
        },
    )

    assert response.status_code == 200, response.text
    sources = list(
        session.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == "anthropic",
                CredentialSource.source_id == source_id,
            )
        ).all()
    )
    assert len(sources) == 1
    assert sources[0].account_id == "alice@example.com"
    assert sources[0].source_type == "file"
    assert sources[0].source_label == "new machine label"
    assert sources[0].credential_origin == origin
    assert sources[0].sidecar_id == "alpha"
    assert source_id not in manager.credential_source_preferences[("anthropic", "default")]
    assert manager.credential_source_preferences[("anthropic", "alice@example.com")][source_id] == (
        sources[0].enabled,
        sources[0].priority,
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


def test_tag_endpoint_sidecar_scope_clears_only_that_sidecar(client: TestClient, session: Session):
    """scope="sidecar" (default): tag is sidecar-scoped; only that
    sidecar's pending row is cleared (#319)."""
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    _add_provider_config(
        session, provider_id="anthropic", account_id="alice@example.com", api_key="sk-test"
    )
    for sc in ("alpha-host", "beta-host"):
        PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=sc,
            provider_id="anthropic",
            credential_origin="path:/shared/.claude/.credentials.json",
        )
    session.commit()

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "alpha-host",
            "provider_id": "anthropic",
            "credential_origin": "path:/shared/.claude/.credentials.json",
            "account_id": "alice@example.com",
        },
    )
    assert resp.status_code == 200, resp.text

    # Tag scoped to alpha only.
    rows = CredentialTagRepo.list_by_provider(session, provider_id="anthropic")
    assert len(rows) == 1
    assert rows[0].sidecar_id == "alpha-host"
    # Only alpha's pending row cleared; beta still prompts.
    assert (
        PendingCredentialTagRepo.get(
            session,
            sidecar_id="alpha-host",
            provider_id="anthropic",
            credential_origin="path:/shared/.claude/.credentials.json",
        )
        is None
    )
    assert (
        PendingCredentialTagRepo.get(
            session,
            sidecar_id="beta-host",
            provider_id="anthropic",
            credential_origin="path:/shared/.claude/.credentials.json",
        )
        is not None
    )


def test_tag_endpoint_deployment_scope_clears_all_sidecars(client: TestClient, session: Session):
    """scope="deployment": tag is deployment-wide (sidecar_id NULL);
    every sidecar's pending row for that origin is cleared (#319)."""
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    _add_provider_config(
        session, provider_id="anthropic", account_id="alice@example.com", api_key="sk-test"
    )
    for sc in ("alpha-host", "beta-host"):
        PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=sc,
            provider_id="anthropic",
            credential_origin="path:/shared/.claude/.credentials.json",
        )
    session.commit()

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "alpha-host",
            "provider_id": "anthropic",
            "credential_origin": "path:/shared/.claude/.credentials.json",
            "account_id": "alice@example.com",
            "scope": "deployment",
        },
    )
    assert resp.status_code == 200, resp.text

    rows = CredentialTagRepo.list_by_provider(session, provider_id="anthropic")
    assert len(rows) == 1
    assert rows[0].sidecar_id is None
    assert PendingCredentialTagRepo.list_all(session) == []


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


def test_pending_endpoint_describes_the_source_in_words(client: TestClient, session: Session):
    """The pending list names the credential ("Browser cookie"), not just its origin
    string, so the operator can tell a cookie from another credential file."""
    from app.services.credential_tags import PendingCredentialTagRepo

    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="opencode",
        credential_origin="cookie:opencode/Chrome/Profile 1/Cookies",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="opencode",
        credential_origin="cookie:opencode/session",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="anthropic",
        credential_origin="cookie:anthropic/Chrome/Work/Cookies",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="opencode",
        credential_origin="path:/home/u/.local/share/opencode/auth.json",
    )
    session.commit()

    items = client.get("/api/v1/fleet/credentials/tags/pending").json()["items"]
    by_origin = {item["credential_origin"]: item for item in items}
    assert by_origin["cookie:opencode/Chrome/Profile 1/Cookies"]["origin_label"] == "Browser cookie"
    assert (
        by_origin["cookie:opencode/Chrome/Profile 1/Cookies"]["origin_detail"]
        == "Chrome · Profile 1"
    )
    assert by_origin["cookie:opencode/session"]["origin_label"] == "Browser cookie"
    assert by_origin["cookie:opencode/session"]["origin_detail"] is None
    assert by_origin["path:/home/u/.local/share/opencode/auth.json"]["origin_label"] == "auth.json"
    # The owning client names the credential: the browser a native Claude cookie
    # came from, the tool a file belongs to.
    assert by_origin["cookie:anthropic/Chrome/Work/Cookies"]["origin_app"] == "Chrome"
    assert by_origin["cookie:opencode/Chrome/Profile 1/Cookies"]["origin_app"] == "Browser"
    assert by_origin["path:/home/u/.local/share/opencode/auth.json"]["origin_app"] == "OpenCode"
    # The private profile of a native Claude cookie never reaches the payload.
    assert by_origin["cookie:anthropic/Chrome/Work/Cookies"]["origin_detail"] is None


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


def test_pending_endpoint_serves_persisted_quota_preview(client: TestClient, session: Session):
    from app.services.credential_tags import PendingCredentialTagRepo

    PendingCredentialTagRepo.set_quota_preview(
        session,
        sidecar_id="alpha",
        provider_id="antigravity",
        credential_origin="path:/auth.json",
        preview=[
            {
                "service_name": "Antigravity",
                "remaining": 7,
                "api_key": "fake-key",  # pragma: allowlist secret
            }  # pragma: allowlist secret
        ],  # pragma: allowlist secret
    )
    session.commit()

    response = client.get("/api/v1/fleet/credentials/tags/pending")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["quota_preview"] == [{"service_name": "Antigravity", "remaining": 7}]
    assert item["quota_preview_observed_at"]
    assert item["quota_preview_stale"] is False
    assert "fake-key" not in response.text


def test_pending_endpoint_expires_old_quota_preview(
    client: TestClient, session: Session, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from app.core.config import settings
    from app.services.credential_tags import PendingCredentialTagRepo

    monkeypatch.setattr(settings, "PENDING_CREDENTIAL_PREVIEW_MAX_AGE_SECONDS", 60)
    observed_at = datetime.now(UTC) - timedelta(seconds=61)
    row = PendingCredentialTagRepo.set_quota_preview(
        session,
        sidecar_id="alpha",
        provider_id="antigravity",
        credential_origin="path:/auth.json",
        preview=[{"service_name": "Antigravity", "remaining": 7}],
        observed_at=observed_at,
    )
    session.commit()

    response = client.get("/api/v1/fleet/credentials/tags/pending")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["quota_preview"] == []
    assert item["quota_preview_stale"] is True
    assert item["quota_preview_observed_at"] == observed_at.isoformat()
    session.refresh(row)
    assert row.quota_preview_json is not None


def test_tag_endpoint_rejects_empty_sidecar_id_for_machine_scope(
    client: TestClient, session: Session
) -> None:
    _add_provider_config(session, provider_id="anthropic", account_id="alice@example.com")
    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "  ",
            "provider_id": "anthropic",
            "credential_origin": "provider:anthropic",
            "account_id": "alice@example.com",
        },
    )
    assert resp.status_code == 422, resp.text


def test_tag_endpoint_normalizes_sidecar_id(client: TestClient, session: Session) -> None:
    from app.services.credential_tags import CredentialTagRepo

    _add_provider_config(session, provider_id="anthropic", account_id="alice@example.com")
    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "Alpha-Host.example.com",
            "provider_id": "anthropic",
            "credential_origin": "provider:anthropic",
            "account_id": "alice@example.com",
        },
    )
    assert resp.status_code == 200, resp.text
    rows = CredentialTagRepo.list_by_provider(session, provider_id="anthropic")
    assert [r.sidecar_id for r in rows] == ["alpha-host"]


def test_deployment_tag_replaces_machine_scoped_overrides(
    client: TestClient, session: Session
) -> None:
    """Tagging "All machines" must win on every machine — scoped rows
    would otherwise keep resolving first, invisibly."""
    from app.services.credential_tags import CredentialTagRepo

    _add_provider_config(session, provider_id="anthropic", account_id="alice@example.com")
    _add_provider_config(session, provider_id="anthropic", account_id="team@example.com")
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="alice@example.com",
        sidecar_id="beta",
    )
    session.commit()

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "alpha",
            "provider_id": "anthropic",
            "credential_origin": "provider:anthropic",
            "account_id": "team@example.com",
            "scope": "deployment",
        },
    )
    assert resp.status_code == 200, resp.text

    r_beta = client.get("/api/v1/fleet/config?sidecar_id=beta")
    assert r_beta.json()["account_tag_hints"]["anthropic"] == {
        "provider:anthropic": "team@example.com"
    }


def test_listed_credential_tags_describe_their_origin(client: TestClient, session: Session) -> None:
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="chatgpt",
        credential_origin="path:/home/u/.codex/auth.json#abc",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    session.commit()

    (item,) = client.get("/api/v1/fleet/credentials/tags").json()["items"]
    assert (item["origin_label"], item["origin_app"], item["origin_path"]) == (
        "auth.json",
        "Codex CLI",
        "~/.codex/auth.json",
    )


def test_listed_credential_tags_name_the_browser_a_cookie_came_from(
    client: TestClient, session: Session
) -> None:
    """A cookie rule says which browser holds it, so it can't be read as a file."""
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin="cookie:opencode/Chrome/Profile 1/Cookies",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    session.commit()

    (item,) = client.get("/api/v1/fleet/credentials/tags").json()["items"]
    assert (item["origin_label"], item["origin_detail"]) == (
        "Browser cookie",
        "Chrome · Profile 1",
    )


def test_listed_credential_tags_report_whether_a_machine_still_matches_them(
    client: TestClient, session: Session
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.models.db import CredentialSource, SidecarRegistry
    from app.services.credential_tags import CredentialTagRepo

    origin_used = "path:/home/u/.config/gh/hosts.yml"
    origin_gone = "path:/home/u/gone.json"
    session.add(SidecarRegistry(sidecar_id="alpha", last_seen=datetime.now(UTC)))
    for origin in (origin_used, origin_gone):
        CredentialTagRepo.set_tag(
            session,
            provider_id="github",
            credential_origin=origin,
            account_id="me",
            sidecar_id="alpha",
        )
    session.commit()
    for tag in CredentialTagRepo.list_all(session):
        tag.set_at = datetime.now(UTC) - timedelta(days=30)
        session.add(tag)
    session.add(
        CredentialSource(
            provider_id="github",
            account_id="me",
            source_id="sidecar:alpha:hosts",
            source_type="sidecar",
            source_label="hosts.yml",
            credential_origin=origin_used,
            sidecar_id="alpha",
        )
    )
    session.commit()

    items = {
        i["credential_origin"]: i
        for i in client.get("/api/v1/fleet/credentials/tags").json()["items"]
    }

    assert items[origin_used]["stale"] is False
    assert items[origin_used]["last_matched_at"] is not None
    assert items[origin_gone]["stale"] is True
    assert items[origin_gone]["last_matched_at"] is None


def test_list_and_delete_credential_tags(client: TestClient, session: Session) -> None:
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="team@example.com",
    )
    session.commit()

    listed = client.get("/api/v1/fleet/credentials/tags")
    assert listed.status_code == 200
    assert sorted((t["sidecar_id"] or "", t["account_id"]) for t in listed.json()["items"]) == [
        ("", "team@example.com"),
        ("alpha", "alice@example.com"),
    ]

    # Omitted sidecar_id deletes only the deployment-wide row.
    resp = client.delete(
        "/api/v1/fleet/credentials/tags",
        params={"provider_id": "anthropic", "credential_origin": "provider:anthropic"},
    )
    assert resp.status_code == 200, resp.text
    remaining = client.get("/api/v1/fleet/credentials/tags").json()["items"]
    assert [(t["sidecar_id"], t["account_id"]) for t in remaining] == [
        ("alpha", "alice@example.com")
    ]

    resp = client.delete(
        "/api/v1/fleet/credentials/tags",
        params={
            "provider_id": "anthropic",
            "credential_origin": "provider:anthropic",
            "sidecar_id": "alpha",
        },
    )
    assert resp.status_code == 200
    assert client.get("/api/v1/fleet/credentials/tags").json()["items"] == []

    missing = client.delete(
        "/api/v1/fleet/credentials/tags",
        params={"provider_id": "anthropic", "credential_origin": "provider:anthropic"},
    )
    assert missing.status_code == 404


def test_deleting_sidecar_drops_its_scoped_tags_and_pending_rows(
    client: TestClient, session: Session
) -> None:
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    _add_live_sidecar(session, "alpha", "alpha-host")
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="alpha", provider_id="chatgpt", credential_origin="provider:chatgpt"
    )
    session.commit()

    resp = client.delete("/api/v1/fleet/sidecars/alpha")
    assert resp.status_code == 200, resp.text
    assert CredentialTagRepo.list_all(session) == []
    assert PendingCredentialTagRepo.list_all(session) == []


def test_delete_tag_rejects_sidecar_id_that_normalizes_to_empty(
    client: TestClient, session: Session
) -> None:
    """A malformed sidecar_id must 422, not fall through to deleting the
    deployment-wide row (#324 review)."""
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="team@example.com",
    )
    session.commit()

    resp = client.delete(
        "/api/v1/fleet/credentials/tags",
        params={
            "provider_id": "anthropic",
            "credential_origin": "provider:anthropic",
            "sidecar_id": "..",
        },
    )
    assert resp.status_code == 422
    assert len(CredentialTagRepo.list_all(session)) == 1


@pytest.mark.parametrize(
    "origin", ["cookie:kimi_coding/session", "keychain:Claude Code-credentials"]
)
def test_an_all_machines_tag_is_refused_for_browser_and_keychain_credentials(
    client: TestClient, session: Session, origin: str
):
    """Those exist on one machine only; a deployment-wide tag would follow an account
    switch on a host it was never made for."""
    _add_provider_config(session, provider_id="kimi_coding", account_id="alice@example.com")

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "laptop",
            "provider_id": "kimi_coding",
            "credential_origin": origin,
            "account_id": "alice@example.com",
            "scope": "deployment",
        },
    )

    assert resp.status_code == 422
    assert "belong to one machine" in resp.text


def test_a_machine_scoped_tag_is_still_allowed_for_a_cookie_origin(
    client: TestClient, session: Session
):
    _add_provider_config(session, provider_id="kimi_coding", account_id="alice@example.com")

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "laptop",
            "provider_id": "kimi_coding",
            "credential_origin": "cookie:kimi_coding/session",
            "account_id": "alice@example.com",
            "scope": "sidecar",
        },
    )

    assert resp.status_code == 200, resp.text


def test_an_all_machines_tag_is_still_allowed_for_a_shared_path_origin(
    client: TestClient, session: Session
):
    """A shared (NFS) home directory really is one origin: that is what the scope is for."""
    _add_provider_config(session, provider_id="gemini", account_id="alice@example.com")

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "laptop",
            "provider_id": "gemini",
            "credential_origin": "path:/shared/.gemini/oauth_creds.json",
            "account_id": "alice@example.com",
            "scope": "deployment",
        },
    )

    assert resp.status_code == 200, resp.text


def test_retagging_onto_an_existing_source_row_carries_its_attempt_history(
    client: TestClient, session: Session
):
    """Moving a source onto a row already at the target must bring the attempt that
    produced its health, or the merged row reads healthy with no evidence (#485)."""
    from datetime import UTC, datetime

    from app.models.db import CredentialSource
    from app.services.credential_sources import sidecar_source_id

    origin = "path:/home/u/.gemini/oauth_creds.json"
    source_id = sidecar_source_id("laptop", origin)
    for account in ("hash:old", "alice@example.com"):
        _file_source(
            session, provider_id="gemini", account_id=account, source_id=source_id, origin=origin
        )
    _add_provider_config(session, provider_id="gemini", account_id="alice@example.com")
    when = datetime(2026, 10, 1, tzinfo=UTC)
    old = session.exec(
        select(CredentialSource).where(CredentialSource.account_id == "hash:old")
    ).one()
    old.health = "auth_failed"
    old.last_attempt_at = when
    old.last_error = "Authentication failed"
    session.add(old)
    session.commit()

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "laptop",
            "provider_id": "gemini",
            "credential_origin": origin,
            "account_id": "alice@example.com",
        },
    )
    assert resp.status_code == 200, resp.text

    merged = session.exec(select(CredentialSource)).one()
    assert merged.account_id == "alice@example.com"
    assert (merged.health, merged.last_error) == ("auth_failed", "Authentication failed")
    assert merged.last_attempt_at is not None
