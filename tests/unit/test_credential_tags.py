"""Unit tests for ``app/services/credential_tags.py``.

Pins the silent-listener resolution table contract:

- ``get`` / ``get_account_id`` lookup by ``(provider_id, credential_origin)``.
- ``set_tag`` is idempotent — re-tagging the same pair updates ``account_id``
  and ``set_at`` in place rather than inserting a duplicate (matches the
  ``UniqueConstraint`` on the model).
- ``delete_tag`` returns ``True`` only when a row was actually removed.
- ``list_by_provider`` / ``list_all`` return deterministic ordering for
  stable serialization (operators see the same UI every reload).
- ``list_pending_payload`` produces the ``/fleet/credentials/manifest``
  response shape — the sidecar consumes this on its next ``/fleet/config``
  to start stamping cards it couldn't resolve locally.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import CredentialTag
from app.services.credential_tags import CredentialTagRepo


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_get_returns_none_when_unset(session: Session):
    assert (
        CredentialTagRepo.get(session, provider_id="anthropic", credential_origin="path:/x") is None
    )
    assert (
        CredentialTagRepo.get_account_id(
            session, provider_id="anthropic", credential_origin="path:/x"
        )
        is None
    )


def test_set_tag_inserts_and_get_returns_row(session: Session):
    row = CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/home/alice/.claude/.credentials.json",
        account_id="alice@example.com",
        set_by="alice@example.com",
    )
    session.commit()

    fetched = CredentialTagRepo.get(
        session,
        provider_id="anthropic",
        credential_origin="path:/home/alice/.claude/.credentials.json",
    )
    assert fetched is not None
    assert fetched.id == row.id
    assert fetched.account_id == "alice@example.com"
    assert fetched.provider_id == "anthropic"
    assert fetched.credential_origin == "path:/home/alice/.claude/.credentials.json"
    assert fetched.set_by == "alice@example.com"


def test_set_tag_is_idempotent_on_same_pair(session: Session):
    """Re-tagging the same (provider, origin) updates in place, no duplicate row."""
    first = CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice@example.com",
    )
    session.commit()
    first_id = first.id

    second = CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice-work@example.com",
    )
    session.commit()

    assert second.id == first_id, "second set_tag must update in place, not insert a duplicate"
    assert second.account_id == "alice-work@example.com"

    all_rows = CredentialTagRepo.list_by_provider(session, provider_id="anthropic")
    assert len(all_rows) == 1


def test_unique_constraint_blocks_duplicate_insert(session: Session):
    """Direct insert of a duplicate (provider, origin) row fails the unique constraint.

    The repo's ``set_tag`` path doesn't exercise this because it upserts.
    This test guards against a future regression where someone bypasses the
    repo and gets a 500 from a raw duplicate INSERT.
    """
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice@example.com",
    )
    session.commit()

    duplicate = CredentialTag(
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="bob@example.com",
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()


def test_delete_tag_returns_true_when_removed(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice@example.com",
    )
    session.commit()

    assert (
        CredentialTagRepo.delete_tag(session, provider_id="anthropic", credential_origin="path:/x")
        is True
    )
    session.commit()
    assert (
        CredentialTagRepo.get(session, provider_id="anthropic", credential_origin="path:/x") is None
    )


def test_delete_tag_returns_false_when_missing(session: Session):
    assert (
        CredentialTagRepo.delete_tag(session, provider_id="anthropic", credential_origin="path:/x")
        is False
    )


def test_list_by_provider_returns_only_that_provider(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/a",
        account_id="alice@example.com",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="chatgpt",
        credential_origin="path:/c",
        account_id="alice@example.com",
    )
    session.commit()

    anthropic = CredentialTagRepo.list_by_provider(session, provider_id="anthropic")
    assert [r.credential_origin for r in anthropic] == ["path:/a"]
    chatgpt = CredentialTagRepo.list_by_provider(session, provider_id="chatgpt")
    assert [r.credential_origin for r in chatgpt] == ["path:/c"]


def test_list_all_orders_by_provider_then_origin(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="chatgpt",
        credential_origin="path:/z",
        account_id="alice@example.com",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/b",
        account_id="alice@example.com",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/a",
        account_id="alice@example.com",
    )
    session.commit()

    rows = CredentialTagRepo.list_all(session)
    assert [(r.provider_id, r.credential_origin) for r in rows] == [
        ("anthropic", "path:/a"),
        ("anthropic", "path:/b"),
        ("chatgpt", "path:/z"),
    ]


def test_list_pending_payload_returns_map_for_known_origins(session: Session):
    """``list_pending_payload`` is the manifest-endpoint response shape."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/home/alice/.claude/.credentials.json",
        account_id="alice@example.com",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="chatgpt",
        credential_origin="path:/home/alice/.codex/auth.json",
        account_id="alice@example.com",
    )
    session.commit()

    manifests = [
        {
            "provider_id": "anthropic",
            "credential_origins": [
                "path:/home/alice/.claude/.credentials.json",
                "path:/home/alice/.claude-work/.credentials.json",  # not tagged
            ],
        },
        {
            "provider_id": "chatgpt",
            "credential_origins": [
                "path:/home/alice/.codex/auth.json",
            ],
        },
    ]

    out = CredentialTagRepo.list_pending_payload(session, manifests=manifests)

    assert out == {
        "anthropic": {
            "path:/home/alice/.claude/.credentials.json": "alice@example.com",
        },
        "chatgpt": {
            "path:/home/alice/.codex/auth.json": "alice@example.com",
        },
    }


def test_list_pending_payload_handles_empty_input(session: Session):
    assert CredentialTagRepo.list_pending_payload(session, manifests=[]) == {}


def test_list_pending_payload_handles_manifests_with_no_origins(session: Session):
    """Defensive: a manifest that lists no origins for a provider is a no-op for that provider."""
    out = CredentialTagRepo.list_pending_payload(
        session,
        manifests=[{"provider_id": "anthropic", "credential_origins": []}],
    )
    assert out == {}
