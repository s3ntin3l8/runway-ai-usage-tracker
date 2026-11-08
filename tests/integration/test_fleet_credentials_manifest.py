"""Integration tests for the fleet credential manifest endpoint and silent-listener lifecycle."""

from __future__ import annotations

import json
import time

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from app.api.endpoints.fleet_credentials import CredentialManifestRequest
from app.main import app
from app.models.db import CredentialSource, ProviderConfig
from tests.fixtures.test_client import TestClient
from tests.integration._fleet_credentials_helpers import (
    _add_archived_provider_config,
    _add_live_sidecar,
    _add_provider_config,
    _ingest,
    _isolated_ingest_key,  # noqa: F401 — fixture
    _post_manifest,
    _sign,
    _sign_manifest,
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)


async def _tagged_claude_manifest_source(
    session, cache, manager, provider, fingerprint, set_by, profile="Default"
):
    from app.services.credential_sources import sidecar_source_id
    from app.services.credential_tags import CredentialTagRepo

    origin = f"cookie:{provider}/chrome/{profile}/Cookies#{fingerprint}"
    source_id = sidecar_source_id("resolved-host", origin)
    account = (
        "11111111-1111-4111-8111-111111111111"
        if provider == "anthropic_console"
        else "person@example.com"
    )
    session.add(
        CredentialSource(
            provider_id=provider,
            account_id=account,
            source_id=source_id,
            source_type="cookie",
            source_label="resolved-host",
            sidecar_id="resolved-host",
            credential_origin=origin,
        )
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id=provider,
        account_id=account,
        sidecar_id="resolved-host",
        credential_origin=origin,
        set_by=set_by,
    )
    session.commit()
    await cache.store(
        provider,
        {"session_cookie": f"fake-cookie-{fingerprint}"},
        account_id=account,
        source_id=source_id,
        source="resolved-host",
        source_metadata={
            "source_type": "cookie",
            "sidecar_id": "resolved-host",
            "credential_origin": origin,
            "identity_pending": False,
        },
    )
    manager.set_credential_source_preference(provider, account, source_id, True, 3)
    return origin, source_id, account


def test_manifest_503_when_ingest_key_missing(monkeypatch):
    """Same gate as /fleet/ingest: INGEST_API_KEY must be configured."""
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", "")
    client = TestClient(app)
    r = _post_manifest(client, {"sidecar_id": "alpha", "entries": []})
    assert r.status_code == 503


def test_claude_manifest_only_surfaces_unmatched_oauth_account(client, session):
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    _add_provider_config(session, provider_id="anthropic", account_id="alice@example.com")
    body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["anthropic"],
        "entries": [
            {
                "provider_id": "anthropic",
                "credential_origin": "path:/alice",
                "account_id": "alice@example.com",
            },
            {
                "provider_id": "anthropic",
                "credential_origin": "path:/bob",
                "account_id": "bob@example.com",
            },
        ],
    }
    result = _post_manifest(client, body)

    assert result.status_code == 200
    pending = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert [row.credential_origin for row in pending] == ["path:/bob"]
    assert pending[0].claimed_account_id == "bob@example.com"
    matched = CredentialTagRepo.get(
        session,
        provider_id="anthropic",
        credential_origin="path:/alice",
        sidecar_id="alpha-host",
    )
    assert matched is not None
    assert matched.account_id == "alice@example.com"
    assert matched.set_by == "identity_claim"
    assert (
        CredentialTagRepo.list_pending_payload(
            session, providers=["anthropic"], sidecar_id="alpha-host"
        )
        == {}
    )

    # Automatic claim records are refreshed from current account configuration;
    # they do not become sticky operator assignments if the account is disabled.
    alice = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "anthropic",
            ProviderConfig.account_id == "alice@example.com",
        )
    ).one()
    alice.enabled = False
    session.add(alice)
    session.commit()
    _post_manifest(client, body)
    pending = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert {row.credential_origin for row in pending} == {"path:/alice", "path:/bob"}
    assert (
        CredentialTagRepo.get(
            session,
            provider_id="anthropic",
            credential_origin="path:/alice",
            sidecar_id="alpha-host",
        )
        is None
    )
    listed = client.get("/api/v1/fleet/credentials/tags/pending")
    assert listed.status_code == 200
    assert {
        item["credential_origin"]: item["claimed_account_id"] for item in listed.json()["items"]
    } == {"path:/alice": "alice@example.com", "path:/bob": "bob@example.com"}


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
    # recorded in pending_credential_tags. The answer is complete per
    # provider, so a provider with no hints comes back as an empty map
    # rather than being absent (#874).
    assert payload["resolved"] == {
        "anthropic": {"path:/tagged/.claude/.credentials.json": "alice@example.com"},
        "chatgpt": {},
    }


def test_manifest_ignores_pass_through_hermes_and_drops_its_stale_row(
    client: TestClient, session: Session
):
    """Hermes calls other providers' accounts; it has none to tag.

    An old sidecar still reports ``provider:hermes``. The entry must not become
    a pending row, a row an earlier report left behind must go, and neither may
    show on the Untagged list.
    """
    from app.api.endpoints.fleet_credentials import visible_pending_rows
    from app.models.db import PendingCredentialTag
    from app.services.credential_tags import PendingCredentialTagRepo

    for stale in ("hermes", "hermes-nous"):
        PendingCredentialTagRepo.upsert(
            session,
            sidecar_id="alpha-host",
            provider_id=stale,
            credential_origin=f"provider:{stale}",
        )
    session.commit()

    body = {
        "sidecar_id": "alpha-host",
        "entries": [
            {
                "provider_id": "hermes",
                "credential_origin": "provider:hermes",
                "reason": "events_untagged",
            },
            {
                "provider_id": "hermes-nous",
                "credential_origin": "provider:hermes-nous",
                "reason": "events_untagged",
            },
            {
                "provider_id": "minimax",
                "credential_origin": "provider:minimax",
                "reason": "events_untagged",
            },
        ],
    }
    assert _post_manifest(client, body).status_code == 200

    rows = session.exec(select(PendingCredentialTag)).all()
    assert [(r.provider_id, r.credential_origin) for r in rows] == [("minimax", "provider:minimax")]
    assert [r.provider_id for r in visible_pending_rows(session)] == ["minimax"]

    # A prefixed row that is already stored stays out of the list on its own.
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="hermes-openai",
        credential_origin="provider:hermes-openai",
    )
    session.commit()
    assert [r.provider_id for r in visible_pending_rows(session)] == ["minimax"]


def test_manifest_records_why_an_origin_was_reported(client: TestClient, session: Session):
    from app.models.db import PendingCredentialTag

    body = {
        "sidecar_id": "alpha-host",
        "entries": [
            {
                "provider_id": "deepseek",
                "credential_origin": "path:/home/u/.deepseek/credentials.json",
                "reason": "token_withheld",
            },
            {"provider_id": "deepseek", "credential_origin": "path:/older/sidecar.json"},
        ],
    }
    assert _post_manifest(client, body).status_code == 200
    session.expire_all()
    reasons = {
        r.credential_origin: r.reason for r in session.exec(select(PendingCredentialTag)).all()
    }
    assert reasons == {
        "path:/home/u/.deepseek/credentials.json": "token_withheld",
        "path:/older/sidecar.json": None,
    }


def test_manifest_keeps_the_louder_reason_whatever_the_entry_order(
    client: TestClient, session: Session
):
    from app.models.db import PendingCredentialTag

    origin = "path:/home/u/.deepseek/credentials.json"
    for entries in (
        [{"reason": "token_withheld"}, {"reason": "events_untagged"}],
        [{"reason": "events_untagged"}, {"reason": "token_withheld"}],
    ):
        body = {
            "sidecar_id": "alpha-host",
            "entries": [
                {"provider_id": "deepseek", "credential_origin": origin, **e} for e in entries
            ],
        }
        assert _post_manifest(client, body).status_code == 200
        session.expire_all()
        assert session.exec(select(PendingCredentialTag)).one().reason == "token_withheld"


def test_manifest_persists_safe_health_metadata_without_token_values(
    client: TestClient, session: Session
):
    from datetime import UTC, datetime

    body = {
        "sidecar_id": "health-host",
        "entries": [],
        "observations": [
            {
                "provider_id": "antigravity",
                "credential_origin": "path:/home/user/.config/agy/oauth_creds.json",
                "token_types": ["oauth_token", "refresh_token"],
                "expires_at": 1_700_000_000,
            }
        ],
    }
    response = _post_manifest(client, body)

    assert response.status_code == 200
    source = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == "antigravity",
            CredentialSource.account_id == "default",
        )
    ).one()
    assert source.token_types_json == '["oauth_token", "refresh_token"]'
    assert source.credential_expires_at.replace(tzinfo=UTC) == datetime.fromtimestamp(
        1_700_000_000, tz=UTC
    )
    assert "oauth_creds" not in response.text


def test_manifest_observation_reuses_locally_identified_source_row(
    client: TestClient, session: Session
):
    """A credential whose identity the sidecar resolved locally (no operator tag) is
    already registered under the real account by ingest. The manifest must update that
    row, not file a second one under ``default`` (a phantom "Pending identity")."""
    from app.services.credential_sources import sidecar_source_id, touch_source

    origin = "path:/home/user/.codex/auth.json"
    source_id = sidecar_source_id("health-host", origin)
    touch_source(
        session,
        provider_id="chatgpt",
        account_id="alice@example.com",
        source_id=source_id,
        source_type="file",
        source_label="auth.json",
        credential_origin=origin,
        sidecar_id="health-host",
    )
    session.commit()

    response = _post_manifest(
        client,
        {
            "sidecar_id": "health-host",
            "entries": [],
            "observations": [
                {
                    "provider_id": "chatgpt",
                    "credential_origin": origin,
                    "token_types": ["oauth_token", "refresh_token"],
                    "expires_at": 1_900_000_000,
                }
            ],
        },
    )

    assert response.status_code == 200
    rows = session.exec(
        select(CredentialSource).where(CredentialSource.provider_id == "chatgpt")
    ).all()
    assert [r.account_id for r in rows] == ["alice@example.com"]
    assert rows[0].token_types_json == '["oauth_token", "refresh_token"]'
    assert rows[0].credential_expires_at is not None


def test_manifest_limits_health_observation_count():
    observations = [
        {"provider_id": "antigravity", "credential_origin": f"path:/{index}.json"}
        for index in range(257)
    ]

    with pytest.raises(ValidationError):
        CredentialManifestRequest(sidecar_id="health-host", observations=observations)


def test_manifest_ignores_unrepresentable_health_expiry(client: TestClient, session: Session):
    response = _post_manifest(
        client,
        {
            "sidecar_id": "health-host",
            "entries": [],
            "observations": [
                {
                    "provider_id": "antigravity",
                    "credential_origin": "path:/overflow.json",
                    "token_types": ["oauth_token"],
                    "expires_at": 1e300,
                }
            ],
        },
    )

    assert response.status_code == 200
    source = session.exec(
        select(CredentialSource).where(
            CredentialSource.source_label == "overflow.json",
        )
    ).one()
    assert source.credential_expires_at is None


def test_manifest_prunes_origins_missing_from_complete_snapshot(
    client: TestClient, session: Session
):
    """A complete manifest removes credential origins that disappeared."""
    from app.services.credential_tags import PendingCredentialTagRepo

    body_full = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["anthropic"],
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
        "completed_providers": ["anthropic"],
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


def test_manifest_prunes_only_the_reporting_sidecar(client: TestClient, session: Session):
    """A later empty snapshot prunes alpha without touching beta."""
    from app.services.credential_tags import PendingCredentialTagRepo

    for sidecar in ("alpha", "beta"):
        PendingCredentialTagRepo.upsert(
            session,
            sidecar_id=sidecar,
            provider_id="anthropic",
            credential_origin="path:/shared",
        )
        session.commit()

    r = _post_manifest(
        client,
        {"sidecar_id": "alpha", "entries": [], "completed_providers": ["anthropic"]},
    )  # alpha now empty
    assert r.status_code == 200
    assert r.json()["entries_pruned"] == 1

    assert [
        (r.sidecar_id, r.credential_origin) for r in PendingCredentialTagRepo.list_all(session)
    ] == [("beta", "path:/shared")]


def test_manifest_does_not_prune_provider_omitted_from_partial_cycle(
    client: TestClient, session: Session
):
    from app.services.credential_tags import PendingCredentialTagRepo

    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="anthropic",
        credential_origin="path:/keep-anthropic",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="chatgpt",
        credential_origin="path:/keep-chatgpt",
    )
    session.commit()

    response = _post_manifest(
        client,
        {
            "sidecar_id": "alpha",
            "entries": [],
            "completed_providers": ["anthropic"],
        },
    )

    assert response.status_code == 200
    assert response.json()["entries_pruned"] == 1
    assert (
        PendingCredentialTagRepo.get(
            session,
            sidecar_id="alpha",
            provider_id="anthropic",
            credential_origin="path:/keep-anthropic",
        )
        is None
    )
    assert (
        PendingCredentialTagRepo.get(
            session,
            sidecar_id="alpha",
            provider_id="chatgpt",
            credential_origin="path:/keep-chatgpt",
        )
        is not None
    )


def test_legacy_manifest_without_completion_list_does_not_prune(
    client: TestClient, session: Session
):
    from app.services.credential_tags import PendingCredentialTagRepo

    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="anthropic",
        credential_origin="path:/keep",
    )
    session.commit()

    response = _post_manifest(client, {"sidecar_id": "alpha", "entries": []})

    assert response.status_code == 200
    assert response.json()["entries_pruned"] == 0
    assert (
        PendingCredentialTagRepo.get(
            session,
            sidecar_id="alpha",
            provider_id="anthropic",
            credential_origin="path:/keep",
        )
        is not None
    )


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


def test_manifest_returns_saved_provider_mapping_without_pending_origin(
    client: TestClient, session: Session
):
    """Saved mappings are included even after their pending origin is cleared."""
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="minimax",
        credential_origin="provider:minimax",
        account_id="alice@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    response = _post_manifest(
        client, {"sidecar_id": "alpha-host", "entries": [], "completed_providers": ["opencode"]}
    )

    assert response.status_code == 200
    assert response.json()["resolved"] == {"minimax": {"provider:minimax": "alice@example.com"}}

    other = _post_manifest(client, {"sidecar_id": "beta-host", "entries": []})
    assert other.status_code == 200
    assert other.json()["resolved"] == {}


def test_manifest_prunes_stale_pending_credential_origin(
    client: TestClient, session: Session
) -> None:
    """A complete empty manifest clears stale credential origins."""
    from app.services.credential_tags import PendingCredentialTagRepo

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    _add_live_sidecar(session, "alpha", "alpha-host")
    _add_live_sidecar(session, "beta", "beta-host")

    # Cycle 1: sidecar reports the synthetic origin.
    r1 = _post_manifest(
        client,
        {
            "sidecar_id": "alpha",
            "entries": [{"provider_id": "minimax", "credential_origin": "provider:minimax"}],
            "completed_providers": ["minimax"],
        },
    )
    assert r1.status_code == 200, r1.text
    # The provider is answered for, with no hints: the answer is complete per
    # provider, so the sidecar can tell "no hints" from "not answered" (#874).
    assert r1.json()["resolved"] == {"minimax": {}}
    assert PendingCredentialTagRepo.get(
        session,
        sidecar_id="alpha",
        provider_id="minimax",
        credential_origin="provider:minimax",
    )

    # Cycle 2: the source disappears from a complete manifest.
    r2 = _post_manifest(
        client,
        {"sidecar_id": "alpha", "entries": [], "completed_providers": ["minimax"]},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["entries_pruned"] == 1
    assert (
        PendingCredentialTagRepo.get(
            session,
            sidecar_id="alpha",
            provider_id="minimax",
            credential_origin="provider:minimax",
        )
        is None
    )


def test_manifest_replay_cannot_restore_old_pending_state(client, session):
    from app.services.credential_tags import PendingCredentialTagRepo

    old_body = {
        "sidecar_id": "replay-host",
        "completed_providers": ["anthropic"],
        "entries": [{"provider_id": "anthropic", "credential_origin": "path:/old"}],
    }
    timestamp, _ = _sign_manifest(json.dumps(old_body).encode())
    assert _post_manifest(client, old_body, ts=timestamp).status_code == 200
    new_body = {
        **old_body,
        "entries": [{"provider_id": "anthropic", "credential_origin": "path:/new"}],
    }
    assert _post_manifest(client, new_body).status_code == 200
    assert _post_manifest(client, old_body, ts=timestamp).status_code == 409
    assert {
        r.credential_origin
        for r in PendingCredentialTagRepo.list_all(session, sidecar_id="replay-host")
    } == {"path:/new"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,token_key",
    [("anthropic", "cookie_sessionKey"), ("anthropic_console", "session_cookie")],
)
async def test_claude_same_store_cookie_sources_survive_ingest_and_completed_manifest(
    client, session, monkeypatch, provider, token_key
):
    from app.services.credential_tags import CredentialTagRepo
    from app.services.token_cache import TokenCache

    cache = TokenCache()
    monkeypatch.setattr("app.api.endpoints.fleet_ingest.token_cache", cache)
    monkeypatch.setattr("app.api.endpoints.fleet_credentials.token_cache", cache)
    base = f"cookie:{provider}/chrome/Default/store"
    origins = [base + "#aaaaaaaaaaaa", base + "#bbbbbbbbbbbb"]
    CredentialTagRepo.set_tag(
        session,
        provider_id=provider,
        credential_origin=base,
        account_id="old@example.com",
        sidecar_id="cookie-host",
        set_by="operator",
    )
    session.commit()
    manifest = {
        "sidecar_id": "cookie-host",
        "entries": [{"provider_id": provider, "credential_origin": origin} for origin in origins],
        "completed_providers": [provider],
    }
    assert _post_manifest(client, manifest).status_code == 200
    payload = {
        "provider": "sidecar",
        "sidecar_id": "cookie-host",
        "metrics": [
            {
                "service_name": "Claude",
                "remaining": "Token",
                "unit": "oauth",
                "metadata": {
                    "provider_id": provider,
                    token_key: f"cookie-{index}",
                    "credential_origin": origin,
                    "identity_pending": True,
                },
            }
            for index, origin in enumerate(origins)
        ],
    }
    raw = json.dumps(payload).encode()
    ts, sig = _sign(raw)
    response = client.post(
        "/api/v1/fleet/ingest",
        content=raw,
        headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    candidates = await cache.get_pending_sources(provider)
    assert {c["credential_origin"] for c in candidates} == set(origins)
    assert len({c["account_slot"] for c in candidates}) == 2
    assert all(c["account_slot"] != "old@example.com" for c in candidates)
    assert _post_manifest(client, manifest).status_code == 200
    assert len(await cache.get_pending_sources(provider)) == 2
    manifest["entries"] = manifest["entries"][1:]
    assert _post_manifest(client, manifest).status_code == 200
    candidates = await cache.get_pending_sources(provider)
    assert [c["credential_origin"] for c in candidates] == origins[1:]
    rows = session.exec(
        select(CredentialSource).where(CredentialSource.provider_id == provider)
    ).all()
    assert [row.credential_origin for row in rows] == origins[1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "anthropic_console"])
@pytest.mark.parametrize("set_by", ["identity_verification", "operator"])
@pytest.mark.parametrize("reported_as", ["entries", "observations"])
async def test_completed_claude_manifest_keeps_reported_resolved_siblings(
    client, session, monkeypatch, provider, set_by, reported_as
):
    from app.services.collector_manager import CollectorManager
    from app.services.token_cache import TokenCache

    cache = TokenCache()
    manager = CollectorManager()
    monkeypatch.setattr("app.api.endpoints.fleet_credentials.token_cache", cache)
    monkeypatch.setattr("app.services.collector_manager.manager", manager)
    first = await _tagged_claude_manifest_source(
        session, cache, manager, provider, "aaaaaaaaaaaa", set_by
    )
    second = await _tagged_claude_manifest_source(
        session, cache, manager, provider, "bbbbbbbbbbbb", set_by
    )
    other_store = await _tagged_claude_manifest_source(
        session, cache, manager, provider, "cccccccccccc", set_by, profile="Other"
    )
    with Session(session.bind) as config_session:
        first_row = config_session.exec(
            select(CredentialSource).where(CredentialSource.source_id == first[1])
        ).one()
        first_row.enabled = False
        config_session.add(first_row)
        config_session.commit()
    session.expire_all()
    manager.set_credential_source_preference(provider, first[2], first[1], False, 3)
    payload = {
        "sidecar_id": "resolved-host",
        "completed_providers": [provider],
        reported_as: [
            {"provider_id": provider, "credential_origin": origin}
            for origin, _source, _account in (first, second)
        ],
    }
    assert _post_manifest(client, payload).status_code == 200
    assert {row.source_id for row in session.exec(select(CredentialSource)).all()} == {
        first[1],
        second[1],
        other_store[1],
    }
    assert {c["source_id"] for c in await cache.get_source_candidates(provider, first[2])} == {
        first[1],
        second[1],
        other_store[1],
    }
    # A missing observation is not authoritative if scanning did not complete, or for legacy clients.
    for completed in (None, [], [provider]):
        incomplete = {"sidecar_id": "resolved-host", "entries": []}
        if completed is not None:
            incomplete["completed_providers"] = completed
        assert _post_manifest(client, incomplete).status_code == 200
        assert len(session.exec(select(CredentialSource)).all()) == 3
    payload[reported_as] = payload[reported_as][:1]
    assert _post_manifest(client, payload).status_code == 200
    rows = session.exec(select(CredentialSource)).all()
    assert {row.source_id for row in rows} == {first[1], other_store[1]}
    assert next(row for row in rows if row.source_id == first[1]).enabled is False
    assert {c["source_id"] for c in await cache.get_source_candidates(provider, first[2])} == {
        first[1],
        other_store[1],
    }
    preferences = manager.credential_source_preferences[(provider, first[2])]
    assert preferences[first[1]] == (False, 3)
    assert other_store[1] in preferences and second[1] not in preferences


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "anthropic_console"])
async def test_failed_manifest_commit_retains_claude_cache_and_preferences(
    client, session, monkeypatch, provider
):
    from app.services.collector_manager import CollectorManager
    from app.services.token_cache import TokenCache

    cache = TokenCache()
    manager = CollectorManager()
    monkeypatch.setattr("app.api.endpoints.fleet_credentials.token_cache", cache)
    monkeypatch.setattr("app.services.collector_manager.manager", manager)
    origin, source_id, account = await _tagged_claude_manifest_source(
        session, cache, manager, provider, "aaaaaaaaaaaa", "identity_verification"
    )

    reported = await _tagged_claude_manifest_source(
        session, cache, manager, provider, "bbbbbbbbbbbb", "identity_verification"
    )

    def fail_commit():
        raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        _post_manifest(
            client,
            {
                "sidecar_id": "resolved-host",
                "observations": [{"provider_id": provider, "credential_origin": reported[0]}],
                "completed_providers": [provider],
            },
        )
    session.rollback()
    assert {row.source_id for row in session.exec(select(CredentialSource)).all()} == {
        source_id,
        reported[1],
    }
    assert {c["source_id"] for c in await cache.get_source_candidates(provider, account)} == {
        source_id,
        reported[1],
    }
    assert manager.credential_source_preferences[(provider, account)][source_id] == (True, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("assignment", ["identity_verification", "operator"])
async def test_native_oauth_changed_generation_reenters_pending_despite_old_verified_tag(
    client, session, monkeypatch, assignment
):
    from app.services.credential_sources import sidecar_source_id
    from app.services.credential_tags import CredentialTagRepo
    from app.services.token_cache import TokenCache

    cache = TokenCache()
    monkeypatch.setattr("app.api.endpoints.fleet_ingest.token_cache", cache)
    origin = "keychain:Claude Code-credentials"
    sid = sidecar_source_id("native-host", origin)
    session.add(
        CredentialSource(
            provider_id="anthropic",
            account_id="old@example.com",
            source_id=sid,
            source_type="keychain",
            source_label="Keychain entry",
            credential_origin=origin,
            sidecar_id="native-host",
        )
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin=origin,
        account_id="old@example.com",
        sidecar_id="native-host",
        set_by=assignment,
    )
    session.commit()
    await cache.store(
        "anthropic",
        {"oauth_token": "old", "oauth_read_only": "1"},
        account_id="old@example.com",
        source_id=sid,
        source="native-host",
        source_metadata={"sidecar_id": "native-host", "credential_origin": origin},
    )
    card = {
        "service_name": "Claude",
        "remaining": "Token",
        "unit": "oauth",
        "account_id": "old@example.com",
        "metadata": {
            "provider_id": "anthropic",
            "credential_origin": origin,
            "oauth_token": "new-account-token",
            "oauth_read_only": "1",
        },
    }
    for cycle in range(2):
        card["metadata"]["observation_cycle"] = cycle
        response = _ingest(client, "native-host", [card])
        assert response.status_code == 200
        pending = await cache.get_pending_sources("anthropic")
        if assignment == "operator":
            assert pending == []
            assigned = await cache.get_source_candidates("anthropic", "old@example.com")
            assert assigned[0]["tokens"]["oauth_token"] == "new-account-token"
        else:
            assert len(pending) == 1
            assert pending[0]["tokens"]["oauth_token"] == "new-account-token"
            assert pending[0]["account_slot"] == sid
            assert not await cache.get_source_candidates("anthropic", "old@example.com")


def test_manifest_answers_with_an_explicit_empty_bucket_for_a_parked_provider(
    client: TestClient, session: Session
):
    """The server's answer for a provider is complete.

    A parked mapping is not shipped, so the provider's bucket comes back
    empty — explicitly, not absent. A sidecar that reads "absent" as "no
    answer" keeps the hint it cached before the account was archived.
    """
    from app.services.credential_tags import CredentialTagRepo

    _add_archived_provider_config(
        session, provider_id="kimi_coding", account_id="retired@example.com"
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="kimi_coding",
        credential_origin="provider:kimi_coding",
        account_id="retired@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    result = _post_manifest(
        client,
        {
            "sidecar_id": "alpha-host",
            "entries": [
                {
                    "provider_id": "kimi_coding",
                    "credential_origin": "provider:kimi_coding",
                }
            ],
        },
    )
    assert result.status_code == 200, result.text
    resolved = result.json()["resolved"]
    assert "kimi_coding" in resolved
    assert resolved["kimi_coding"] == {}


def test_manifest_still_ships_a_live_mapping(client: TestClient, session: Session):
    """The explicit empty bucket must not swallow a live mapping."""
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="alice@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    result = _post_manifest(
        client,
        {
            "sidecar_id": "alpha-host",
            "entries": [
                {
                    "provider_id": "anthropic",
                    "credential_origin": "provider:anthropic",
                }
            ],
        },
    )
    assert result.status_code == 200, result.text
    resolved = result.json()["resolved"]
    assert resolved["anthropic"] == {"provider:anthropic": "alice@example.com"}


def test_manifest_answers_for_a_provider_the_sidecar_names(client: TestClient, session: Session):
    """The sidecar names the providers whose hints it holds.

    A provider whose tag was deleted has no tag row left to make it a
    candidate, yet the sidecar may still be holding its hint — so the
    server has to answer for it too, or the stale hint survives.
    """
    result = _post_manifest(
        client,
        {
            "sidecar_id": "alpha-host",
            "entries": [],
            "hint_providers": ["kimi_coding"],
        },
    )
    assert result.status_code == 200, result.text
    resolved = result.json()["resolved"]
    assert "kimi_coding" in resolved
    assert resolved["kimi_coding"] == {}


def test_manifest_caps_the_named_providers(client: TestClient, session: Session):
    """`hint_providers` is bounded like `observations`.

    Every id becomes one term in the query that collects the hints and one
    bucket in the response, so an unbounded list is a way to make the server
    build an arbitrarily large query.
    """
    result = _post_manifest(
        client,
        {
            "sidecar_id": "alpha-host",
            "entries": [],
            "hint_providers": [f"provider_{i}" for i in range(257)],
        },
    )
    assert result.status_code == 400, result.text


def test_manifest_answers_for_a_repeated_named_provider_once(client: TestClient, session: Session):
    """A repeated id is one provider, not two.

    The id becomes one term in the query that collects the hints, so a
    repeated id must not be counted twice.
    """
    result = _post_manifest(
        client,
        {
            "sidecar_id": "alpha-host",
            "entries": [],
            "hint_providers": ["kimi_coding", "kimi_coding"],
        },
    )
    assert result.status_code == 200, result.text
    resolved = result.json()["resolved"]
    assert resolved == {"kimi_coding": {}}
