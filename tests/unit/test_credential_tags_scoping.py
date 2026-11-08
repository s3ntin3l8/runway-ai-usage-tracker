"""Unit tests for per-sidecar credential tag scoping and resolution precedence (#319).

credential_tags.sidecar_id:
- Scoped vs deployment-wide coexistence.
- Read precedence: scoped row wins over deployment-wide row.
- Idempotency within same scope.
- Unique constraint per scope.
- Scoped list_pending_payload.
- Scoped deletion.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models.db import CredentialTag
from app.services.credential_tags import CredentialTagRepo
from tests.unit._credential_tags_helpers import session_fixture  # noqa: F401


def test_delete_scoped_for_origin_keeps_deployment_row(session: Session):
    for sc in ("alpha", "beta"):
        CredentialTagRepo.set_tag(
            session,
            provider_id="anthropic",
            credential_origin="provider:anthropic",
            account_id=f"{sc}@x.com",
            sidecar_id=sc,
        )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="all@x.com",
    )
    session.commit()

    assert (
        CredentialTagRepo.delete_scoped_for_origin(
            session, provider_id="anthropic", credential_origin="provider:anthropic"
        )
        == 2
    )
    session.commit()
    assert (
        CredentialTagRepo.get_account_id(
            session,
            provider_id="anthropic",
            credential_origin="provider:anthropic",
            sidecar_id="alpha",
        )
        == "all@x.com"
    )


def test_delete_tag_in_scope_targets_one_row(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="alpha@x.com",
        sidecar_id="alpha",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="all@x.com",
    )
    session.commit()

    # None = the deployment-wide row only (not "every scope").
    assert CredentialTagRepo.delete_tag_in_scope(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        sidecar_id=None,
    )
    session.commit()
    assert [(t.sidecar_id, t.account_id) for t in CredentialTagRepo.list_all(session)] == [
        ("alpha", "alpha@x.com")
    ]
    assert not CredentialTagRepo.delete_tag_in_scope(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        sidecar_id=None,
    )


def test_set_tag_scoped_and_deployment_rows_are_distinct(session: Session):
    """A scoped row and a deployment-wide row for the same (provider,
    origin) coexist — the partial unique indexes allow both forms."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/shared",
        account_id="alice@example.com",
        sidecar_id=None,
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/shared",
        account_id="alice-work@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    rows = CredentialTagRepo.list_by_provider(session, provider_id="anthropic")
    assert len(rows) == 2
    scoped = [r for r in rows if r.sidecar_id == "alpha-host"]
    deployment = [r for r in rows if r.sidecar_id is None]
    assert len(scoped) == 1 and scoped[0].account_id == "alice-work@example.com"
    assert len(deployment) == 1 and deployment[0].account_id == "alice@example.com"


def test_get_scoped_row_wins_over_deployment_row(session: Session):
    """Read precedence: with ``sidecar_id`` given, the scoped row wins
    over a deployment-wide row for the same origin."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/shared",
        account_id="alice@example.com",
        sidecar_id=None,
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/shared",
        account_id="alice-work@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    # alpha's own scoped row wins.
    assert (
        CredentialTagRepo.get_account_id(
            session,
            provider_id="anthropic",
            credential_origin="path:/shared",
            sidecar_id="alpha-host",
        )
        == "alice-work@example.com"
    )
    # beta falls back to the deployment-wide row.
    assert (
        CredentialTagRepo.get_account_id(
            session,
            provider_id="anthropic",
            credential_origin="path:/shared",
            sidecar_id="beta-host",
        )
        == "alice@example.com"
    )
    # Unidentified reader sees only the deployment-wide row.
    assert (
        CredentialTagRepo.get_account_id(
            session,
            provider_id="anthropic",
            credential_origin="path:/shared",
            sidecar_id=None,
        )
        == "alice@example.com"
    )


def test_set_tag_idempotent_within_same_scope(session: Session):
    """Re-tagging the same (provider, origin, sidecar_id) updates in
    place — no duplicate row within one scope."""
    first = CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()
    first_id = first.id

    second = CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice-work@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    assert second.id == first_id
    assert second.account_id == "alice-work@example.com"
    rows = CredentialTagRepo.list_by_provider(session, provider_id="anthropic")
    assert len(rows) == 1


def test_unique_constraint_blocks_duplicate_scoped_insert(session: Session):
    """Direct insert of a second scoped row for the same
    (provider, origin, sidecar_id) fails the partial unique index."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    duplicate = CredentialTag(
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="bob@example.com",
        sidecar_id="alpha-host",
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_list_pending_payload_scoped_read(session: Session):
    """``list_pending_payload(sidecar_id=...)``: scoped + deployment rows
    ship (scoped winning on conflict); ``sidecar_id=None`` ships only
    deployment-wide rows."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/shared",
        account_id="alice@example.com",
        sidecar_id=None,
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/alpha-only",
        account_id="alice-work@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    # alpha sees both — shared origin falls back to the deployment row
    # (no scoped override exists for it here), alpha-only comes scoped.
    out_alpha = CredentialTagRepo.list_pending_payload(
        session, providers=["anthropic"], sidecar_id="alpha-host"
    )
    assert out_alpha == {
        "anthropic": {
            "path:/shared": "alice@example.com",
            "path:/alpha-only": "alice-work@example.com",
        }
    }
    # beta sees only the deployment-wide row.
    out_beta = CredentialTagRepo.list_pending_payload(
        session, providers=["anthropic"], sidecar_id="beta-host"
    )
    assert out_beta == {"anthropic": {"path:/shared": "alice@example.com"}}
    # Unidentified reader: deployment-wide only.
    out_none = CredentialTagRepo.list_pending_payload(
        session, providers=["anthropic"], sidecar_id=None
    )
    assert out_none == {"anthropic": {"path:/shared": "alice@example.com"}}


def test_list_pending_payload_scoped_overrides_deployment(session: Session):
    """When both a scoped and a deployment-wide row exist for the same
    origin, the scoped row wins in the payload."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/shared",
        account_id="alice@example.com",
        sidecar_id=None,
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/shared",
        account_id="alice-work@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    out = CredentialTagRepo.list_pending_payload(
        session, providers=["anthropic"], sidecar_id="alpha-host"
    )
    assert out == {"anthropic": {"path:/shared": "alice-work@example.com"}}


def test_delete_tag_scoped_leaves_deployment_row(session: Session):
    """``delete_tag(sidecar_id=...)`` removes only that sidecar's row."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice@example.com",
        sidecar_id=None,
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice-work@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    removed = CredentialTagRepo.delete_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        sidecar_id="alpha-host",
    )
    session.commit()
    assert removed is True

    remaining = CredentialTagRepo.list_by_provider(session, provider_id="anthropic")
    assert len(remaining) == 1
    assert remaining[0].sidecar_id is None


def test_delete_tag_without_sidecar_removes_all_scopes(session: Session):
    """``delete_tag(sidecar_id=None)`` removes every row for the pair —
    the "forget this origin entirely" cleanup."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice@example.com",
        sidecar_id=None,
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/x",
        account_id="alice-work@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    removed = CredentialTagRepo.delete_tag(
        session, provider_id="anthropic", credential_origin="path:/x"
    )
    session.commit()
    assert removed is True
    assert CredentialTagRepo.list_by_provider(session, provider_id="anthropic") == []
