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
    """#319 replaces the PR #318 blanket suppression with a per-sidecar
    pending gate: auto-hints are deployment-wide candidates, so in a
    multi-host deployment they only reach a sidecar that has a
    ``pending_credential_tags`` row for ``provider:<pid>`` — the host
    that actually reported the credential. A host that never reported
    it never receives the hint (cross-host attribution block).

    Two LIVE sidecars registered; the requester has no pending row →
    no hint. See the sibling tests for the pending-row path and the
    unidentified-requester path.
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

    # Identified requester (alpha) but no pending row for provider:minimax.
    out = CredentialTagRepo.auto_hints_for_single_account_providers(
        session, providers=["minimax"], sidecar_id="alpha"
    )
    assert out == {}


def test_auto_hints_ships_to_sidecar_with_pending_row_in_multi_sidecar(session: Session) -> None:
    """Multi-host + identified requester WITH a pending
    ``provider:<pid>`` row → the hint ships (that host reported the
    credential; it's the one that needs the auto-resolution)."""
    from app.models.db import ProviderConfig, SidecarRegistry

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    session.add(SidecarRegistry(sidecar_id="alpha", hostname="alpha-host"))
    session.add(SidecarRegistry(sidecar_id="beta", hostname="beta-host"))
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="minimax",
        credential_origin="provider:minimax",
    )
    session.commit()

    out_alpha = CredentialTagRepo.auto_hints_for_single_account_providers(
        session, providers=["minimax"], sidecar_id="alpha"
    )
    assert out_alpha == {"minimax": {"provider:minimax": "s3ntin318@gmail.com"}}

    # beta never reported it → no hint for beta.
    out_beta = CredentialTagRepo.auto_hints_for_single_account_providers(
        session, providers=["minimax"], sidecar_id="beta"
    )
    assert out_beta == {}


def test_auto_hints_withheld_when_unidentified_in_multi_sidecar(session: Session) -> None:
    """Multi-host + unidentified requester (old sidecar binary that
    doesn't send ``?sidecar_id=``): ship nothing — the safe pre-#319
    behavior for multi-host deployments."""
    from app.models.db import ProviderConfig, SidecarRegistry

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    session.add(SidecarRegistry(sidecar_id="alpha", hostname="alpha-host"))
    session.add(SidecarRegistry(sidecar_id="beta", hostname="beta-host"))
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="minimax",
        credential_origin="provider:minimax",
    )
    session.commit()

    out = CredentialTagRepo.auto_hints_for_single_account_providers(
        session, providers=["minimax"], sidecar_id=None
    )
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
    machines age out — and logs at DEBUG when it withholds.
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
    row → auto-hint fires unconditionally (no pending row required —
    the MiniMax first-cycle fix for single-host deployments)."""
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


# ---------------------------------------------------------------------------
# Per-sidecar tag scoping (#319) — credential_tags.sidecar_id
# ---------------------------------------------------------------------------


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


def test_pending_delete_by_origin_clears_all_sidecars(session: Session):
    """``delete_by_origin`` removes every sidecar's pending row for a
    (provider, origin) pair — used by the dialog's deployment scope."""
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="alpha", provider_id="anthropic", credential_origin="path:/x"
    )
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="beta", provider_id="anthropic", credential_origin="path:/x"
    )
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="alpha", provider_id="chatgpt", credential_origin="path:/y"
    )
    session.commit()

    removed = PendingCredentialTagRepo.delete_by_origin(
        session, provider_id="anthropic", credential_origin="path:/x"
    )
    session.commit()

    assert removed == 2
    remaining = PendingCredentialTagRepo.list_all(session)
    assert [(r.sidecar_id, r.provider_id, r.credential_origin) for r in remaining] == [
        ("alpha", "chatgpt", "path:/y")
    ]


# ---------------------------------------------------------------------------
# _migrate_credential_tag_scoping (#319) — rebuild pre-#319 tables
# ---------------------------------------------------------------------------


def test_migrate_credential_tag_scoping_upgrades_legacy_table():
    """Functional upgrade: pre-#319 table (table-level UNIQUE on
    (provider_id, credential_origin), no sidecar_id) is rebuilt with the
    post-#319 shape; existing rows become deployment-wide (sidecar_id
    NULL). Idempotent on second run. Fresh DBs skip (no legacy constraint).
    """
    from sqlalchemy import text

    from app.core.db import _migrate_credential_tag_scoping

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with engine.connect() as conn:
        # Rebuild credential_tags as a pre-#319 legacy table.
        conn.execute(text("DROP TABLE credential_tags"))
        conn.execute(
            text(
                "CREATE TABLE credential_tags ("
                "id INTEGER PRIMARY KEY, provider_id VARCHAR NOT NULL, "
                "credential_origin VARCHAR NOT NULL, account_id VARCHAR NOT NULL, "
                "set_by VARCHAR NOT NULL, set_at TIMESTAMP NOT NULL, "
                "UNIQUE (provider_id, credential_origin) "
                "CONSTRAINT uq_credential_tag_identity)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO credential_tags "
                "(id, provider_id, credential_origin, account_id, set_by, set_at) "
                "VALUES (1, 'anthropic', 'path:/a', 'alice@example.com', "
                "'operator', '2026-01-01 00:00:00'),"
                "(2, 'minimax', 'provider:minimax', 's3ntin318@gmail.com', "
                "'operator', '2026-01-01 00:00:00')"
            )
        )
        conn.commit()

        _migrate_credential_tag_scoping(conn)

        # sidecar_id column exists; legacy rows are deployment-wide.
        rows = conn.execute(
            text("SELECT id, provider_id, account_id, sidecar_id FROM credential_tags ORDER BY id")
        ).fetchall()
        assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
            (1, "anthropic", "alice@example.com", None),
            (2, "minimax", "s3ntin318@gmail.com", None),
        ]

        # Legacy table constraint is gone; partial unique indexes present.
        table_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='credential_tags'")
        ).first()
        assert table_sql is not None and "uq_credential_tag_identity" not in table_sql[0]
        index_names = {r[1] for r in conn.execute(text("PRAGMA index_list(credential_tags)"))}
        assert "uq_credential_tag_deployment" in index_names
        assert "uq_credential_tag_sidecar" in index_names

        # Second run is a no-op (no legacy constraint in DDL).
        _migrate_credential_tag_scoping(conn)
        rows_again = conn.execute(
            text("SELECT id, sidecar_id FROM credential_tags ORDER BY id")
        ).fetchall()
        assert [(r[0], r[1]) for r in rows_again] == [(1, None), (2, None)]


def test_fresh_db_skips_credential_tag_scoping_migration():
    """create_all already builds the post-#319 shape — migration is a no-op."""
    from sqlalchemy import text

    from app.core.db import _migrate_credential_tag_scoping

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='credential_tags'")
        ).first()
        _migrate_credential_tag_scoping(conn)
        after = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='credential_tags'")
        ).first()
        assert before == after
        index_names = {r[1] for r in conn.execute(text("PRAGMA index_list(credential_tags)"))}
        assert "uq_credential_tag_deployment" in index_names
        assert "uq_credential_tag_sidecar" in index_names
