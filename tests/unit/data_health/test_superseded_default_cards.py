"""Tests for the `superseded_default_cards` Data Health check and its maintenance service."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.models.db import LatestUsage, LatestUsageContribution, QuotaSnapshot
from app.services.data_health.checks.lone_default_events import LoneDefaultEventsCheck
from app.services.data_health.checks.superseded_default_cards import SupersededDefaultCardsCheck
from app.services.maintenance import superseded_default_cards as service
from tests.unit.data_health.conftest import make_config, make_latest_usage, make_snapshot

_ERROR_CARD = json.dumps({"data_source": "error", "remaining": "ERR"})


def _check() -> SupersededDefaultCardsCheck:
    return SupersededDefaultCardsCheck()


def _ago(hours: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


def _seed_duplicate(session, provider_id="opencode", *, default_age=48.0, real_age=0.0):
    """A stale `default` card next to a live real-account card (the prod shape)."""
    make_latest_usage(
        session,
        provider_id=provider_id,
        account_id="default",
        window_type="rolling",
        variant="Balance",
        updated_at=_ago(default_age),
    )
    make_latest_usage(
        session,
        provider_id=provider_id,
        account_id="alice@example.com",
        window_type="rolling",
        variant="Balance",
        updated_at=_ago(real_age),
    )


def test_detect_finds_a_stale_default_card_next_to_a_live_real_one(session):
    _seed_duplicate(session)

    report = _check().detect(session)

    assert report.total_count == 1
    group = report.groups[0]
    assert group.key == "opencode"
    assert group.fixable is True
    assert group.detail["live_accounts"] == ["alice@example.com"]


def test_detect_ignores_a_default_card_refreshed_within_the_hour(session):
    _seed_duplicate(session, default_age=0.5)

    assert _check().detect(session).total_count == 0


def test_detect_ignores_a_provider_with_no_live_real_account(session):
    """Nothing supersedes the default card: it may be the only data there is."""
    _seed_duplicate(session, real_age=5)

    assert _check().detect(session).total_count == 0


def test_detect_ignores_a_real_account_that_only_has_a_fresh_error_card(session):
    make_latest_usage(
        session,
        provider_id="github",
        account_id="default",
        window_type="monthly",
        updated_at=_ago(48),
    )
    make_latest_usage(
        session,
        provider_id="github",
        account_id="alice",
        window_type="monthly",
        card_json=_ERROR_CARD,
        updated_at=_ago(0),
    )

    assert _check().detect(session).total_count == 0


def test_detect_ignores_an_active_default_account(session):
    make_config(session, provider_id="opencode", account_id="default")
    _seed_duplicate(session)

    assert _check().detect(session).total_count == 0


@pytest.mark.parametrize("overrides", [{"enabled": False}, {"archived": True}])
def test_a_disabled_or_archived_default_account_does_not_protect_its_cards(session, overrides):
    """Same active-only test as `lone_default_events`: a retired default owns nothing."""
    make_config(session, provider_id="opencode", account_id="default", **overrides)
    _seed_duplicate(session)

    assert [g.key for g in _check().detect(session).groups] == ["opencode"]


def test_detect_reports_a_stale_default_error_card(session):
    """The GitHub shape: an error card left on `default`, real variants elsewhere."""
    make_latest_usage(
        session,
        provider_id="github",
        account_id="default",
        window_type="monthly",
        variant="default",
        card_json=_ERROR_CARD,
        updated_at=_ago(30),
    )
    make_latest_usage(
        session,
        provider_id="github",
        account_id="alice",
        window_type="monthly",
        variant="Chat",
        updated_at=_ago(0),
    )

    report = _check().detect(session)

    assert [g.key for g in report.groups] == ["github"]


def test_apply_deletes_only_the_default_cards_and_their_contributions(session):
    _seed_duplicate(session)
    session.add(
        LatestUsageContribution(
            provider_id="opencode",
            account_id="default",
            source_id="server:opencode",
            window_type="rolling",
            variant="Balance",
            model_id="",
            card_json="{}",
            updated_at=_ago(48),
        )
    )
    make_snapshot(session, provider_id="opencode", account_id="default", ts=_ago(60), pct_used=12.0)
    # Another provider's stale default card must survive the opencode apply.
    make_latest_usage(
        session,
        provider_id="deepseek",
        account_id="default",
        window_type="rolling",
        variant="Balance",
        updated_at=_ago(48),
    )
    session.commit()

    plan = _check().plan(session, "opencode", {})
    result, hooks = _check().apply(session, "opencode", {})
    session.commit()

    assert plan.counts == {"latest_usage": 1}
    assert result.counts == {"latest_usage_deleted": 1, "contributions_deleted": 1}
    assert hooks == []
    assert sorted(
        (r.provider_id, r.account_id) for r in session.exec(select(LatestUsage)).all()
    ) == [
        ("deepseek", "default"),
        ("opencode", "alice@example.com"),
    ]
    assert session.exec(select(LatestUsageContribution)).all() == []
    # History is not a card: the snapshot stays for `orphan_gauge_series`.
    assert len(session.exec(select(QuotaSnapshot)).all()) == 1


def test_plan_and_apply_refuse_a_card_that_came_back_to_life(session):
    _seed_duplicate(session)
    row = session.exec(select(LatestUsage).where(LatestUsage.account_id == "default")).one()
    row.updated_at = _ago(0)
    session.add(row)
    session.commit()

    with pytest.raises(ValueError, match="no superseded default cards"):
        _check().plan(session, "opencode", {})
    with pytest.raises(ValueError, match="no superseded default cards"):
        _check().apply(session, "opencode", {})
    assert len(session.exec(select(LatestUsage)).all()) == 2


def test_each_provider_is_its_own_group(session):
    _seed_duplicate(session, "opencode")
    _seed_duplicate(session, "deepseek")

    report = _check().detect(session)

    assert sorted(g.key for g in report.groups) == ["deepseek", "opencode"]


def test_apply_never_deletes_a_card_refreshed_after_the_plan_check(session, monkeypatch):
    """The DELETE itself carries the staleness rule, not only the read-time plan."""
    _seed_duplicate(session)
    make_latest_usage(
        session,
        provider_id="opencode",
        account_id="default",
        window_type="weekly",
        variant="Balance",
        updated_at=_ago(0),  # written between plan_delete and the DELETE
    )
    monkeypatch.setattr(service, "plan_delete", lambda *_a, **_k: None)

    result = service.apply_delete(session, provider_id="opencode")
    session.commit()

    assert result.latest_usage_deleted == 1
    assert result.snapshots_deleted == 0
    remaining = session.exec(select(LatestUsage).where(LatestUsage.account_id == "default")).all()
    assert [r.window_type for r in remaining] == ["weekly"]


def test_lone_default_events_waits_for_the_superseded_cards_fix():
    """Its fixer would merge the stale cards onto the live account."""
    assert "superseded_default_cards" in LoneDefaultEventsCheck.blocked_by
    assert SupersededDefaultCardsCheck.blocked_by == ()
