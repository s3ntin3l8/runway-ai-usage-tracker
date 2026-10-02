"""Tests for app/services/maintenance/windows.py — rebuilding closed
usage_windows rows after events move or get recosted.

`close_window` writes one row per grain (('',''), (model,''), ('',sidecar),
(model,sidecar), deduped), so an event with both `model_id` and `sidecar_id`
set produces 4 rows for one window identity. Assertions here check the
returned *identity* count (what the maintenance functions report) and the
all-grains row (`model_id="" and sidecar_id=""`) for the preserved
`limit_value`/`pct_used`, not the raw row count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsageWindow
from app.services.maintenance.windows import (
    rebuild_windows_for_providers,
    rebuild_windows_overlapping,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _event(session: Session, **overrides) -> UsageEvent:
    base = {
        "provider_id": "minimax",
        "account_id": "alice@example.com",
        "sidecar_id": "dev-01",
        "event_id": f"msg_{overrides.get('event_id', 'x')}",
        "ts": datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        "kind": "message",
        "model_id": "MiniMax-M3",
        "tokens_input": 100,
        "tokens_output": 20,
        "cost_usd": 0.05,
    }
    base.update(overrides)
    ev = UsageEvent(**base)
    session.add(ev)
    session.commit()
    return ev


def _window(session: Session, **overrides) -> UsageWindow:
    base = {
        "provider_id": "minimax",
        "account_id": "alice@example.com",
        "window_type": "weekly",
        "window_start": datetime(2026, 8, 25, tzinfo=UTC),
        "window_end": datetime(2026, 9, 1, tzinfo=UTC),
        "model_id": "",
        "sidecar_id": "",
        "msgs": 1,
        "limit_value": 100.0,
        "pct_used": 42.0,
    }
    base.update(overrides)
    w = UsageWindow(**base)
    session.add(w)
    session.commit()
    return w


def _all_grains_row(session: Session, **where) -> UsageWindow | None:
    stmt = select(UsageWindow).where(UsageWindow.model_id == "", UsageWindow.sidecar_id == "")
    for k, v in where.items():
        stmt = stmt.where(getattr(UsageWindow, k) == v)
    return session.exec(stmt).first()


def test_rebuild_windows_for_providers_preserves_limit_and_pct():
    session = _session()
    _event(session, event_id="1", ts=datetime(2026, 8, 28, tzinfo=UTC))
    _window(session)

    count = rebuild_windows_for_providers(session, ["minimax"])

    assert count == 1  # one window identity
    row = _all_grains_row(session, provider_id="minimax", account_id="alice@example.com")
    assert row is not None
    assert row.limit_value == 100.0
    assert row.pct_used == 42.0
    assert row.msgs == 1  # rebuilt from the one event, not carried over


def test_rebuild_keeps_quota_series_with_shared_window_boundaries_separate():
    session = _session()
    _event(session, event_id="1", ts=datetime(2026, 8, 28, tzinfo=UTC))
    _window(
        session,
        series_model_id="sonnet",
        series_variant="default",
        limit_value=100.0,
        pct_used=25.0,
    )
    _window(
        session, series_model_id="opus", series_variant="default", limit_value=200.0, pct_used=75.0
    )

    count = rebuild_windows_for_providers(session, ["minimax"])

    assert count == 2
    rows = list(
        session.exec(
            select(UsageWindow).where(UsageWindow.model_id == "", UsageWindow.sidecar_id == "")
        )
    )
    assert {
        (row.series_model_id, row.series_variant, row.limit_value, row.pct_used) for row in rows
    } == {
        ("sonnet", "default", 100.0, 25.0),
        ("opus", "default", 200.0, 75.0),
    }


def test_rebuild_windows_for_providers_none_means_every_provider():
    session = _session()
    _event(session, event_id="1", provider_id="minimax", ts=datetime(2026, 8, 28, tzinfo=UTC))
    _event(session, event_id="2", provider_id="kimi_coding", ts=datetime(2026, 8, 28, tzinfo=UTC))
    _window(session, provider_id="minimax")
    _window(session, provider_id="kimi_coding")

    count = rebuild_windows_for_providers(session, None)

    assert count == 2
    providers = {w.provider_id for w in session.exec(select(UsageWindow))}
    assert providers == {"minimax", "kimi_coding"}


def test_rebuild_windows_for_providers_scopes_to_the_given_providers_only():
    session = _session()
    _event(session, event_id="1", provider_id="minimax", ts=datetime(2026, 8, 28, tzinfo=UTC))
    _event(session, event_id="2", provider_id="kimi_coding", ts=datetime(2026, 8, 28, tzinfo=UTC))
    _window(session, provider_id="minimax")
    _window(session, provider_id="kimi_coding")

    rebuild_windows_for_providers(session, ["minimax"])

    kept = _all_grains_row(session, provider_id="kimi_coding")
    assert kept is not None  # untouched — out of scope
    assert kept.msgs == 1  # still the original stored row, not rebuilt (0 or otherwise)


def test_rebuild_windows_overlapping_only_touches_the_overlapping_window():
    session = _session()
    _event(session, event_id="1", ts=datetime(2026, 8, 28, tzinfo=UTC))
    overlapping = _window(session)
    _window(
        session,
        window_start=datetime(2026, 9, 1, tzinfo=UTC),
        window_end=datetime(2026, 9, 8, tzinfo=UTC),
    )

    count = rebuild_windows_overlapping(
        session,
        provider_id="minimax",
        account_ids=["alice@example.com"],
        ts_min=datetime(2026, 8, 28, tzinfo=UTC),
        ts_max=datetime(2026, 8, 28, tzinfo=UTC),
    )

    assert count == 1
    non_overlapping = _all_grains_row(session, window_start=datetime(2026, 9, 1, tzinfo=UTC))
    assert non_overlapping is not None  # untouched
    rebuilt = _all_grains_row(session, window_start=overlapping.window_start)
    assert rebuilt is not None
    assert rebuilt.limit_value == overlapping.limit_value


def test_rebuild_windows_overlapping_preserves_metadata_per_account():
    """The reassign/merge use case: events moved from `default` to a real
    account. Only accounts with a *stored* window in the overlapping range
    get their boundary rebuilt — the repair follows recorded window
    boundaries, it doesn't invent a new one for an account that never had
    one (that's a live-scrape concern, not a repair concern)."""
    session = _session()
    # Some events already moved to alice; a stray one is still under default.
    _event(
        session, event_id="1", account_id="alice@example.com", ts=datetime(2026, 8, 28, tzinfo=UTC)
    )
    _event(session, event_id="2", account_id="default", ts=datetime(2026, 8, 29, tzinfo=UTC))
    _window(session, account_id="default", limit_value=100.0, pct_used=99.0)
    _window(session, account_id="alice@example.com", limit_value=100.0, pct_used=1.0)

    count = rebuild_windows_overlapping(
        session,
        provider_id="minimax",
        account_ids=["default", "alice@example.com"],
        ts_min=datetime(2026, 8, 28, tzinfo=UTC),
        ts_max=datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert count == 2  # both stored boundaries attempted
    default_row = _all_grains_row(session, provider_id="minimax", account_id="default")
    alice_row = _all_grains_row(session, provider_id="minimax", account_id="alice@example.com")
    assert default_row is not None
    assert default_row.msgs == 1  # the one event still under default
    assert alice_row is not None
    assert alice_row.msgs == 1  # the one event under alice
    # Metadata is preserved per-account, not overwritten across accounts.
    assert default_row.pct_used == 99.0
    assert alice_row.pct_used == 1.0


def test_rebuild_windows_for_providers_drops_a_window_with_no_supporting_events():
    """A window whose events all moved away entirely (nothing left in the
    range) must not survive the rebuild, even though its identity is
    still attempted (it had a stored row to seed the boundary from)."""
    session = _session()
    _window(session)  # no matching event at all

    count = rebuild_windows_for_providers(session, ["minimax"])

    assert count == 1  # attempted — it had a boundary to replay
    assert list(session.exec(select(UsageWindow))) == []  # nothing survives


def test_rebuild_windows_batches_without_losing_windows(monkeypatch):
    """Exercise the _WINDOW_BATCH commit/expunge cycling with more windows
    than one batch, without needing hundreds of real rows in the test."""
    monkeypatch.setattr("app.services.maintenance.windows._WINDOW_BATCH", 2)
    session = _session()
    base_ts = datetime(2026, 8, 1, tzinfo=UTC)
    for i in range(5):
        ts = base_ts + timedelta(days=i * 7)
        _event(session, event_id=str(i), ts=ts)
        _window(session, window_start=ts, window_end=ts + timedelta(days=7))

    count = rebuild_windows_for_providers(session, ["minimax"])

    assert count == 5
    rows = list(
        session.exec(
            select(UsageWindow).where(UsageWindow.model_id == "", UsageWindow.sidecar_id == "")
        )
    )
    assert len(rows) == 5
