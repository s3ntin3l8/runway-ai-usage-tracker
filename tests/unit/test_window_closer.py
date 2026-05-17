"""TDD tests for close_window aggregation logic.

Tests verify:
- Empty events → 0 rows inserted, no error.
- All-models/all-sidecars rollup row sums correctly.
- Per-model rows are written with correct per-model sums.
- Per-sidecar rows are written with correct per-sidecar sums.
- Full (model, sidecar) breakdown rows exist.
- Idempotent: second call inserts 0 rows.
- Events outside the window range are excluded.
"""

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent, UsageWindow
from app.services.pricing_seed import seed_pricing_table
from app.services.window_closer import close_window

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine():
    e = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(e)
    return e


def _seeded_session():
    e = _engine()
    s = Session(e)
    seed_pricing_table(s)
    return s


_WINDOW_START = datetime(2026, 5, 5, 18, 0, 0, tzinfo=UTC)
_WINDOW_END = datetime(2026, 5, 12, 18, 0, 0, tzinfo=UTC)


def _add_event(
    session: Session,
    event_id: str,
    ts: datetime,
    *,
    kind: str = "message",
    model_id: str | None = "sonnet",
    sidecar_id: str = "dev-01",
    tokens_input: int = 100,
    tokens_output: int = 200,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    cost_usd: float = 0.01,
) -> None:
    ev = UsageEvent(
        provider_id="anthropic",
        account_id="user@example.com",
        sidecar_id=sidecar_id,
        event_id=event_id,
        ts=ts,
        kind=kind,
        model_id=model_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        cost_usd=cost_usd,
    )
    session.add(ev)
    session.commit()


def _mid_window(hour: int = 12) -> datetime:
    """A timestamp safely inside [_WINDOW_START, _WINDOW_END)."""
    return datetime(2026, 5, 8, hour, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_close_window_with_no_events():
    """Empty events table → 0 rows inserted, no exception raised."""
    s = _seeded_session()
    inserted = close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )
    assert inserted == 0
    rows = s.exec(select(UsageWindow)).all()
    assert len(rows) == 0


def test_close_window_writes_all_models_rollup():
    """3 events with mixed models → all-models/all-sidecars row sums all 3."""
    s = _seeded_session()
    _add_event(
        s,
        "e1",
        _mid_window(10),
        model_id="sonnet",
        tokens_input=100,
        tokens_output=200,
        cost_usd=0.01,
    )
    _add_event(
        s,
        "e2",
        _mid_window(11),
        model_id="haiku",
        tokens_input=50,
        tokens_output=80,
        cost_usd=0.005,
    )
    _add_event(
        s, "e3", _mid_window(12), model_id=None, tokens_input=30, tokens_output=60, cost_usd=0.003
    )

    inserted = close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        limit_value=100_000,
        pct_used=0.42,
    )
    assert inserted > 0

    rollup = s.exec(
        select(UsageWindow).where(
            UsageWindow.provider_id == "anthropic",
            UsageWindow.account_id == "user@example.com",
            UsageWindow.model_id == "",
            UsageWindow.sidecar_id == "",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert rollup is not None, "All-models/all-sidecars rollup row missing"
    assert rollup.msgs == 3
    assert rollup.tokens_input == 180  # 100+50+30
    assert rollup.tokens_output == 340  # 200+80+60
    assert abs(rollup.cost_usd - 0.018) < 1e-9
    assert rollup.limit_value == 100_000
    assert rollup.pct_used == 0.42


def test_close_window_writes_per_model_rows():
    """Events with different models → per-model rows with correct sums."""
    s = _seeded_session()
    _add_event(
        s,
        "e1",
        _mid_window(10),
        model_id="sonnet",
        tokens_input=100,
        tokens_output=200,
        cost_usd=0.01,
    )
    _add_event(
        s,
        "e2",
        _mid_window(11),
        model_id="sonnet",
        tokens_input=50,
        tokens_output=80,
        cost_usd=0.005,
    )
    _add_event(
        s,
        "e3",
        _mid_window(12),
        model_id="haiku",
        tokens_input=30,
        tokens_output=60,
        cost_usd=0.003,
    )

    close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    # Per-model (sonnet), all-sidecars
    sonnet_row = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "sonnet",
            UsageWindow.sidecar_id == "",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert sonnet_row is not None, "Per-model sonnet row missing"
    assert sonnet_row.msgs == 2
    assert sonnet_row.tokens_input == 150  # 100+50
    assert sonnet_row.tokens_output == 280  # 200+80

    # Per-model (haiku), all-sidecars
    haiku_row = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "haiku",
            UsageWindow.sidecar_id == "",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert haiku_row is not None, "Per-model haiku row missing"
    assert haiku_row.msgs == 1
    assert haiku_row.tokens_input == 30


def test_close_window_writes_per_sidecar_rows():
    """Events from 2 different sidecars → per-sidecar rows with correct sums."""
    s = _seeded_session()
    _add_event(
        s,
        "e1",
        _mid_window(10),
        model_id="sonnet",
        sidecar_id="dev-01",
        tokens_input=100,
        tokens_output=200,
        cost_usd=0.01,
    )
    _add_event(
        s,
        "e2",
        _mid_window(11),
        model_id="sonnet",
        sidecar_id="dev-01",
        tokens_input=50,
        tokens_output=80,
        cost_usd=0.005,
    )
    _add_event(
        s,
        "e3",
        _mid_window(12),
        model_id="sonnet",
        sidecar_id="laptop",
        tokens_input=30,
        tokens_output=60,
        cost_usd=0.003,
    )

    close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    dev01_row = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "",
            UsageWindow.sidecar_id == "dev-01",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert dev01_row is not None, "Per-sidecar dev-01 row missing"
    assert dev01_row.msgs == 2
    assert dev01_row.tokens_input == 150

    laptop_row = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "",
            UsageWindow.sidecar_id == "laptop",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert laptop_row is not None, "Per-sidecar laptop row missing"
    assert laptop_row.msgs == 1
    assert laptop_row.tokens_input == 30


def test_close_window_writes_full_breakdown():
    """Full (model, sidecar) breakdown rows exist for each unique pair."""
    s = _seeded_session()
    _add_event(
        s,
        "e1",
        _mid_window(10),
        model_id="sonnet",
        sidecar_id="dev-01",
        tokens_input=100,
        tokens_output=200,
        cost_usd=0.01,
    )
    _add_event(
        s,
        "e2",
        _mid_window(11),
        model_id="haiku",
        sidecar_id="laptop",
        tokens_input=50,
        tokens_output=80,
        cost_usd=0.005,
    )
    _add_event(
        s,
        "e3",
        _mid_window(12),
        model_id="sonnet",
        sidecar_id="laptop",
        tokens_input=30,
        tokens_output=60,
        cost_usd=0.003,
    )

    close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    # (sonnet, dev-01)
    row_sd = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "sonnet",
            UsageWindow.sidecar_id == "dev-01",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert row_sd is not None, "(sonnet, dev-01) breakdown row missing"
    assert row_sd.msgs == 1

    # (haiku, laptop)
    row_hl = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "haiku",
            UsageWindow.sidecar_id == "laptop",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert row_hl is not None, "(haiku, laptop) breakdown row missing"
    assert row_hl.msgs == 1

    # (sonnet, laptop)
    row_sl = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "sonnet",
            UsageWindow.sidecar_id == "laptop",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert row_sl is not None, "(sonnet, laptop) breakdown row missing"
    assert row_sl.msgs == 1


def test_close_window_is_idempotent():
    """Calling close_window twice: second call inserts 0 rows."""
    s = _seeded_session()
    _add_event(
        s,
        "e1",
        _mid_window(),
        model_id="sonnet",
        tokens_input=100,
        tokens_output=200,
        cost_usd=0.01,
    )

    first = close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )
    second = close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    assert first > 0, "First call should insert rows"
    assert second == 0, "Second call should be a no-op (idempotent)"

    # Ensure no duplicate rows
    all_rows = s.exec(select(UsageWindow)).all()
    assert len(all_rows) == first


def test_close_window_excludes_error_events():
    """`kind='error'` events must not contribute to window totals.

    Closes the consistency gap flagged in the audit (D5): rollups already
    skip error events in event_ingestor, but `close_window` was selecting
    all events regardless of kind. The result was window totals exceeding
    the sum of daily rollups for the same range. Quota-window aggregates
    represent billable usage; an upstream API failure isn't usage.
    """
    s = _seeded_session()
    _add_event(
        s,
        "ok1",
        _mid_window(10),
        model_id="sonnet",
        tokens_input=100,
        tokens_output=200,
        cost_usd=0.01,
    )
    _add_event(
        s,
        "err1",
        _mid_window(11),
        kind="error",
        model_id="sonnet",
        # Use large numbers so this event would visibly skew totals if counted.
        tokens_input=999,
        tokens_output=999,
        cost_usd=9.99,
    )
    _add_event(
        s,
        "ok2",
        _mid_window(12),
        model_id="sonnet",
        tokens_input=50,
        tokens_output=80,
        cost_usd=0.005,
    )

    inserted = close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )
    assert inserted > 0

    rollup = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "",
            UsageWindow.sidecar_id == "",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert rollup is not None
    assert rollup.msgs == 2, f"expected 2 messages (err excluded), got {rollup.msgs}"
    assert rollup.tokens_input == 150  # 100 + 50, no 999
    assert rollup.tokens_output == 280  # 200 + 80, no 999
    assert abs(rollup.cost_usd - 0.015) < 1e-9  # 0.01 + 0.005, no 9.99


def test_close_window_with_only_error_events_inserts_nothing():
    """When the window contains only error events, no usage_windows row
    should be written — there's no usage to capture."""
    s = _seeded_session()
    _add_event(s, "err1", _mid_window(10), kind="error", model_id="sonnet")
    _add_event(s, "err2", _mid_window(11), kind="error", model_id="haiku")

    inserted = close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )
    assert inserted == 0
    assert s.exec(select(UsageWindow)).all() == []


def test_close_window_excludes_events_outside_range():
    """Events before window_start or at/after window_end are excluded."""
    s = _seeded_session()

    before = datetime(2026, 5, 5, 10, 0, 0, tzinfo=UTC)  # before _WINDOW_START
    at_end = datetime(2026, 5, 12, 18, 0, 0, tzinfo=UTC)  # == _WINDOW_END (excluded)
    after = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)  # after _WINDOW_END

    _add_event(
        s,
        "outside_before",
        before,
        model_id="sonnet",
        tokens_input=999,
        tokens_output=999,
        cost_usd=9.99,
    )
    _add_event(
        s,
        "inside",
        _mid_window(),
        model_id="sonnet",
        tokens_input=100,
        tokens_output=200,
        cost_usd=0.01,
    )
    _add_event(
        s,
        "outside_at_end",
        at_end,
        model_id="sonnet",
        tokens_input=888,
        tokens_output=888,
        cost_usd=8.88,
    )
    _add_event(
        s,
        "outside_after",
        after,
        model_id="sonnet",
        tokens_input=777,
        tokens_output=777,
        cost_usd=7.77,
    )

    close_window(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type="weekly",
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )

    rollup = s.exec(
        select(UsageWindow).where(
            UsageWindow.model_id == "",
            UsageWindow.sidecar_id == "",
            UsageWindow.window_end == _WINDOW_END,
        )
    ).first()
    assert rollup is not None
    assert rollup.msgs == 1  # only "inside" event
    assert rollup.tokens_input == 100
    assert rollup.tokens_output == 200
    assert abs(rollup.cost_usd - 0.01) < 1e-9
