"""Tests for /fleet/ingest reset_anchors response.

Phase 6: Implement _reset_anchors_for_sidecar(session) to return authoritative
reset_at per (provider, account, window_type) from LatestUsage rows.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import LatestUsage


@pytest.fixture
def session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    LatestUsage.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_empty_db_returns_empty_dict(session: Session):
    """No LatestUsage rows → returns empty dict."""
    from app.api.endpoints.fleet import _reset_anchors_for_sidecar

    result = _reset_anchors_for_sidecar(session)
    assert result == {}


def test_single_anthropic_weekly_card(session: Session):
    """One LatestUsage row with reset_at in future → included in output."""
    from app.api.endpoints.fleet import _reset_anchors_for_sidecar

    future_reset = datetime.now(UTC) + timedelta(days=7)
    card_json = json.dumps(
        {
            "reset_at": future_reset.isoformat(),
        }
    )

    row = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=card_json,
    )
    session.add(row)
    session.commit()

    result = _reset_anchors_for_sidecar(session)
    assert "anthropic" in result
    assert "weekly" in result["anthropic"]
    assert result["anthropic"]["weekly"] == future_reset.isoformat()


def test_multiple_window_types_same_provider(session: Session):
    """Multiple window types for same provider (session, weekly, variant) → only defaults."""
    from app.api.endpoints.fleet import _reset_anchors_for_sidecar

    now = datetime.now(UTC)
    session_reset = now + timedelta(hours=5)
    weekly_reset = now + timedelta(days=7)
    weekly_sonnet_reset = now + timedelta(days=6)  # Different reset_at for variant

    # session window
    row1 = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="session",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": session_reset.isoformat()}),
    )

    # weekly window (default)
    row2 = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": weekly_reset.isoformat()}),
    )

    # weekly_sonnet variant (should be skipped)
    row3 = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        variant="sonnet",
        model_id="sonnet",
        card_json=json.dumps({"reset_at": weekly_sonnet_reset.isoformat()}),
    )

    session.add(row1)
    session.add(row2)
    session.add(row3)
    session.commit()

    result = _reset_anchors_for_sidecar(session)
    assert result == {
        "anthropic": {
            "session": session_reset.isoformat(),
            "weekly": weekly_reset.isoformat(),
        }
    }


def test_multiple_providers(session: Session):
    """Cards from multiple providers → both top-level keys present."""
    from app.api.endpoints.fleet import _reset_anchors_for_sidecar

    now = datetime.now(UTC)
    anthropic_reset = now + timedelta(days=7)
    gemini_reset = now + timedelta(days=1)

    row1 = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": anthropic_reset.isoformat()}),
    )

    row2 = LatestUsage(
        provider_id="gemini",
        account_id="user@example.com",
        window_type="daily",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": gemini_reset.isoformat()}),
    )

    session.add(row1)
    session.add(row2)
    session.commit()

    result = _reset_anchors_for_sidecar(session)
    assert "anthropic" in result
    assert "gemini" in result
    assert result["anthropic"]["weekly"] == anthropic_reset.isoformat()
    assert result["gemini"]["daily"] == gemini_reset.isoformat()


def test_past_reset_at_excluded(session: Session):
    """Rows with reset_at in past → not included in output."""
    from app.api.endpoints.fleet import _reset_anchors_for_sidecar

    now = datetime.now(UTC)
    past_reset = now - timedelta(days=1)

    row = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": past_reset.isoformat()}),
    )
    session.add(row)
    session.commit()

    result = _reset_anchors_for_sidecar(session)
    assert result == {}


def test_null_reset_at_excluded(session: Session):
    """Rows with missing/null reset_at → not included in output."""
    from app.api.endpoints.fleet import _reset_anchors_for_sidecar

    row = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=json.dumps({"some_field": "value"}),  # no reset_at
    )
    session.add(row)
    session.commit()

    result = _reset_anchors_for_sidecar(session)
    assert result == {}


def test_malformed_json_excluded(session: Session):
    """Rows with malformed card_json → skipped silently."""
    from app.api.endpoints.fleet import _reset_anchors_for_sidecar

    now = datetime.now(UTC)
    future_reset = now + timedelta(days=7)

    # Good row
    row1 = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": future_reset.isoformat()}),
    )

    # Bad row (invalid JSON)
    row2 = LatestUsage(
        provider_id="chatgpt",
        account_id="user@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json="not valid json {",
    )

    session.add(row1)
    session.add(row2)
    session.commit()

    result = _reset_anchors_for_sidecar(session)
    # Only the good row should be in output
    assert "anthropic" in result
    assert "chatgpt" not in result


def test_invalid_datetime_format_excluded(session: Session):
    """Rows with malformed datetime in reset_at → skipped silently."""
    from app.api.endpoints.fleet import _reset_anchors_for_sidecar

    now = datetime.now(UTC)
    future_reset = now + timedelta(days=7)

    # Good row
    row1 = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": future_reset.isoformat()}),
    )

    # Bad row (invalid datetime)
    row2 = LatestUsage(
        provider_id="chatgpt",
        account_id="user@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": "not-a-datetime"}),
    )

    session.add(row1)
    session.add(row2)
    session.commit()

    result = _reset_anchors_for_sidecar(session)
    # Only the good row should be in output
    assert "anthropic" in result
    assert "chatgpt" not in result


def test_multiple_rows_same_window_picks_max(session: Session):
    """Multiple rows for same (provider, window_type) with different reset_at → pick max."""
    from app.api.endpoints.fleet import _reset_anchors_for_sidecar

    now = datetime.now(UTC)
    early_reset = now + timedelta(days=6)
    late_reset = now + timedelta(days=7)

    # Two rows, same provider/window_type but different accounts
    # (they should share the same reset_at if it's truly authoritative)
    row1 = LatestUsage(
        provider_id="anthropic",
        account_id="user1@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": early_reset.isoformat()}),
    )

    row2 = LatestUsage(
        provider_id="anthropic",
        account_id="user2@example.com",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=json.dumps({"reset_at": late_reset.isoformat()}),
    )

    session.add(row1)
    session.add(row2)
    session.commit()

    result = _reset_anchors_for_sidecar(session)
    # Should pick the max (latest) reset_at
    assert result["anthropic"]["weekly"] == late_reset.isoformat()
