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


# ---------------------------------------------------------------------------
# auto_hints_for_single_account_providers — single-row heuristic that
# closes the MiniMax card-split. The repo method ships a
# ``provider:<provider_id>`` → ``account_id`` hint when exactly one
# enabled non-default row exists for a provider; the sidecar consumes
# the hint on its next cycle and stamps events onto the labeled quota
# card instead of the synthetic-default card.
# ---------------------------------------------------------------------------


def test_auto_hints_single_labeled_row_ships_hint(session: Session) -> None:
    """The MiniMax card-split scenario: one enabled non-default row."""
    from app.models.db import ProviderConfig

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    session.commit()

    out = CredentialTagRepo.auto_hints_for_single_account_providers(session, providers=["minimax"])
    assert out == {"minimax": {"provider:minimax": "s3ntin318@gmail.com"}}


def test_auto_hints_empty_when_no_rows(session: Session) -> None:
    assert (
        CredentialTagRepo.auto_hints_for_single_account_providers(session, providers=["minimax"])
        == {}
    )


def test_auto_hints_skips_default_only_account(session: Session) -> None:
    """A single ``account_id="default"`` row → no hint. The sidecar's
    events already land at ``("default")``; no benefit to retargeting
    them to themselves."""
    from app.models.db import ProviderConfig

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="default",
            enabled=True,
        )
    )
    session.commit()

    out = CredentialTagRepo.auto_hints_for_single_account_providers(session, providers=["minimax"])
    assert out == {}


def test_auto_hints_skips_multi_account_ambiguity(session: Session) -> None:
    """Two non-default rows for the same provider → no hint.

    The operator must tag explicitly via the Untagged Credentials
    dialog; the auto-hint heuristic refuses to guess which account
    the sidecar's events belong to."""
    from app.models.db import ProviderConfig

    for aid in ("alice@example.com", "bob@example.com"):
        session.add(
            ProviderConfig(
                provider_id="minimax",
                account_id=aid,
                enabled=True,
            )
        )
    session.commit()

    out = CredentialTagRepo.auto_hints_for_single_account_providers(session, providers=["minimax"])
    assert out == {}


def test_auto_hints_skips_disabled_labeled_row(session: Session) -> None:
    """A disabled row → no hint. The collector isn't running, so
    shipping events there would land on a card the user can't see."""
    from app.models.db import ProviderConfig

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=False,
        )
    )
    session.commit()

    out = CredentialTagRepo.auto_hints_for_single_account_providers(session, providers=["minimax"])
    assert out == {}


def test_auto_hints_independent_per_provider(session: Session) -> None:
    """Single-account hints are scoped per-provider — a single labeled
    MiniMax row fires only the MiniMax hint."""
    from app.models.db import ProviderConfig

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    session.commit()

    out = CredentialTagRepo.auto_hints_for_single_account_providers(
        session,
        providers=["minimax", "anthropic", "opencode"],
    )
    assert "minimax" in out
    assert "anthropic" not in out
    assert "opencode" not in out


def test_auto_hints_empty_providers_returns_empty(session: Session) -> None:
    """Defensive: empty input → empty output (caller treats it as no
    hints at all)."""
    assert CredentialTagRepo.auto_hints_for_single_account_providers(session, providers=[]) == {}


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


def test_auto_hints_suppressed_in_multi_sidecar_deployment(session: Session) -> None:
    """PR #318 round-2 review (Hermes warning #2): auto-hints are
    deployment-wide — they would cross-contaminate hosts in a multi-
    sidecar deployment. Gating: when ``sidecar_registry`` has 2+ rows
    with ``last_seen`` inside the last 7 days, the auto-hint is
    suppressed. Operators must tag explicitly via the Untagged
    Credentials dialog.

    PR #318 round-2 re-review S: only *live* rows count — rows default
    ``last_seen`` to now (so both fixtures here are live) and stale/retired
    rows age out of the window instead of suppressing forever; see
    ``test_auto_hints_not_suppressed_by_stale_sidecar_rows``.

    Tracking: ``credential_tags`` is planned to gain a ``sidecar_id``
    column (#319) which will let this heuristic resume
    per-host scoping. Until then, the multi-host deployment is the
    safer default — explicit tags never cross-contaminate.
    """
    from app.models.db import ProviderConfig, SidecarRegistry

    # Single-account setup that would normally fire the auto-hint.
    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    # Two sidecars registered — multi-host deployment. Both get the
    # default ``last_seen=now`` → both live within the 7-day window.
    session.add(SidecarRegistry(sidecar_id="alpha", hostname="alpha-host"))
    session.add(SidecarRegistry(sidecar_id="beta", hostname="beta-host"))
    session.commit()

    out = CredentialTagRepo.auto_hints_for_single_account_providers(session, providers=["minimax"])
    # Auto-hint suppressed because two LIVE sidecars are registered.
    assert out == {}


def test_auto_hints_not_suppressed_by_stale_sidecar_rows(session: Session) -> None:
    """PR #318 round-2 re-review S: stale/retired registry rows must not
    suppress the auto-hint forever.

    ``sidecar_registry`` rows are never pruned automatically (the only
    prune is the operator-triggered ``remove_inactive_sidecars_days``
    cleanup), so counting every row ever created meant a single-host
    operator who once ran a second machine lost the MiniMax auto-hint
    permanently with no log line. The gate now counts only rows whose
    ``last_seen`` is within 7 days — beyond the ~60s heartbeat cadence
    and the 60-min staleness threshold, but short enough that retired
    machines age out — and logs at DEBUG when it suppresses.
    """
    from datetime import UTC, datetime, timedelta

    from app.models.db import ProviderConfig, SidecarRegistry

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    # One live sidecar (default last_seen=now) + one retired machine
    # that last checked in 30 days ago. Only the live one counts →
    # single-host detection holds and the hint fires.
    session.add(SidecarRegistry(sidecar_id="alpha", hostname="alpha-host"))
    stale = SidecarRegistry(sidecar_id="beta", hostname="beta-host")
    stale.last_seen = datetime.now(UTC) - timedelta(days=30)
    session.add(stale)
    session.commit()

    out = CredentialTagRepo.auto_hints_for_single_account_providers(session, providers=["minimax"])
    assert out == {"minimax": {"provider:minimax": "s3ntin318@gmail.com"}}


def test_auto_hints_unaffected_when_only_one_sidecar(session: Session) -> None:
    """The single-host happy path: one sidecar registered, one labeled
    row → auto-hint fires (matches the pre-multi-host-gate contract)."""
    from app.models.db import ProviderConfig, SidecarRegistry

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    session.add(SidecarRegistry(sidecar_id="alpha", hostname="alpha-host"))
    session.commit()

    out = CredentialTagRepo.auto_hints_for_single_account_providers(session, providers=["minimax"])
    assert out == {"minimax": {"provider:minimax": "s3ntin318@gmail.com"}}
