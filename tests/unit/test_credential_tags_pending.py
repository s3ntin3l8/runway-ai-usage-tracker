"""Unit tests for PendingCredentialTagRepo lifecycle, stale cleanup, and quota previews.

PendingCredentialTagRepo — silent-listener reconcile table:
- Upsert updates in place, preserving first_seen.
- Multi-sidecar dimensions.
- Stale entry removal.
- Quota preview persistence, staleness marking, and cleanup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from app.services.credential_tags import (
    CredentialTagRepo,
    PendingCredentialTagRepo,
)
from tests.unit._credential_tags_helpers import session_fixture  # noqa: F401


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


def test_delete_for_sidecar_drops_only_that_sidecars_rows(session: Session):
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="alpha", provider_id="anthropic", credential_origin="path:/a"
    )
    PendingCredentialTagRepo.upsert(
        session, sidecar_id="beta", provider_id="anthropic", credential_origin="path:/a"
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/a",
        account_id="a@x.com",
        sidecar_id="alpha",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/a",
        account_id="shared@x.com",
    )
    session.commit()

    assert PendingCredentialTagRepo.delete_for_sidecar(session, sidecar_id="alpha") == 1
    assert CredentialTagRepo.delete_for_sidecar(session, sidecar_id="alpha") == 1
    session.commit()

    assert [r.sidecar_id for r in PendingCredentialTagRepo.list_all(session)] == ["beta"]
    # The deployment-wide row is untouched.
    assert [(t.sidecar_id, t.account_id) for t in CredentialTagRepo.list_all(session)] == [
        (None, "shared@x.com")
    ]


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


def test_pending_quota_preview_persists_only_safe_fields(session: Session):
    row = PendingCredentialTagRepo.set_quota_preview(
        session,
        sidecar_id="host-a",
        provider_id="antigravity",
        credential_origin="path:/auth.json",
        preview=[
            {
                "service_name": "Antigravity",
                "remaining": 7,
                "pct_used": 30,
                "oauth_token": "fake-token",
                "cookie_session": "fake-cookie",
                "metadata": {"refresh_token": "fake-refresh-token"},
            }
        ],
    )
    session.commit()
    session.refresh(row)

    preview, observed_at, stale = PendingCredentialTagRepo.read_quota_preview(row)

    assert preview == [{"service_name": "Antigravity", "remaining": 7, "pct_used": 30}]
    assert observed_at is not None
    assert stale is False
    assert "fake-token" not in (row.quota_preview_json or "")
    assert "fake-cookie" not in (row.quota_preview_json or "")
    assert "fake-refresh-token" not in (row.quota_preview_json or "")


def test_pending_quota_preview_expires_and_marks_stale(session: Session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PENDING_CREDENTIAL_PREVIEW_MAX_AGE_SECONDS", 60)
    observed_at = datetime.now(UTC) - timedelta(seconds=61)
    row = PendingCredentialTagRepo.set_quota_preview(
        session,
        sidecar_id="host-a",
        provider_id="antigravity",
        credential_origin="path:/auth.json",
        preview=[{"remaining": 7}],
        observed_at=observed_at,
    )
    session.commit()

    preview, timestamp, stale = PendingCredentialTagRepo.read_quota_preview(
        row, now=datetime.now(UTC)
    )

    assert preview == []
    assert timestamp == observed_at.isoformat()
    assert stale is True
    assert row.quota_preview_json is not None


def test_pending_quota_preview_cleanup_clears_expired_payloads(session: Session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "PENDING_CREDENTIAL_PREVIEW_MAX_AGE_SECONDS", 60)
    observed_at = datetime.now(UTC) - timedelta(seconds=61)
    row = PendingCredentialTagRepo.set_quota_preview(
        session,
        sidecar_id="host-a",
        provider_id="antigravity",
        credential_origin="path:/auth.json",
        preview=[{"remaining": 7}],
        observed_at=observed_at,
    )
    session.commit()

    cleared = PendingCredentialTagRepo.expire_quota_previews(session, now=datetime.now(UTC))
    session.commit()
    session.refresh(row)

    assert cleared == 1
    assert row.quota_preview_json is None
    assert row.quota_preview_observed_at == observed_at


def test_pending_quota_preview_is_not_written_after_source_is_tagged(session: Session):
    CredentialTagRepo.set_tag(
        session,
        provider_id="antigravity",
        credential_origin="path:/auth.json",
        account_id="alice@example.com",
        sidecar_id="host-a",
    )

    row = PendingCredentialTagRepo.set_quota_preview(
        session,
        sidecar_id="host-a",
        provider_id="antigravity",
        credential_origin="path:/auth.json",
        preview=[{"remaining": 7}],
    )

    assert row is None
    assert (
        PendingCredentialTagRepo.get(
            session,
            sidecar_id="host-a",
            provider_id="antigravity",
            credential_origin="path:/auth.json",
        )
        is None
    )
