"""The account merge moves the credential with the account (#903).

The merge moves an account's history, credential sources and tags to the
destination and retires the source id. The machine, however, keeps resolving
its credential to the source id and pushes it every cycle — so without a
record of the move the source account comes back, with a live credential, and
the destination is left with a credential-source row it cannot collect with.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.db import get_session
from app.main import app
from app.models.db import (
    CredentialSource,
    CredentialTag,
    LatestUsage,
    ProviderAccountMerge,
    ProviderConfig,
    SidecarSourceContribution,
    UsageEvent,
)
from app.services.account_merges import load_journal
from tests.fixtures.test_client import TestClient


@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    os.close(fd)
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()
        os.remove(db_path)


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    from app.services.token_cache import token_cache

    token_cache._cache.clear()
    token_cache._source_cache.clear()
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()
    token_cache._cache.clear()
    token_cache._source_cache.clear()


TEST_TOKEN = "sk-test-merged"  # pragma: allowlist secret


def _admin_headers() -> dict[str, str]:
    from app.core.config import settings

    if settings.ADMIN_API_KEY:
        return {"X-Admin-Key": settings.ADMIN_API_KEY}
    return {}


def _add_provider_config(
    session: Session, *, provider_id: str, account_id: str, enabled: bool = True
) -> ProviderConfig:
    row = ProviderConfig(provider_id=provider_id, account_id=account_id, enabled=enabled)
    session.add(row)
    session.commit()
    return row


def _merge(
    client: TestClient, provider_id: str, source_account_id: str, destination_account_id: str
):
    return client.post(
        "/api/v1/system/provider-account-merge/apply",
        json={
            "provider_id": provider_id,
            "source_account_id": source_account_id,
            "destination_account_id": destination_account_id,
        },
        headers=_admin_headers(),
    )


def _sign(body: bytes, secret: str, timestamp: str | None = None) -> tuple[str, str]:
    import hashlib
    import hmac
    import time

    ts = timestamp or str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    return ts, sig


def _ingest_token(client: TestClient, secret: str, provider: str, account_id: str, origin: str):
    """Push a token card the way a sidecar reports one."""
    import json

    card = {
        "service_name": provider,
        "remaining": "Token",
        "unit": "api_key",
        "provider_id": provider,
        "account_id": account_id,
        "metadata": {
            "provider_id": provider,
            "api_key": TEST_TOKEN,
            "credential_origin": origin,
            "account_id": account_id,
        },
    }
    body = json.dumps({"provider": "sidecar", "sidecar_id": "dev-01", "metrics": [card]}).encode()
    ts, sig = _sign(body, secret)
    return client.post(
        "/api/v1/fleet/ingest",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
    )


@pytest.fixture
def ingest_secret(monkeypatch):
    """Pin INGEST_API_KEY so the sidecar push is accepted."""
    secret = "test-sidecar-secret-for-merge"  # pragma: allowlist secret
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", secret)
    return secret


def test_a_merge_records_where_the_source_account_went(client, session):
    """The record is what tells the ingest that the source id is retired."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")

    resp = _merge(client, "openrouter", "alice@example.com", "bob@example.com")
    assert resp.status_code == 200, resp.text

    rows = session.exec(select(ProviderAccountMerge)).all()
    assert [(row.source_account_id, row.destination_account_id) for row in rows] == [
        ("alice@example.com", "bob@example.com")
    ]


def test_a_push_claiming_a_merged_account_is_filed_under_the_destination(
    client, session, ingest_secret
):
    """The machine keeps claiming the merged-away id — the credential follows
    the account the merge put it in, and the source id stays empty."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    resp = _ingest_token(
        client, ingest_secret, "openrouter", "alice@example.com", "path:/home/u/.opencode/auth.json"
    )
    assert resp.status_code == 200, resp.text

    session.expire_all()
    rows = session.exec(
        select(CredentialSource).where(CredentialSource.provider_id == "openrouter")
    ).all()
    # One row for the source, filed under the destination: no second row under
    # the retired id.
    assert [row.account_id for row in rows] == ["bob@example.com"]

    from app.services.token_cache import token_cache

    assert token_cache._cache.get("openrouter", {}).get("bob@example.com") is not None
    assert token_cache._cache.get("openrouter", {}).get("alice@example.com") is None


def test_a_merge_moves_the_cached_bundle_to_the_destination(client, session):
    """The cached credential follows the account, so the destination collects
    with it without waiting for the machine's next push."""
    import time

    from app.services.token_cache import token_cache

    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")
    # A credential the machine reported: the source bundle is what the
    # collector reads, and the source row is what the merge moves.
    session.add(
        CredentialSource(
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="sidecar:dev-01:opencode",
            source_type="file",
            source_label="auth.json",
            credential_origin="path:/home/u/.opencode/auth.json",
            sidecar_id="dev-01",
        )
    )
    session.commit()
    token_cache._source_cache.setdefault("openrouter", {}).setdefault("alice@example.com", {})[
        "sidecar:dev-01:opencode"
    ] = ({"api_key": TEST_TOKEN}, {"source": "sidecar"}, time.time())

    resp = _merge(client, "openrouter", "alice@example.com", "bob@example.com")
    assert resp.status_code == 200, resp.text

    moved = token_cache._source_cache.get("openrouter", {}).get("bob@example.com", {})
    assert "sidecar:dev-01:opencode" in moved
    # The retired id holds nothing.
    assert not token_cache._source_cache.get("openrouter", {}).get("alice@example.com")


def test_a_usage_card_claiming_a_merged_account_is_filed_under_the_destination(
    client, session, ingest_secret
):
    """The quota-card path is the same resurrection: a card claiming the
    merged-away id must land under the destination, not bring the retired
    account back into the fleet view."""
    import json

    from app.models.db import LatestUsage

    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    card = {
        "service_name": "openrouter",
        "provider_id": "openrouter",
        "account_id": "alice@example.com",
        "remaining": "1000",
        "unit": "tokens",
        "unit_type": "tokens",
        "data_source": "logs",
    }
    body = json.dumps({"provider": "sidecar", "sidecar_id": "dev-01", "metrics": [card]}).encode()
    ts, sig = _sign(body, ingest_secret)
    resp = client.post(
        "/api/v1/fleet/ingest",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text

    session.expire_all()
    accounts = {row.account_id for row in session.exec(select(LatestUsage)).all()}
    assert accounts == {"bob@example.com"}


def test_merging_into_a_merged_away_account_is_rejected(client, session):
    """alice → bob, then bob → alice would leave two records pointing at each
    other."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    resp = _merge(client, "openrouter", "bob@example.com", "alice@example.com")
    assert resp.status_code == 409, resp.text
    assert "merged away" in resp.json()["detail"]

    # And the source of a merge that was already merged away is refused too.
    resp = _merge(client, "openrouter", "alice@example.com", "carol@example.com")
    assert resp.status_code == 409, resp.text


def test_a_chain_of_merges_resolves_to_the_last_destination(client, session, ingest_secret):
    """alice → bob → carol: a machine still claiming alice lands in carol."""
    for account_id in ("alice@example.com", "bob@example.com", "carol@example.com"):
        _add_provider_config(session, provider_id="openrouter", account_id=account_id)

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200
    assert _merge(client, "openrouter", "bob@example.com", "carol@example.com").status_code == 200

    resp = _ingest_token(
        client, ingest_secret, "openrouter", "alice@example.com", "path:/home/u/.opencode/auth.json"
    )
    assert resp.status_code == 200, resp.text

    session.expire_all()
    rows = session.exec(
        select(CredentialSource).where(CredentialSource.provider_id == "openrouter")
    ).all()
    assert [row.account_id for row in rows] == ["carol@example.com"]


# ── The operator-facing surface (#912) ──────────────────────────────────────
# A merged-away account holds no credential and is not an account any more, so
# it disappears from every list the operator reads — nothing says where it
# went. These tests pin the surface that says it.


def _provider(payload: dict, provider_id: str = "openrouter") -> dict:
    return next(p for p in payload["providers"] if p["provider_id"] == provider_id)


def test_the_provider_configs_list_names_where_a_merged_account_went(client, session):
    """The account registry says where the account went, and the leftover row
    of the merged-away account says it too."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    resp = client.get("/api/v1/system/provider-configs", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    provider = _provider(resp.json())

    merged = provider["merged_accounts"]
    assert [(m["account_id"], m["destination_account_id"]) for m in merged] == [
        ("alice@example.com", "bob@example.com")
    ]
    assert merged[0]["merged_at"]

    # The row the merge left behind (archived, no credential) points at the
    # destination, so the UI can keep it out of the archived list.
    alice = next(a for a in provider["accounts"] if a["account_id"] == "alice@example.com")
    assert alice["merged_into"] == "bob@example.com"
    # An account that was never merged says nothing.
    bob = next(a for a in provider["accounts"] if a["account_id"] == "bob@example.com")
    assert bob["merged_into"] is None


def test_the_credential_inventory_lists_the_merged_accounts(client, session, monkeypatch):
    """The inventory holds no credential for a merged-away account, so the
    merges are a list of their own."""
    from app.services import credential_inventory

    monkeypatch.setattr(credential_inventory, "engine", session.get_bind())
    monkeypatch.setattr(credential_inventory, "_scan_server_credentials", lambda: ({}, set()))
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    resp = client.get("/api/v1/system/credentials", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    merged = resp.json()["merged_accounts"]
    assert [(m["provider_id"], m["account_id"], m["destination_account_id"]) for m in merged] == [
        ("openrouter", "alice@example.com", "bob@example.com")
    ]


def test_a_chain_of_merges_is_reported_at_its_end(client, session):
    """alice → bob → carol: the operator is told where the account ended up,
    not which hop it took."""
    for account_id in ("alice@example.com", "bob@example.com", "carol@example.com"):
        _add_provider_config(session, provider_id="openrouter", account_id=account_id)

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200
    assert _merge(client, "openrouter", "bob@example.com", "carol@example.com").status_code == 200

    resp = client.get("/api/v1/system/provider-configs", headers=_admin_headers())
    provider = _provider(resp.json())
    destinations = {
        m["account_id"]: m["destination_account_id"] for m in provider["merged_accounts"]
    }
    assert destinations["alice@example.com"] == "carol@example.com"
    assert destinations["bob@example.com"] == "carol@example.com"


def test_a_merged_account_cannot_be_restored(client, session):
    """Restoring it would resurrect a phantom: the merge moved its history
    and its credential to the destination."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    resp = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"archived": False},
        headers=_admin_headers(),
    )
    assert resp.status_code == 409, resp.text
    assert "bob@example.com" in resp.json()["detail"]

    session.expire_all()
    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "alice@example.com",
        )
    ).first()
    assert row is not None
    assert row.archived is True
    assert row.enabled is False


def test_a_merged_account_cannot_be_given_a_credential_again(client, session):
    """Any write that would make the retired id an account again is refused —
    not only the restore. A PUT that creates the row (the add-provider path)
    would otherwise bring the phantom back, with a live credential."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    for body in (
        {"api_key": "sk-test-merged"},  # pragma: allowlist secret
        {"enabled": True},
        {"archived": False},
    ):
        resp = client.put(
            "/api/v1/system/provider-config/openrouter/alice@example.com",
            json=body,
            headers=_admin_headers(),
        )
        assert resp.status_code == 409, (body, resp.text)
        assert "bob@example.com" in resp.json()["detail"]

    # The row is untouched: still archived, still without a credential.
    session.expire_all()
    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "alice@example.com",
        )
    ).first()
    assert row.archived is True
    assert row.enabled is False
    assert row.api_key_encrypted is None

    # An explicit archive is still accepted: it keeps the account inert.
    resp = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"archived": True},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text


def test_a_destination_account_can_still_be_archived_and_restored(client, session):
    """The account that was merged *into* is a normal account — the guard must
    not fire for it."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    resp = client.put(
        "/api/v1/system/provider-config/openrouter/bob@example.com",
        json={"archived": True},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text

    resp = client.put(
        "/api/v1/system/provider-config/openrouter/bob@example.com",
        json={"archived": False},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text


def test_the_merge_list_is_empty_when_nothing_was_merged(client):
    resp = client.get("/api/v1/system/provider-configs", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    assert _provider(resp.json())["merged_accounts"] == []


# ── The journal: what the merge moved (#919) ───────────────────────────────
# The rows are journalled by id: once they sit under the destination they
# cannot be told apart from the destination's own rows, so the merge has to
# say which ones it carried over.


def _seed_rows_for_the_journal(session: Session) -> dict[str, int]:
    event = UsageEvent(
        provider_id="openrouter",
        account_id="alice@example.com",
        event_id="evt-1",
        ts=datetime.now(UTC),
    )
    source = CredentialSource(
        provider_id="openrouter",
        account_id="alice@example.com",
        source_id="host-a:openrouter",
        source_type="sidecar",
        source_label="Host A",
    )
    tag = CredentialTag(
        provider_id="openrouter",
        account_id="alice@example.com",
        credential_origin="env:OPENROUTER_API_KEY",
    )
    contribution = SidecarSourceContribution(
        device_id="dev-01",
        provider_id="openrouter",
        credential_source_id="host-a:openrouter",
        origin="env:OPENROUTER_API_KEY",
        account_id="alice@example.com",
    )
    session.add(event)
    session.add(source)
    session.add(tag)
    session.add(contribution)
    session.commit()
    return {
        "event": event.id,
        "source": source.id,
        "tag": tag.id,
        "contribution": contribution.id,
    }


def test_the_merge_journals_what_it_moved(client, session):
    ids = _seed_rows_for_the_journal(session)
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    journal = load_journal(session, "openrouter", "alice@example.com")
    assert journal is not None
    assert journal.rows["usage_events"] == [ids["event"]]
    assert journal.credential_sources == ["host-a:openrouter"]
    assert journal.credential_tags == [ids["tag"]]
    assert journal.contributions == [ids["contribution"]]
    # The source had no destination config row, so its row was renamed.
    assert journal.config_outcome == "renamed"
    assert journal.config_row_id is not None
    assert journal.config_enabled is True


def test_the_journal_records_the_archived_config_outcome(client, session):
    """With a destination config row, the source's row is archived instead —
    the journal says which of the two happened."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    journal = load_journal(session, "openrouter", "alice@example.com")
    assert journal is not None
    assert journal.config_outcome == "archived"
    # The flags before the merge: the un-merge restores them, and it has to
    # tell the merge's own archive from an archive the operator did earlier.
    assert journal.config_enabled is True
    assert journal.config_archived is False


def test_the_journal_records_an_already_archived_source(client, session):
    """Merging an account the operator had already archived is reachable — the
    journal has to say which archive the row carried."""
    _add_provider_config(
        session, provider_id="openrouter", account_id="alice@example.com", enabled=False
    )
    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "alice@example.com",
        )
    ).first()
    row.archived = True
    session.commit()
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    journal = load_journal(session, "openrouter", "alice@example.com")
    assert journal is not None
    assert journal.config_archived is True


def test_the_journal_does_not_list_the_rows_the_merge_deleted(client, session):
    """The duplicates the operator confirmed dropping are gone; the journal
    says how many, not which — they cannot be brought back."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")
    # The same quota card under both accounts: the same window, variant and
    # model — the identity the merge deduplicates on.
    for account_id in ("alice@example.com", "bob@example.com"):
        session.add(
            LatestUsage(
                provider_id="openrouter",
                account_id=account_id,
                sidecar_id="local",
                window_type="monthly",
                variant="default",
                model_id="m1",
                card_json="{}",
            )
        )
    session.commit()

    resp = client.post(
        "/api/v1/system/provider-account-merge/apply",
        json={
            "provider_id": "openrouter",
            "source_account_id": "alice@example.com",
            "destination_account_id": "bob@example.com",
            "confirm_collisions": True,
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["collisions_removed"] == 1

    journal = load_journal(session, "openrouter", "alice@example.com")
    assert journal is not None
    assert journal.collisions_removed == 1
    assert journal.rows["latest_usage"] == []


# ---------------------------------------------------------------------------
# Upgrade-path guard
# ---------------------------------------------------------------------------


def test_deferred_columns_includes_the_merge_journal():
    """The journal column must be in _DEFERRED_COLUMNS for existing DB upgrades.

    Every other test builds a fresh database from the model, so nothing else
    covers this path: a typo here would only show up as "no such column" on
    an install that already has a database.
    """
    from app.core.db import _DEFERRED_COLUMNS

    assert ("provider_account_merges", "moved", "TEXT") in _DEFERRED_COLUMNS
