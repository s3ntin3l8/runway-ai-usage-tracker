"""Tests for app/services/maintenance/claude_billing_type.py.

The repair prices the Claude accounts that were created before detection
existed. It may only price the rows nobody decided about, and it may only
price them from what the account itself published.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import LatestUsage, ProviderConfig, SystemConfig
from app.services.maintenance.claude_billing_type import (
    apply_claude_billing_type,
    plan_claude_billing_type,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _account(
    session: Session,
    provider_id: str = "anthropic",
    account_id: str = "person@example.com",
    billing_type: str = "unknown",
    billing_type_source: str | None = None,
    poll_interval_seconds: int | None = None,
) -> ProviderConfig:
    row = ProviderConfig(
        provider_id=provider_id,
        account_id=account_id,
        account_label=account_id,
        billing_type=billing_type,
        billing_type_source=billing_type_source,
        poll_interval_seconds=poll_interval_seconds,
    )
    session.add(row)
    session.commit()
    return row


def _card(
    session: Session,
    provider_id: str,
    account_id: str,
    card: dict,
    *,
    window_type: str = "weekly",
    updated_at: datetime | None = None,
) -> LatestUsage:
    row = LatestUsage(
        provider_id=provider_id,
        account_id=account_id,
        window_type=window_type,
        variant="default",
        model_id="",
        card_json=json.dumps(card),
        updated_at=updated_at or datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    return row


def _quota_card(used: float = 25.0) -> dict:
    return {"unit_type": "percent", "used_value": used, "limit_value": 100.0}


def _spend_card() -> dict:
    return {"unit_type": "currency", "currency": "USD", "used_value": 5.0}


def test_the_repair_prices_the_accounts_nobody_decided_about():
    """An account created before detection existed is priced from what it published."""
    session = _session()
    try:
        _account(session, account_id="old@example.com")
        _card(session, "anthropic", "old@example.com", _quota_card())

        plan = plan_claude_billing_type(session)
        assert plan.total == 1
        assert [c.account_id for c in plan.priced] == ["old@example.com"]
        assert plan.priced[0].model == "subscription"
        assert plan.skipped == []
    finally:
        session.close()


def test_the_repair_leaves_the_operator_own_rows_alone():
    """A model somebody chose is not the repair's to change."""
    session = _session()
    try:
        _account(
            session,
            account_id="chosen@example.com",
            billing_type="pay_as_you_go",
            billing_type_source="operator",
        )
        _card(session, "anthropic", "chosen@example.com", _quota_card())

        plan = plan_claude_billing_type(session)
        assert plan.total == 0
        assert plan.priced == []
    finally:
        session.close()


def test_the_repair_prices_only_claude_accounts():
    """Another provider's accounts are not this repair's business."""
    session = _session()
    try:
        _account(session, provider_id="gemini", account_id="someone@example.com")
        _card(session, "gemini", "someone@example.com", _quota_card())

        plan = plan_claude_billing_type(session)
        assert plan.total == 0
    finally:
        session.close()


def test_an_account_that_published_nothing_is_skipped_not_guessed():
    """No published card is no proof."""
    session = _session()
    try:
        _account(session, account_id="silent@example.com")

        plan = plan_claude_billing_type(session)
        assert plan.total == 1
        assert plan.priced == []
        assert [c.account_id for c in plan.skipped] == ["silent@example.com"]
        assert plan.skipped[0].model is None
    finally:
        session.close()


def test_cards_that_say_nothing_are_skipped_not_guessed():
    """A card that counts tokens says nothing about how an account bills."""
    session = _session()
    try:
        _account(session, account_id="tokenish@example.com")
        _card(
            session,
            "anthropic",
            "tokenish@example.com",
            {"unit_type": "token", "used_value": 1000},
        )

        plan = plan_claude_billing_type(session)
        assert plan.priced == []
        assert [c.account_id for c in plan.skipped] == ["tokenish@example.com"]
    finally:
        session.close()


def test_a_card_that_names_its_evidence_prices_nothing():
    """A claude.ai balance does not say how the account bills."""
    session = _session()
    try:
        _account(session, account_id="balanced@example.com")
        _card(
            session,
            "anthropic",
            "balanced@example.com",
            {
                "unit_type": "currency",
                "currency": "USD",
                "used_value": 0.0,
                "limit_value": 42.0,
                "billing_evidence": "prepaid_credit",
            },
        )

        plan = plan_claude_billing_type(session)
        assert plan.total == 1
        assert plan.priced == []
        assert [c.account_id for c in plan.skipped] == ["balanced@example.com"]

        result = apply_claude_billing_type(session)
        assert result.priced == 0
        row = session.exec(select(ProviderConfig)).one()
        assert row.billing_type == "unknown"
        assert row.billing_type_source is None
    finally:
        session.close()


def test_the_repair_writes_the_model_and_says_who_wrote_it():
    """A detected model is recorded as detected, so it is never touched again."""
    session = _session()
    try:
        _account(session, account_id="old@example.com")
        _card(session, "anthropic", "old@example.com", _quota_card())

        result = apply_claude_billing_type(session)
        assert result.priced == 1
        assert result.skipped == 0

        row = session.exec(select(ProviderConfig)).one()
        assert row.billing_type == "subscription"
        assert row.billing_type_source == "detected"
    finally:
        session.close()


def test_the_repair_reads_the_account_own_published_cards():
    """A prepaid balance is spend, and a plan's window is a plan."""
    session = _session()
    try:
        _account(session, account_id="spender@example.com")
        _card(session, "anthropic", "spender@example.com", _spend_card())

        apply_claude_billing_type(session)

        row = session.exec(select(ProviderConfig)).one()
        assert row.billing_type == "pay_as_you_go"
    finally:
        session.close()


def test_the_repair_is_idempotent():
    """A second run finds nothing left to price."""
    session = _session()
    try:
        _account(session, account_id="old@example.com")
        _card(session, "anthropic", "old@example.com", _quota_card())

        assert apply_claude_billing_type(session).priced == 1
        assert apply_claude_billing_type(session).priced == 0

        plan = plan_claude_billing_type(session)
        assert plan.total == 0
    finally:
        session.close()


def test_the_operator_choice_during_the_repair_wins():
    """A row somebody decided about while the plan was read is left alone."""
    session = _session()
    try:
        row = _account(session, account_id="old@example.com")
        _card(session, "anthropic", "old@example.com", _quota_card())

        plan = plan_claude_billing_type(session)
        assert len(plan.priced) == 1

        # The operator prices the account themselves before the repair lands.
        row.billing_type = "pay_as_you_go"
        row.billing_type_source = "operator"
        session.add(row)
        session.commit()

        result = apply_claude_billing_type(session)
        assert result.priced == 0

        row = session.exec(select(ProviderConfig)).one()
        assert row.billing_type == "pay_as_you_go"
        assert row.billing_type_source == "operator"
    finally:
        session.close()


def test_the_repair_can_be_asked_about_one_provider():
    """A repair run for one provider leaves the others alone."""
    session = _session()
    try:
        _account(session, provider_id="anthropic", account_id="plan@example.com")
        _card(session, "anthropic", "plan@example.com", _quota_card())
        _account(session, provider_id="anthropic_console", account_id="wallet@example.com")
        _card(session, "anthropic_console", "wallet@example.com", _spend_card())

        apply_claude_billing_type(session, provider_id="anthropic_console")

        rows = {
            (row.provider_id, row.account_id): row
            for row in session.exec(select(ProviderConfig)).all()
        }
        assert rows[("anthropic_console", "wallet@example.com")].billing_type == "pay_as_you_go"
        assert rows[("anthropic", "plan@example.com")].billing_type == "unknown"
        assert rows[("anthropic", "plan@example.com")].billing_type_source is None
    finally:
        session.close()


def test_the_repair_never_reads_a_credential():
    """The evidence is what the account published, not what it signed in with."""
    session = _session()
    try:
        row = _account(session, account_id="old@example.com")
        _card(session, "anthropic", "old@example.com", _quota_card())

        plan = plan_claude_billing_type(session)
        assert plan.priced[0].model == "subscription"
        # Nothing about the credential is needed to reach that answer.
        assert row.billing_type == "unknown"
    finally:
        session.close()


def test_a_card_the_collector_marked_stale_prices_nothing():
    """A card from a credential that stopped working is not evidence."""
    session = _session()
    try:
        _account(session, account_id="broken@example.com")
        _card(
            session,
            "anthropic",
            "broken@example.com",
            {
                **_quota_card(),
                "stale": True,
                "collection_failing": True,
                "detail": "Collection failing: 401 invalid token",
            },
        )

        plan = plan_claude_billing_type(session)
        assert plan.total == 1
        assert plan.priced == []
        assert len(plan.skipped) == 1

        result = apply_claude_billing_type(session)
        assert result.priced == 0
        row = session.exec(select(ProviderConfig)).one()
        assert row.billing_type == "unknown"
        assert row.billing_type_source is None
    finally:
        session.close()


def test_a_card_that_was_never_refreshed_prices_nothing():
    """A card from an account the collector has not reached since is not evidence."""
    session = _session()
    try:
        _account(session, account_id="ancient@example.com")
        _card(
            session,
            "anthropic",
            "ancient@example.com",
            _quota_card(),
            updated_at=datetime.now(UTC) - timedelta(days=800),
        )

        plan = plan_claude_billing_type(session)
        assert plan.priced == []
        assert len(plan.skipped) == 1
    finally:
        session.close()


def test_an_older_plan_card_does_not_outrank_a_current_spend_card():
    """The most recent publication describes the account now."""
    session = _session()
    try:
        _account(session, account_id="switched@example.com")
        _card(
            session,
            "anthropic",
            "switched@example.com",
            _quota_card(),
            window_type="weekly",
            updated_at=datetime.now(UTC) - timedelta(days=2),
        )
        _card(
            session,
            "anthropic",
            "switched@example.com",
            _spend_card(),
            window_type="monthly",
        )

        plan = plan_claude_billing_type(session)
        assert [candidate.model for candidate in plan.priced] == ["pay_as_you_go"]
    finally:
        session.close()


def test_cards_from_one_publication_are_read_together():
    """One publication of both shapes reads the way the detector reads it."""
    session = _session()
    try:
        _account(session, account_id="both@example.com")
        now = datetime.now(UTC)
        _card(
            session,
            "anthropic",
            "both@example.com",
            _quota_card(),
            window_type="weekly",
            updated_at=now,
        )
        _card(
            session,
            "anthropic",
            "both@example.com",
            _spend_card(),
            window_type="monthly",
            updated_at=now - timedelta(seconds=5),
        )

        plan = plan_claude_billing_type(session)
        assert [candidate.model for candidate in plan.priced] == ["subscription"]
    finally:
        session.close()


def test_a_card_from_the_previous_collection_cycle_still_counts():
    """A card the collector has not refreshed *yet* is still the account's."""
    session = _session()
    try:
        _account(session, account_id="current@example.com")
        now = datetime.now(UTC)
        _card(
            session,
            "anthropic",
            "current@example.com",
            _quota_card(),
            window_type="weekly",
            updated_at=now - timedelta(minutes=15),
        )
        _card(
            session,
            "anthropic",
            "current@example.com",
            _spend_card(),
            window_type="monthly",
            updated_at=now,
        )

        plan = plan_claude_billing_type(session)
        assert [candidate.model for candidate in plan.priced] == ["subscription"]
    finally:
        session.close()


def test_a_card_several_cycles_behind_is_not_evidence():
    """A card the collector has stopped publishing describes a state left behind."""
    session = _session()
    try:
        _account(session, account_id="gone@example.com")
        now = datetime.now(UTC)
        _card(
            session,
            "anthropic",
            "gone@example.com",
            _quota_card(),
            window_type="weekly",
            updated_at=now - timedelta(minutes=45),
        )
        _card(
            session,
            "anthropic",
            "gone@example.com",
            _spend_card(),
            window_type="monthly",
            updated_at=now,
        )

        plan = plan_claude_billing_type(session)
        assert [candidate.model for candidate in plan.priced] == ["pay_as_you_go"]
    finally:
        session.close()


def test_the_window_follows_the_accounts_own_poll_interval():
    """A slowly polled account keeps its cards longer than the default."""
    session = _session()
    try:
        _account(session, account_id="slow@example.com", poll_interval_seconds=86_400)
        now = datetime.now(UTC)
        _card(
            session,
            "anthropic",
            "slow@example.com",
            _quota_card(),
            window_type="weekly",
            updated_at=now - timedelta(hours=1),
        )
        _card(
            session,
            "anthropic",
            "slow@example.com",
            _spend_card(),
            window_type="monthly",
            updated_at=now,
        )

        plan = plan_claude_billing_type(session)
        assert [candidate.model for candidate in plan.priced] == ["subscription"]
    finally:
        session.close()


def test_the_window_follows_the_fleet_wide_poll_interval():
    """The operator's fleet-wide interval applies when the account has none."""
    session = _session()
    try:
        session.add(SystemConfig(default_poll_interval_seconds=3600))
        session.commit()
        _account(session, account_id="fleet@example.com")
        now = datetime.now(UTC)
        _card(
            session,
            "anthropic",
            "fleet@example.com",
            _quota_card(),
            window_type="weekly",
            updated_at=now - timedelta(minutes=45),
        )
        _card(
            session,
            "anthropic",
            "fleet@example.com",
            _spend_card(),
            window_type="monthly",
            updated_at=now,
        )

        plan = plan_claude_billing_type(session)
        assert [candidate.model for candidate in plan.priced] == ["subscription"]
    finally:
        session.close()
