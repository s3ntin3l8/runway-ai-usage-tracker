"""Unit tests for CredentialTagRepo auto-hints heuristic for single-account providers.

auto_hints_for_single_account_providers — single-row heuristic that
closes the MiniMax card-split. The repo method ships a
``provider:<provider_id>`` → ``account_id`` hint when exactly one
enabled non-default row exists for a provider; the sidecar consumes
the hint on its next cycle and stamps events onto the labeled quota
card instead of the synthetic-default card.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from app.models.db import ProviderConfig, SidecarRegistry
from app.services.credential_tags import (
    CredentialTagRepo,
    PendingCredentialTagRepo,
)
from tests.unit._credential_tags_helpers import session_fixture  # noqa: F401


def test_auto_hints_single_labeled_row_ships_hint(session: Session) -> None:
    """The MiniMax card-split scenario: one enabled non-default row."""
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
