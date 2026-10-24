"""The un-merge puts a merged account back (#919).

A merge moves an account's history, its credential and its name to the
destination. The un-merge is the way back: the rows the merge moved go back
to the account they came from, the credential follows, and the merge record
is deleted — so the retired id names an account again.

What the merge destroyed stays destroyed: the duplicates it dropped and the
stored credential it wiped do not come back, and the preview says so instead
of losing them silently.
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
from app.services.account_merges import merged_accounts
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


def _preview(client: TestClient, provider_id: str, source_account_id: str):
    return client.post(
        "/api/v1/system/provider-account-unmerge/preview",
        json={"provider_id": provider_id, "source_account_id": source_account_id},
        headers=_admin_headers(),
    )


def _unmerge(
    client: TestClient, provider_id: str, source_account_id: str, confirm_collisions: bool = False
):
    return client.post(
        "/api/v1/system/provider-account-unmerge/apply",
        json={
            "provider_id": provider_id,
            "source_account_id": source_account_id,
            "confirm_collisions": confirm_collisions,
        },
        headers=_admin_headers(),
    )


def _account_ids(session: Session, model: object) -> list[str]:
    return [row.account_id for row in session.exec(select(model)).all()]


# ── The way back ──────────────────────────────────────────────────────────


def test_an_unmerge_puts_the_history_back(client, session):
    """The usage events the merge moved go back to the account they came from."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    session.add(
        UsageEvent(
            provider_id="openrouter",
            account_id="alice@example.com",
            event_id="evt-1",
            ts=datetime.now(UTC),
        )
    )
    session.commit()

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200
    session.expire_all()
    assert _account_ids(session, UsageEvent) == ["bob@example.com"]

    resp = _unmerge(client, "openrouter", "alice@example.com")
    assert resp.status_code == 200, resp.text

    session.expire_all()
    assert _account_ids(session, UsageEvent) == ["alice@example.com"]
    # The merge record is gone: the retired id names an account again.
    assert session.exec(select(ProviderAccountMerge)).all() == []


def test_an_unmerge_puts_the_credential_back(client, session):
    """The machine's credential, the operator's tag and the durable
    contribution follow the account back."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    session.add(
        CredentialSource(
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="host-a:openrouter",
            source_type="sidecar",
            source_label="Host A",
        )
    )
    session.add(
        CredentialTag(
            provider_id="openrouter",
            account_id="alice@example.com",
            credential_origin="env:OPENROUTER_API_KEY",
        )
    )
    session.add(
        SidecarSourceContribution(
            device_id="dev-01",
            provider_id="openrouter",
            credential_source_id="host-a:openrouter",
            origin="env:OPENROUTER_API_KEY",
            account_id="alice@example.com",
        )
    )
    session.commit()

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200
    session.expire_all()
    assert _account_ids(session, CredentialSource) == ["bob@example.com"]
    assert _account_ids(session, CredentialTag) == ["bob@example.com"]
    assert _account_ids(session, SidecarSourceContribution) == ["bob@example.com"]

    assert _unmerge(client, "openrouter", "alice@example.com").status_code == 200

    session.expire_all()
    assert _account_ids(session, CredentialSource) == ["alice@example.com"]
    assert _account_ids(session, CredentialTag) == ["alice@example.com"]
    assert _account_ids(session, SidecarSourceContribution) == ["alice@example.com"]


def test_an_unmerge_puts_the_provider_row_back(client, session):
    """The merge renamed the account's row to the destination; the un-merge
    renames it back — the account comes back with the credential it had."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200
    session.expire_all()
    assert _account_ids(session, ProviderConfig) == ["bob@example.com"]

    assert _unmerge(client, "openrouter", "alice@example.com").status_code == 200

    session.expire_all()
    assert _account_ids(session, ProviderConfig) == ["alice@example.com"]


def test_an_unmerge_restores_an_archived_provider_row(client, session):
    """With a destination row, the merge archived the source's row instead of
    renaming it. The un-merge puts the flags back as they were before the
    merge."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")

    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200
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

    assert _unmerge(client, "openrouter", "alice@example.com").status_code == 200

    session.expire_all()
    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "alice@example.com",
        )
    ).first()
    assert row is not None
    assert row.archived is False
    assert row.enabled is True


def test_an_account_merged_away_is_not_merged_away_any_more(client, session):
    """The record of the merge is what makes an account merged away. Without
    it the id names an account again — and the guard that refused to restore
    the account stops refusing."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200
    assert [merged.account_id for merged in merged_accounts(session)] == ["alice@example.com"]

    assert _unmerge(client, "openrouter", "alice@example.com").status_code == 200

    session.expire_all()
    assert merged_accounts(session) == []
    resp = client.put(
        "/api/v1/system/provider-config/openrouter/alice@example.com",
        json={"archived": False},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text


# ── What the un-merge will not do ─────────────────────────────────────────


def test_the_preview_says_what_cannot_come_back(client, session):
    """What the merge destroyed stays destroyed — the preview says so instead
    of losing it silently."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _add_provider_config(session, provider_id="openrouter", account_id="bob@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    resp = _preview(client, "openrouter", "alice@example.com")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_account_id"] == "alice@example.com"
    assert body["destination_account_id"] == "bob@example.com"
    assert body["config"] == "archived"
    # The stored credential the merge wiped does not come back.
    assert any("wiped" in item for item in body["not_restored"])


def test_the_preview_says_when_the_provider_row_is_gone(client, session):
    """The operator may have deleted the account's provider row since the
    merge. The preview must not claim a restoration that cannot happen."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "openrouter",
            ProviderConfig.account_id == "bob@example.com",
        )
    ).first()
    assert row is not None
    session.delete(row)
    session.commit()

    resp = _preview(client, "openrouter", "alice@example.com")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"] == "absent"
    assert any("provider row is gone" in item for item in body["not_restored"])


def test_an_account_id_that_was_taken_again_cannot_be_restored(client, session):
    """The merge renamed the account's provider row to the destination. If the
    id has been taken since, two accounts would answer to one id — the
    un-merge refuses, and refuses before it touches anything."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    # Someone took the account id again.
    session.add(
        ProviderConfig(provider_id="openrouter", account_id="alice@example.com", enabled=True)
    )
    session.commit()

    resp = _unmerge(client, "openrouter", "alice@example.com")
    assert resp.status_code == 409, resp.text
    assert "taken again" in resp.json()["detail"]

    # And nothing moved: the merge is still in place.
    session.expire_all()
    records = session.exec(select(ProviderAccountMerge)).all()
    assert [row.source_account_id for row in records] == ["alice@example.com"]


def test_a_merge_without_a_journal_cannot_be_reversed(client, session):
    """A merge performed before the merge kept a journal has nothing to
    reverse — the refusal is the answer, not a silent no-op."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    session.add(
        ProviderAccountMerge(
            provider_id="openrouter",
            source_account_id="alice@example.com",
            destination_account_id="bob@example.com",
        )
    )
    session.commit()

    resp = _unmerge(client, "openrouter", "alice@example.com")
    assert resp.status_code == 409, resp.text
    assert "journal" in resp.json()["detail"]


def test_an_unmerge_without_a_merge_record_is_a_404(client, session):
    resp = _unmerge(client, "openrouter", "alice@example.com")
    assert resp.status_code == 404, resp.text


def test_a_chain_is_unwound_from_the_end(client, session):
    """alice → bob → carol: undoing the first link would rewrite the chain
    twice, so the chain is unwound from the end."""
    for account_id in ("alice@example.com", "bob@example.com", "carol@example.com"):
        _add_provider_config(session, provider_id="openrouter", account_id=account_id)
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200
    assert _merge(client, "openrouter", "bob@example.com", "carol@example.com").status_code == 200

    resp = _unmerge(client, "openrouter", "alice@example.com")
    assert resp.status_code == 409, resp.text
    assert "from the end" in resp.json()["detail"]

    # The end of the chain goes first, and then the link before it.
    assert _unmerge(client, "openrouter", "bob@example.com").status_code == 200
    assert _unmerge(client, "openrouter", "alice@example.com").status_code == 200

    session.expire_all()
    assert session.exec(select(ProviderAccountMerge)).all() == []


# ── The rows that got in the way ──────────────────────────────────────────


def _seed_a_quota_card(session: Session, account_id: str) -> None:
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


def test_a_row_in_the_way_is_refused_without_an_explicit_confirmation(client, session):
    """An account recreated under the same id holds a row that would collide
    with a row the merge moved. The un-merge refuses until the operator
    confirms, and says what is in the way."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _seed_a_quota_card(session, "alice@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200

    # The account was recreated under the same id: it holds a quota card with
    # the same identity as the one the merge moved.
    _seed_a_quota_card(session, "alice@example.com")

    resp = _preview(client, "openrouter", "alice@example.com")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_collisions"] == 1
    assert resp.json()["rows_in_the_way"] == {"latest_usage": 1}

    resp = _unmerge(client, "openrouter", "alice@example.com")
    assert resp.status_code == 409, resp.text
    assert "collide" in resp.json()["detail"]


def test_a_confirmed_row_in_the_way_is_replaced_by_the_row_the_merge_moved(client, session):
    """The original history wins: the row that got in the way is dropped and
    the row the merge moved takes its place."""
    _add_provider_config(session, provider_id="openrouter", account_id="alice@example.com")
    _seed_a_quota_card(session, "alice@example.com")
    assert _merge(client, "openrouter", "alice@example.com", "bob@example.com").status_code == 200
    _seed_a_quota_card(session, "alice@example.com")

    resp = _unmerge(client, "openrouter", "alice@example.com", confirm_collisions=True)
    assert resp.status_code == 200, resp.text

    session.expire_all()
    rows = session.exec(
        select(LatestUsage).where(LatestUsage.account_id == "alice@example.com")
    ).all()
    assert len(rows) == 1
