"""Unit tests for credential rotation carry (#474).

A CLI re-login re-fingerprints a fingerprinted origin; when the base
location is unchanged and exactly one account was ever tagged there, the
manifest carries that binding to the new origin instead of reopening a
pending row (and withholding the token) for an operator who never moved
the credential.
"""

from __future__ import annotations

from sqlmodel import Session

from app.services.credential_tags import CredentialTagRepo
from tests.unit._credential_tags_helpers import session_fixture  # noqa: F401

_ROTATION_BASE = "path:/home/alice/.local/share/opencode/auth.json"
_OLD_FP = "0123456789ab"
_NEW_FP = "abcdef012345"


def test_rotation_carries_the_single_account_tagged_at_that_location(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#{_OLD_FP}",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    carried = CredentialTagRepo.inherited_account_for_rotation(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#{_NEW_FP}",
        sidecar_id="alpha",
    )
    assert carried == "alice@example.com"


def test_rotation_inherits_a_deployment_wide_tag(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#{_OLD_FP}",
        account_id="alice@example.com",
        sidecar_id=None,
    )
    carried = CredentialTagRepo.inherited_account_for_rotation(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#{_NEW_FP}",
        sidecar_id="alpha",
    )
    assert carried == "alice@example.com"


def test_rotation_refuses_when_two_accounts_ever_shared_the_location(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#{_OLD_FP}",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#000000000000",
        account_id="bob@example.com",
        sidecar_id=None,
    )
    carried = CredentialTagRepo.inherited_account_for_rotation(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#{_NEW_FP}",
        sidecar_id="alpha",
    )
    assert carried is None


def test_rotation_ignores_another_machines_tag(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#{_OLD_FP}",
        account_id="alice@example.com",
        sidecar_id="beta",
    )
    assert (
        CredentialTagRepo.inherited_account_for_rotation(
            session,
            provider_id="opencode",
            credential_origin=f"{_ROTATION_BASE}#{_NEW_FP}",
            sidecar_id="alpha",
        )
        is None
    )


def test_rotation_never_applies_to_an_unfingerprinted_origin(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=_ROTATION_BASE,
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    # Without a fingerprint suffix there is nothing to rotate against.
    assert (
        CredentialTagRepo.inherited_account_for_rotation(
            session,
            provider_id="opencode",
            credential_origin=f"{_ROTATION_BASE}#{_NEW_FP}",
            sidecar_id="alpha",
        )
        is None
    )


def test_rotation_is_vetoed_by_a_contradicting_reported_identity(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin=f"{_ROTATION_BASE}#{_OLD_FP}",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    assert (
        CredentialTagRepo.inherited_account_for_rotation(
            session,
            provider_id="anthropic",
            credential_origin=f"{_ROTATION_BASE}#{_NEW_FP}",
            sidecar_id="alpha",
            claimed_account_id="bob@example.com",
        )
        is None
    )
    # An agreeing claim does not veto.
    assert (
        CredentialTagRepo.inherited_account_for_rotation(
            session,
            provider_id="anthropic",
            credential_origin=f"{_ROTATION_BASE}#{_NEW_FP}",
            sidecar_id="alpha",
            claimed_account_id="alice@example.com",
        )
        == "alice@example.com"
    )


def test_rotation_does_not_match_look_alike_paths(session: Session):
    # A SQL LIKE prefix would treat "_" in the query path as a single-char
    # wildcard and let path:/a_b inherit path:/axb's binding.
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"path:/axb#{_OLD_FP}",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    assert (
        CredentialTagRepo.inherited_account_for_rotation(
            session,
            provider_id="opencode",
            credential_origin=f"path:/a_b#{_NEW_FP}",
            sidecar_id="alpha",
        )
        is None
    )


def test_rotation_carries_again_on_a_second_relogin(session: Session):
    """The carry is not one-shot: each re-login at this location inherits again."""
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#{_OLD_FP}",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    assert (
        CredentialTagRepo.inherited_account_for_rotation(
            session,
            provider_id="opencode",
            credential_origin=f"{_ROTATION_BASE}#{_NEW_FP}",
            sidecar_id="alpha",
        )
        == "alice@example.com"
    )
    # The carried row is itself evidence for the next rotation.
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"{_ROTATION_BASE}#{_NEW_FP}",
        account_id="alice@example.com",
        sidecar_id="alpha",
        set_by="rotation",
    )
    assert (
        CredentialTagRepo.inherited_account_for_rotation(
            session,
            provider_id="opencode",
            credential_origin=f"{_ROTATION_BASE}#000000000000",
            sidecar_id="alpha",
        )
        == "alice@example.com"
    )
