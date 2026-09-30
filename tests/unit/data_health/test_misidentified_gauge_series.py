"""Tests for app/services/data_health/checks/misidentified_gauge_series.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.models.db import LatestUsage, QuotaSnapshot
from app.services.data_health.checks.misidentified_gauge_series import (
    MisidentifiedGaugeSeriesCheck,
)
from tests.unit.data_health.conftest import (
    make_config,
    make_event,
    make_latest_usage,
    make_snapshot,
    make_tag,
)


def _check() -> MisidentifiedGaugeSeriesCheck:
    return MisidentifiedGaugeSeriesCheck()


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


def test_detect_finds_gauge_only_account_with_no_evidence(session):
    """An account that exists only in latest_usage with no events/creds/config."""
    make_latest_usage(session, provider_id="github", account_id="s3ntin3l8@gmail.com")

    report = _check().detect(session)

    assert report.total_count == 1
    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.key == "github::s3ntin3l8@gmail.com"
    assert group.fixable is True
    assert group.not_fixable_reason is None


def test_detect_finds_account_with_only_quota_snapshots(session):
    """An account with only quota_snapshots rows and no evidence is flagged."""
    now = datetime.now(UTC)
    make_snapshot(session, provider_id="github", account_id="ghost@example.com", ts=now)

    report = _check().detect(session)

    assert report.total_count == 1
    assert report.groups[0].fixable is True


def test_detect_ignores_account_with_usage_events(session):
    """An account that has usage_events is NOT flagged, regardless of gauge data."""
    make_latest_usage(session, provider_id="github", account_id="real@example.com")
    make_event(
        session,
        event_id="1",
        provider_id="github",
        account_id="real@example.com",
        ts=datetime.now(UTC),
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_account_with_credential_tag(session):
    """An account tagged via credential_tags is NOT flagged."""
    make_latest_usage(session, provider_id="github", account_id="tagged@example.com")
    make_tag(
        session,
        provider_id="github",
        credential_origin="path:/home/user/.config/gh/hosts.yml",
        account_id="tagged@example.com",
    )

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_account_with_provider_config(session):
    """An account with a provider_configs row is NOT flagged."""
    make_latest_usage(session, provider_id="github", account_id="configured")
    make_config(session, provider_id="github", account_id="configured")

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_account_with_credential_source(session):
    """An account with a credential_source is NOT flagged."""
    from app.models.db import CredentialSource

    make_latest_usage(session, provider_id="github", account_id="sourced")
    src = CredentialSource(
        provider_id="github",
        account_id="sourced",
        source_id="sidecar:abc123",
        source_type="file",
        source_label="hosts.yml",
        sidecar_id="dev-01",
        enabled=True,
        priority=0,
    )
    session.add(src)
    session.commit()

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_default_account(session):
    """The generic 'default' account represents server-configured credentials
    (e.g. from .env without DB rows) and is never flagged as misidentified."""
    make_latest_usage(session, provider_id="github", account_id="default")
    now = datetime.now(UTC)
    make_snapshot(session, provider_id="github", account_id="default", ts=now)

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_ignores_account_with_provider_account_label(session):
    """An account that has an explicit label override is NOT flagged."""
    from app.models.db import ProviderAccountLabel

    make_latest_usage(session, provider_id="github", account_id="labeled")
    label_row = ProviderAccountLabel(
        provider_id="github",
        account_id="labeled",
        account_label="Work Account",
    )
    session.add(label_row)
    session.commit()

    report = _check().detect(session)

    assert report.total_count == 0


def test_detect_counts_both_latest_usage_and_snapshots(session):
    """Total count is sum of latest_usage rows + quota_snapshots rows."""
    now = datetime.now(UTC)
    make_latest_usage(session, provider_id="github", account_id="ghost@example.com")
    make_latest_usage(
        session,
        provider_id="github",
        account_id="ghost@example.com",
        window_type="weekly",
    )
    make_snapshot(session, provider_id="github", account_id="ghost@example.com", ts=now)
    make_snapshot(
        session,
        provider_id="github",
        account_id="ghost@example.com",
        ts=now - timedelta(minutes=5),
    )

    report = _check().detect(session)

    assert report.total_count == 4  # 2 latest_usage + 2 snapshots
    sample = report.groups[0].samples[0].detail
    assert sample["latest_usage_rows"] == 2
    assert sample["quota_snapshots_rows"] == 2


def test_detect_returns_zero_when_no_gauge_data(session):
    report = _check().detect(session)
    assert report.total_count == 0
    assert report.groups == []


def test_detect_finds_multiple_groups_independently(session):
    """Two misidentified accounts produce two separate groups."""
    make_latest_usage(session, provider_id="github", account_id="ghost1@example.com")
    make_latest_usage(session, provider_id="opencode", account_id="ghost2@example.com")

    report = _check().detect(session)

    assert report.total_count == 2
    keys = {g.key for g in report.groups}
    assert keys == {"github::ghost1@example.com", "opencode::ghost2@example.com"}


# ---------------------------------------------------------------------------
# plan()
# ---------------------------------------------------------------------------


def test_plan_returns_correct_counts(session):
    now = datetime.now(UTC)
    make_latest_usage(session, provider_id="github", account_id="ghost@example.com")
    make_snapshot(session, provider_id="github", account_id="ghost@example.com", ts=now)

    fix_plan = _check().plan(session, "github::ghost@example.com", {})

    assert fix_plan.counts["latest_usage"] == 1
    assert fix_plan.counts["quota_snapshots"] == 1
    assert "ghost@example.com" in fix_plan.summary
    assert (
        fix_plan.confirmation_text
        == "I confirm github/ghost@example.com has no real usage history."
    )


def test_plan_raises_when_evidence_appears(session):
    """plan() re-validates; if evidence has appeared since detect(), it raises."""
    make_latest_usage(session, provider_id="github", account_id="ghost@example.com")
    # Sneak in a usage event before plan() is called
    make_event(
        session,
        event_id="1",
        provider_id="github",
        account_id="ghost@example.com",
        ts=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="supporting evidence"):
        _check().plan(session, "github::ghost@example.com", {})


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------


def test_apply_raises_without_confirmation(session):
    """apply() requires explicit same_account_confirmed=True parameter."""
    make_latest_usage(session, provider_id="github", account_id="ghost@example.com")

    with pytest.raises(ValueError, match="Confirmation required"):
        _check().apply(session, "github::ghost@example.com", {})

    with pytest.raises(ValueError, match="Confirmation required"):
        _check().apply(session, "github::ghost@example.com", {"same_account_confirmed": False})


def test_apply_deletes_gauge_rows(session):
    now = datetime.now(UTC)
    make_latest_usage(session, provider_id="github", account_id="ghost@example.com")
    make_snapshot(session, provider_id="github", account_id="ghost@example.com", ts=now)

    result, hooks = _check().apply(
        session, "github::ghost@example.com", {"same_account_confirmed": True}
    )

    assert result.counts["latest_usage_deleted"] == 1
    assert result.counts["snapshots_deleted"] == 1
    assert hooks == []
    # Rows are gone
    assert (
        session.exec(
            select(LatestUsage).where(LatestUsage.account_id == "ghost@example.com")
        ).first()
        is None
    )
    assert (
        session.exec(
            select(QuotaSnapshot).where(QuotaSnapshot.account_id == "ghost@example.com")
        ).first()
        is None
    )


def test_apply_raises_when_evidence_appears(session):
    """apply() re-validates independently of plan()."""
    make_latest_usage(session, provider_id="github", account_id="ghost@example.com")
    make_event(
        session,
        event_id="1",
        provider_id="github",
        account_id="ghost@example.com",
        ts=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="supporting evidence"):
        _check().apply(session, "github::ghost@example.com", {"same_account_confirmed": True})


def test_apply_does_not_touch_sibling_accounts(session):
    """Deleting a misidentified account must not touch the legitimate sibling."""
    now = datetime.now(UTC)
    # Misidentified
    make_latest_usage(session, provider_id="github", account_id="ghost@example.com")
    make_snapshot(session, provider_id="github", account_id="ghost@example.com", ts=now)
    # Legitimate sibling
    make_latest_usage(session, provider_id="github", account_id="real-user")
    make_config(session, provider_id="github", account_id="real-user")
    make_snapshot(
        session, provider_id="github", account_id="real-user", ts=now - timedelta(seconds=1)
    )

    _check().apply(session, "github::ghost@example.com", {"same_account_confirmed": True})

    # Sibling rows survive
    remaining_lu = session.exec(
        select(LatestUsage).where(LatestUsage.account_id == "real-user")
    ).all()
    assert len(remaining_lu) == 1
    remaining_qs = session.exec(
        select(QuotaSnapshot).where(QuotaSnapshot.account_id == "real-user")
    ).all()
    assert len(remaining_qs) == 1


def test_apply_deletes_contributions(session):
    """Deleting a misidentified account must also drop its LatestUsageContribution rows."""
    from app.models.db import LatestUsageContribution

    make_latest_usage(session, provider_id="github", account_id="ghost@example.com")
    contrib = LatestUsageContribution(
        provider_id="github",
        account_id="ghost@example.com",
        source_id="local:default",
        source_type="api",
        source_label="Copilot",
        window_type="weekly",
        variant="default",
        model_id="copilot",
        card_json="{}",
        updated_at=datetime.now(UTC),
    )
    session.add(contrib)
    session.commit()

    result, _ = _check().apply(
        session, "github::ghost@example.com", {"same_account_confirmed": True}
    )

    assert result.counts["contributions_deleted"] == 1
    assert (
        session.exec(
            select(LatestUsageContribution).where(
                LatestUsageContribution.account_id == "ghost@example.com"
            )
        ).first()
        is None
    )


def test_detect_query_count_is_constant_regardless_of_pair_count(session, query_counter):
    """detect() must use bulk-aggregated queries (O(1) in DB roundtrips)
    rather than scaling N+1 queries with the number of accounts/pairs."""
    # Seed 10 separate misidentified accounts
    for i in range(10):
        make_latest_usage(session, provider_id="github", account_id=f"ghost_{i}@example.com")
        make_snapshot(
            session,
            provider_id="github",
            account_id=f"ghost_{i}@example.com",
            ts=datetime.now(UTC),
        )

    query_counter.reset()
    report = _check().detect(session)
    assert report.total_count == 20
    assert len(report.groups) == 10
    # Exactly 7 queries total (2 counts group_by + 5 distinct evidence sets)
    assert query_counter.count <= 8


# ---------------------------------------------------------------------------
# _has_evidence() direct tests
# ---------------------------------------------------------------------------


def test_has_evidence_truth_table(session):
    from app.models.db import CredentialSource, ProviderAccountLabel
    from app.services.data_health.checks.misidentified_gauge_series import _has_evidence

    # 1. Default account is always evidenced
    assert _has_evidence(session, "github", "default") is True
    assert _has_evidence(session, "github", "") is True

    # 2. No evidence -> False
    assert _has_evidence(session, "github", "target@example.com") is False

    # 3. UsageEvent -> True
    ev = make_event(
        session, event_id="ev_t1", provider_id="github", account_id="target@example.com"
    )
    assert _has_evidence(session, "github", "target@example.com") is True
    session.delete(ev)
    session.commit()

    # 4. CredentialSource -> True
    cs = CredentialSource(
        provider_id="github",
        account_id="target@example.com",
        source_id="src1",
        source_type="file",
        source_label="hosts.yml",
        sidecar_id="dev-01",
        enabled=True,
        priority=0,
    )
    session.add(cs)
    session.commit()
    assert _has_evidence(session, "github", "target@example.com") is True
    session.delete(cs)
    session.commit()

    # 5. CredentialTag -> True
    tag = make_tag(
        session,
        provider_id="github",
        credential_origin="orig1",
        account_id="target@example.com",
    )
    assert _has_evidence(session, "github", "target@example.com") is True
    session.delete(tag)
    session.commit()

    # 6. ProviderConfig -> True
    cfg = make_config(session, provider_id="github", account_id="target@example.com")
    assert _has_evidence(session, "github", "target@example.com") is True
    session.delete(cfg)
    session.commit()

    # 7. ProviderAccountLabel -> True
    pal = ProviderAccountLabel(
        provider_id="github", account_id="target@example.com", account_label="Work"
    )
    session.add(pal)
    session.commit()
    assert _has_evidence(session, "github", "target@example.com") is True
    session.delete(pal)
    session.commit()

    # 8. With precomputed evidence_pairs parameter
    pairs = {("github", "cached@example.com")}
    assert _has_evidence(session, "github", "cached@example.com", evidence_pairs=pairs) is True
    assert _has_evidence(session, "github", "not_cached@example.com", evidence_pairs=pairs) is False
