"""Unit tests for CredentialTagRepo core CRUD, ordering, redirects, and parked mappings.

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
- Retagging and target provider redirects.
- Parked mappings and archived account pairs.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models.db import CredentialTag
from app.services.credential_tags import CredentialTagRepo
from tests.unit._credential_tags_helpers import session_fixture  # noqa: F401


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

    # PR #290 round-2 review (CodeQL): separate side-effecting delete_tag
    # from the assert expression. Same fix as
    # ``test_pending_delete_returns_true_when_present_false_when_absent``.
    delete_result = CredentialTagRepo.delete_tag(
        session, provider_id="anthropic", credential_origin="path:/x"
    )
    assert delete_result is True
    session.commit()
    get_result = CredentialTagRepo.get(
        session, provider_id="anthropic", credential_origin="path:/x"
    )
    assert get_result is None


def test_delete_tag_returns_false_when_missing(session: Session):
    delete_result = CredentialTagRepo.delete_tag(
        session, provider_id="anthropic", credential_origin="path:/x"
    )
    assert delete_result is False


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
    ``account_tag_hints_for_providers`` in ``app/api/endpoints/fleet_shared.py``
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

    out = CredentialTagRepo.list_pending_payload(session, providers=["anthropic", "chatgpt"])

    assert out == {
        "anthropic": {
            "path:/home/alice/.claude/.credentials.json": "alice@example.com",
        },
        "chatgpt": {
            "path:/home/alice/.codex/auth.json": "alice@example.com",
        },
    }
    assert "not-requested" not in out, (
        "untagged provider scope — must NOT leak other providers' tags into a scoped read"
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


def test_retagging_without_a_target_clears_a_previous_redirect(session: Session):
    kwargs = {
        "provider_id": "gemini",
        "credential_origin": "provider:gemini",
        "account_id": "me@example.com",
        "sidecar_id": "laptop",
    }
    CredentialTagRepo.set_tag(session, target_provider_id="antigravity", **kwargs)
    assert CredentialTagRepo.get_redirect(session, provider_id="gemini", sidecar_id="laptop")

    row = CredentialTagRepo.set_tag(session, **kwargs)

    assert row.target_provider_id is None
    assert (
        CredentialTagRepo.get_redirect(session, provider_id="gemini", sidecar_id="laptop") is None
    )


def test_real_account_ids_count_a_redirect_tag_under_its_target_provider(session: Session):
    from app.services.credential_sources import real_account_ids

    CredentialTagRepo.set_tag(
        session,
        provider_id="gemini",
        credential_origin="provider:gemini",
        account_id="me@example.com",
        sidecar_id="laptop",
        target_provider_id="antigravity",
    )

    assert "me@example.com" in real_account_ids(session, "antigravity")
    assert "me@example.com" not in real_account_ids(session, "gemini")


def test_archived_account_pairs_lists_only_archived_rows(session: Session):
    """The parked set: the (provider, account) pairs whose row is archived.

    Parked-ness is derived from ``ProviderConfig.archived`` — never stored on
    the tag — so archiving an account parks the mappings that point at it and
    restoring it revives them with no backfill.
    """
    from app.models.db import ProviderConfig
    from app.services.credential_tags import archived_account_pairs

    session.add(
        ProviderConfig(
            provider_id="kimi_coding",
            account_id="retired@example.com",
            enabled=False,
            archived=True,
        )
    )
    session.add(
        ProviderConfig(
            provider_id="kimi_coding",
            account_id="live@example.com",
            enabled=True,
            archived=False,
        )
    )
    session.add(
        ProviderConfig(
            provider_id="other",
            account_id="retired@example.com",
            enabled=False,
            archived=True,
        )
    )
    session.commit()

    assert archived_account_pairs(session, ["kimi_coding"]) == {
        ("kimi_coding", "retired@example.com")
    }
    # An empty provider list must not turn into a full-table scan.
    assert archived_account_pairs(session, []) == set()


def test_parked_mapping_resolves_its_seeded_providers_in_one_query(session: Session, monkeypatch):
    """The seeded provider set resolves in a single query, not one per provider.

    `/fleet/config` asks about every provider with a tag, so a per-provider
    lookup would turn one request into a query per provider.
    """
    from app.services.credentials import tags as credential_tags_module

    calls: list[tuple[str, ...]] = []
    original = credential_tags_module.archived_account_pairs

    def counting(session: Session, providers):  # noqa: ANN001 — mirrors the wrapped helper
        calls.append(tuple(providers))
        return original(session, providers)

    monkeypatch.setattr(credential_tags_module, "archived_account_pairs", counting)

    parked = credential_tags_module.ParkedMapping(session, ["anthropic", "kimi_coding", "gemini"])
    for provider_id, account_id in (
        ("anthropic", "a@example.com"),
        ("kimi_coding", "b@example.com"),
        ("gemini", "c@example.com"),
    ):
        assert parked.is_parked_account(provider_id, account_id) is False

    assert len(calls) == 1
    assert set(calls[0]) == {"anthropic", "kimi_coding", "gemini"}


def test_parked_mapping_asks_no_query_when_nobody_looks(session: Session, monkeypatch):
    """A reader that never asks pays no query at all."""
    from app.services.credentials import tags as credential_tags_module

    calls: list[tuple[str, ...]] = []
    original = credential_tags_module.archived_account_pairs

    def counting(session: Session, providers):  # noqa: ANN001 — mirrors the wrapped helper
        calls.append(tuple(providers))
        return original(session, providers)

    monkeypatch.setattr(credential_tags_module, "archived_account_pairs", counting)

    credential_tags_module.ParkedMapping(session, ["anthropic"])
    assert calls == []


def test_parked_mapping_matches_a_legacy_non_canonical_account_row(session: Session):
    """A legacy config row whose id was never canonicalised still matches.

    Every write path stores an account in the canonical form (see
    ``canonical_account_id``), so the archived set and the tag have to be
    compared in that form too — otherwise a mapping onto a legacy row would
    keep shipping and the retired account would keep being stamped.
    """
    from app.models.db import ProviderConfig
    from app.services.credential_tags import ParkedMapping

    session.add(
        ProviderConfig(
            provider_id="kimi_coding",
            account_id="Alice@Example.com",
            enabled=False,
            archived=True,
        )
    )
    session.add(
        CredentialTag(
            provider_id="kimi_coding",
            credential_origin="provider:kimi_coding",
            account_id="alice@example.com",
            set_by="operator",
        )
    )
    session.commit()

    parked = ParkedMapping(session)
    tag = CredentialTagRepo.get(
        session, provider_id="kimi_coding", credential_origin="provider:kimi_coding"
    )
    assert tag is not None
    assert parked.is_parked(tag) is True
    assert parked.is_parked_account("kimi_coding", "alice@example.com") is True
    assert parked.is_parked_account("kimi_coding", "live@example.com") is False
