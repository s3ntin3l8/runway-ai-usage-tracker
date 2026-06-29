"""Integration tests for window-close detection at LatestUsage upsert time.

When the poller receives a card whose reset_at is strictly later than the
existing LatestUsage row's reset_at, the previous window should be closed
by calling close_window() and writing rows to usage_windows.

Tests exercise the helper _maybe_close_previous_window() directly rather than
driving the full poll_now() async path, which would require mocking collectors.
"""

import json
from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import LatestUsage, UsageEvent, UsageWindow
from app.services.pricing_seed import seed_pricing_table
from app.services.window_closer import _maybe_close_previous_window

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OLD_RESET = datetime(2026, 5, 12, 18, 0, 0, tzinfo=UTC)  # existing reset_at
_NEW_RESET = datetime(2026, 5, 19, 18, 0, 0, tzinfo=UTC)  # new reset_at (7d later)
_WINDOW_START = datetime(2026, 5, 5, 18, 0, 0, tzinfo=UTC)  # old_reset - 7 days


def _seeded_session():
    e = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(e)
    s = Session(e)
    seed_pricing_table(s)
    return s


def _add_event(
    session: Session,
    event_id: str,
    ts: datetime,
    *,
    model_id: str = "sonnet",
    sidecar_id: str = "dev-01",
    tokens_input: int = 100,
    tokens_output: int = 200,
    cost_usd: float = 0.01,
) -> None:
    ev = UsageEvent(
        provider_id="anthropic",
        account_id="user@example.com",
        sidecar_id=sidecar_id,
        event_id=event_id,
        ts=ts,
        model_id=model_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
    )
    session.add(ev)
    session.commit()


def _make_latest_usage(session: Session, reset_at: datetime) -> LatestUsage:
    card_json = json.dumps(
        {
            "provider_id": "anthropic",
            "account_id": "user@example.com",
            "window_type": "weekly",
            "reset_at": reset_at.isoformat(),
            "limit_value": 100_000,
            "pct_used": 0.42,
        }
    )
    row = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        sidecar_id="local",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=card_json,
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_window_close_triggered_when_reset_at_advances():
    """When new reset_at > existing reset_at, usage_windows rows are created."""
    s = _seeded_session()

    # Seed LatestUsage with old reset_at
    existing = _make_latest_usage(s, _OLD_RESET)

    # Seed events inside the previous window [_WINDOW_START, _OLD_RESET)
    mid = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    _add_event(s, "e1", mid, tokens_input=100, tokens_output=200, cost_usd=0.01)
    _add_event(s, "e2", mid, tokens_input=50, tokens_output=80, cost_usd=0.005)

    # Call the helper with the new reset_at
    _maybe_close_previous_window(
        s,
        existing=existing,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        new_reset_at=_NEW_RESET,
    )

    windows = s.exec(select(UsageWindow)).all()
    assert len(windows) > 0, "Expected usage_windows rows after reset_at advance"

    # All-up rollup row
    rollup = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "",
            UsageWindow.sidecar_id == "",
            UsageWindow.window_end == _OLD_RESET,
        )
    ).first()
    assert rollup is not None, "All-models/all-sidecars rollup row missing"
    assert rollup.msgs == 2
    assert rollup.tokens_input == 150
    # SQLite strips timezone info on round-trip; compare naive representations
    assert rollup.window_start.replace(tzinfo=None) == _WINDOW_START.replace(tzinfo=None)
    assert rollup.window_type == "weekly"


def test_window_close_not_triggered_when_reset_at_unchanged():
    """When new reset_at == existing reset_at, no usage_windows rows are inserted."""
    s = _seeded_session()

    existing = _make_latest_usage(s, _OLD_RESET)

    mid = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    _add_event(s, "e1", mid, tokens_input=100, tokens_output=200, cost_usd=0.01)

    _maybe_close_previous_window(
        s,
        existing=existing,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        new_reset_at=_OLD_RESET,  # same — no advance
    )

    windows = s.exec(select(UsageWindow)).all()
    assert len(windows) == 0, "No rows expected when reset_at has not advanced"


def test_window_close_not_triggered_on_subduration_reset_jitter():
    """A sub-duration reset_at advance (provider jitter) must not close a window.

    Anthropic's weekly resets_at oscillates ~±2s around the boundary between
    polls; every upward bounce previously archived a spurious window, flooding
    usage_windows. Only an advance of ~one full duration is a real rollover.
    """
    s = _seeded_session()

    existing = _make_latest_usage(s, _OLD_RESET)

    mid = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    _add_event(s, "e1", mid, tokens_input=100, tokens_output=200, cost_usd=0.01)

    # ~0.7s later — jitter, not a 7-day rollover.
    jittered = _OLD_RESET.replace(microsecond=700_000)
    _maybe_close_previous_window(
        s,
        existing=existing,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        new_reset_at=jittered,
    )

    windows = s.exec(select(UsageWindow)).all()
    assert len(windows) == 0, "Sub-duration jitter must not archive a window"


def test_window_close_not_triggered_when_no_existing_row():
    """When existing is None (first poll), no window-close occurs."""
    s = _seeded_session()

    mid = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    _add_event(s, "e1", mid, tokens_input=100, tokens_output=200, cost_usd=0.01)

    _maybe_close_previous_window(
        s,
        existing=None,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        new_reset_at=_NEW_RESET,
    )

    windows = s.exec(select(UsageWindow)).all()
    assert len(windows) == 0, "No rows expected for first poll"


def test_window_close_not_triggered_for_unknown_window_type():
    """Window types not in WINDOW_DURATION are silently skipped."""
    s = _seeded_session()

    existing = _make_latest_usage(s, _OLD_RESET)
    # Override card_json to have an unknown window_type
    existing.card_json = json.dumps(
        {
            "reset_at": _OLD_RESET.isoformat(),
            "window_type": "weekly_sonnet",  # not in WINDOW_DURATION
        }
    )
    s.add(existing)
    s.commit()

    mid = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    _add_event(s, "e1", mid, tokens_input=100, tokens_output=200, cost_usd=0.01)

    _maybe_close_previous_window(
        s,
        existing=existing,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly_sonnet",
        new_reset_at=_NEW_RESET,
    )

    windows = s.exec(select(UsageWindow)).all()
    assert len(windows) == 0, "Unknown window types should be skipped"


def test_window_close_idempotent_on_double_trigger():
    """Calling helper twice with same window params inserts rows only once."""
    s = _seeded_session()

    existing = _make_latest_usage(s, _OLD_RESET)

    mid = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    _add_event(s, "e1", mid, tokens_input=100, tokens_output=200, cost_usd=0.01)

    _maybe_close_previous_window(
        s,
        existing=existing,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        new_reset_at=_NEW_RESET,
    )
    first_count = len(s.exec(select(UsageWindow)).all())

    _maybe_close_previous_window(
        s,
        existing=existing,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        new_reset_at=_NEW_RESET,
    )
    second_count = len(s.exec(select(UsageWindow)).all())

    assert first_count > 0
    assert second_count == first_count, "Second call should not add duplicate rows"


def test_window_close_not_triggered_when_card_json_has_no_reset_at():
    """When card_json exists but has no reset_at field, no window-close occurs."""
    s = _seeded_session()

    card_json = json.dumps({"provider_id": "anthropic", "window_type": "weekly"})
    row = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        sidecar_id="local",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json=card_json,
    )
    s.add(row)
    s.commit()

    _maybe_close_previous_window(
        s,
        existing=row,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        new_reset_at=_NEW_RESET,
    )

    windows = s.exec(select(UsageWindow)).all()
    assert len(windows) == 0, "No reset_at in card_json should be a no-op"


def test_window_close_handles_invalid_card_json_gracefully():
    """Invalid card_json (unparseable) must not raise — returns 0."""
    s = _seeded_session()

    row = LatestUsage(
        provider_id="anthropic",
        account_id="user@example.com",
        sidecar_id="local",
        window_type="weekly",
        variant="default",
        model_id="",
        card_json="not valid json {{{",
    )
    s.add(row)
    s.commit()

    result = _maybe_close_previous_window(
        s,
        existing=row,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        new_reset_at=_NEW_RESET,
    )

    assert result == 0, "Malformed card_json should be silently skipped"
    windows = s.exec(select(UsageWindow)).all()
    assert len(windows) == 0
