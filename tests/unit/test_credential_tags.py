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
- ``PendingCredentialTagRepo`` upserts and prunes per-sidecar so the
  fleet UI's "Untagged" panel reflects the sidecar's current local
  state, not stale history.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import CredentialTag
from app.services.credential_tags import (
    CredentialTagRepo,
    PendingCredentialTagRepo,
)


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
    """``list_pending_payload`` is the production read path used by
    ``_account_tag_hints_for_providers`` in ``app/api/endpoints/fleet.py``
    — the canonical ``account_tag_hints`` shape that ships in both
    ``/fleet/config`` and ``/fleet/ingest`` responses (PR #290 round-2
    review, Hermes suggestion #6: make the repo the canonical read
    path)."""
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
    # An untagged provider is requested too — must NOT appear in the
    # output (the silent-listener block guard treats missing hints the
    # same as no hints, blocking the card until the operator resolves
    # it).
    CredentialTagRepo.set_tag(
        session,
        provider_id="not-requested",
        credential_origin="path:/never-asked",
        account_id="ghost@example.com",
    )
    session.commit()

    out = CredentialTagRepo.list_pending_payload(
        session, providers=["anthropic", "chatgpt"]
    )

    assert out == {
        "anthropic": {
            "path:/home/alice/.claude/.credentials.json": "alice@example.com",
        },
        "chatgpt": {
            "path:/home/alice/.codex/auth.json": "alice@example.com",
        },
    }
    assert "not-requested" not in out, (
        "untagged provider scope — must NOT leak other providers' tags "
        "into a scoped read"
    )


def test_list_pending_payload_handles_empty_input(session: Session):
    assert CredentialTagRepo.list_pending_payload(session, providers=[]) == {}


def test_list_pending_payload_handles_unknown_provider_only(session: Session):
    """Defensive: requesting only providers that have no stored tags
    returns an empty map (the caller should treat missing hints the
    same way as no hints at all)."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/never-asked",
        account_id="alice@example.com",
    )
    session.commit()

    out = CredentialTagRepo.list_pending_payload(session, providers=["openai"])
    assert out == {}


# ---------------------------------------------------------------------------
# PendingCredentialTagRepo — silent-listener reconcile table
# ---------------------------------------------------------------------------


def test_pending_upsert_inserts_then_updates_in_place(session: Session):
    """Re-upserting the same (sidecar, provider, origin) refreshes ``last_seen``.

    Doesn't insert a duplicate: the unique constraint would block it, but
    more importantly the repo's job is update-in-place so the row's
    ``first_seen`` carries the original discovery time.
    """
    first = PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="anthropic",
        credential_origin="path:/x",
    )
    session.commit()
    first_id = first.id
    first_seen = first.first_seen
    original_last_seen = first.last_seen

    second = PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="anthropic",
        credential_origin="path:/x",
    )
    session.commit()

    assert second.id == first_id
    assert second.first_seen == first_seen  # preserves original
    assert second.last_seen >= original_last_seen


def test_pending_upsert_returns_pending_row_with_sidecar_dimension(session: Session):
    """Two sidecars reporting the same origin produce distinct pending rows."""
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="anthropic",
        credential_origin="path:/shared",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="beta-host",
        provider_id="anthropic",
        credential_origin="path:/shared",
    )
    session.commit()

    rows_alpha = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    rows_beta = PendingCredentialTagRepo.list_all(session, sidecar_id="beta-host")
    assert [r.credential_origin for r in rows_alpha] == ["path:/shared"]
    assert [r.credential_origin for r in rows_beta] == ["path:/shared"]


def test_pending_delete_stale_removes_only_dropped_entries(session: Session):
    """``delete_stale`` drops pending rows whose (provider, origin) is missing
    from the manifest-derived keep-map. Other sidecars / providers are untouched.
    """
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="anthropic",
        credential_origin="path:/stay",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="anthropic",
        credential_origin="path:/gone",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="chatgpt",
        credential_origin="path:/stay-chatgpt",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="beta-host",
        provider_id="anthropic",
        credential_origin="path:/beta-should-not-touch",
    )
    session.commit()

    removed = PendingCredentialTagRepo.delete_stale(
        session,
        sidecar_id="alpha-host",
        keep_origins_by_provider={
            "anthropic": {"path:/stay"},
            "chatgpt": {"path:/stay-chatgpt"},
        },
    )
    session.commit()

    assert removed == 1  # only path:/gone for alpha-host

    remaining_alpha = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    remaining_beta = PendingCredentialTagRepo.list_all(session, sidecar_id="beta-host")
    assert {r.credential_origin for r in remaining_alpha} == {"path:/stay", "path:/stay-chatgpt"}
    assert {r.credential_origin for r in remaining_beta} == {"path:/beta-should-not-touch"}


def test_pending_delete_stale_with_empty_keep_map_removes_everything(session: Session):
    """A sidecar that reports empty content every cycle finally clears its list."""
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="anthropic",
        credential_origin="path:/x",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="chatgpt",
        credential_origin="path:/y",
    )
    session.commit()

    removed = PendingCredentialTagRepo.delete_stale(
        session,
        sidecar_id="alpha-host",
        keep_origins_by_provider={},
    )
    session.commit()

    assert removed == 2
    assert PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host") == []


def test_pending_count_by_sidecar_aggregates(session: Session):
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="alpha", provider_id="anthropic", credential_origin="path:/a"
    )
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="alpha", provider_id="chatgpt", credential_origin="path:/b"
    )
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="beta", provider_id="anthropic", credential_origin="path:/c"
    )
    session.commit()

    counts = PendingCredentialTagRepo.pending_count_by_sidecar(session)
    assert counts == {"alpha": 2, "beta": 1}


def test_pending_delete_returns_true_when_present_false_when_absent(session: Session):
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="alpha", provider_id="anthropic", credential_origin="path:/x"
    )
    session.commit()

    # PR #290 round-2 review (CodeQL): separate the side-effecting
    # delete() call from the assert. CodeQL flagged the original
    # ``assert (...delete(...) is True)`` pattern because the function
    # call mutates DB state inside the assert expression.
    first_delete = PendingCredentialTagRepo.delete(
        session,
        sidecar_id="alpha",
        provider_id="anthropic",
        credential_origin="path:/x",
    )
    assert first_delete is True
    session.commit()
    second_delete = PendingCredentialTagRepo.delete(
        session,
        sidecar_id="alpha",
        provider_id="anthropic",
        credential_origin="path:/x",
    )
    assert second_delete is False
