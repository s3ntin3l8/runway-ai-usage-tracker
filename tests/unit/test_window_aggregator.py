"""TDD tests for query_window_aggregation helper.

Tests verify:
- Empty events → zero totals, empty by_model, empty by_sidecar.
- Token aggregation per model.
- Token aggregation per sidecar.
- Events outside [reset_at - duration, reset_at) are excluded.
- Events with kind='error' are excluded.
- per-model and per-sidecar sums match the total when all events have non-null
  model_id and sidecar_id.
"""

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent
from app.services.event_query import query_window_aggregation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RESET_AT = datetime(2026, 5, 12, 18, 0, 0, tzinfo=UTC)
# Weekly window → 7 days; window_start = 2026-05-05T18:00:00Z
_WINDOW_TYPE = "weekly"


def _engine():
    e = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(e)
    return e


def _session() -> Session:
    return Session(_engine())


def _add_event(
    session: Session,
    event_id: str,
    ts: datetime,
    *,
    provider_id: str = "anthropic",
    account_id: str = "user@example.com",
    model_id: str | None = "sonnet",
    sidecar_id: str = "dev-01",
    tokens_input: int = 100,
    tokens_output: int = 200,
    tokens_cache_read: int = 10,
    tokens_cache_create: int = 5,
    tokens_reasoning: int = 3,
    cost_usd: float = 0.01,
    kind: str = "message",
) -> None:
    ev = UsageEvent(
        provider_id=provider_id,
        account_id=account_id,
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


def _mid_window() -> datetime:
    """A timestamp safely inside the weekly window ending at _RESET_AT."""
    return datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_events_returns_zero_totals():
    """No events → all token fields are 0, by_model and by_sidecar are empty."""
    s = _session()
    result = query_window_aggregation(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type=_WINDOW_TYPE,
        reset_at=_RESET_AT,
    )
    tu = result["token_usage"]
    assert tu["input"] == 0
    assert tu["output"] == 0
    assert tu["cache_read"] == 0
    assert tu["cache_create"] == 0
    assert tu["reasoning"] == 0
    assert tu["total"] == 0
    assert result["cost_usd"] == 0.0
    assert result["by_model"] == {}
    assert result["by_sidecar"] == {}
    assert result["window_type"] == _WINDOW_TYPE


def test_aggregates_tokens_per_model():
    """Events for sonnet + opus → by_model has both with correct sums."""
    s = _session()
    # 2 sonnet events
    _add_event(
        s,
        "e1",
        _mid_window(),
        model_id="sonnet",
        tokens_input=100,
        tokens_output=200,
        tokens_cache_read=10,
        tokens_cache_create=5,
        tokens_reasoning=3,
    )
    _add_event(
        s,
        "e2",
        _mid_window() + timedelta(hours=1),
        model_id="sonnet",
        tokens_input=50,
        tokens_output=80,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    # 1 opus event
    _add_event(
        s,
        "e3",
        _mid_window() + timedelta(hours=2),
        model_id="opus",
        tokens_input=200,
        tokens_output=400,
        tokens_cache_read=20,
        tokens_cache_create=10,
        tokens_reasoning=7,
    )

    result = query_window_aggregation(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type=_WINDOW_TYPE,
        reset_at=_RESET_AT,
    )

    # 3 events, each with the default cost_usd=0.01 from _add_event.
    assert result["cost_usd"] == 0.03

    bm = result["by_model"]
    assert set(bm.keys()) == {"sonnet", "opus"}

    sonnet = bm["sonnet"]
    assert sonnet["tokens_input"] == 150  # 100+50
    assert sonnet["tokens_output"] == 280  # 200+80
    assert sonnet["tokens_cache_read"] == 10  # 10+0
    assert sonnet["tokens_cache_create"] == 5  # 5+0
    assert sonnet["tokens_reasoning"] == 3  # 3+0
    assert sonnet["msgs"] == 2

    opus = bm["opus"]
    assert opus["tokens_input"] == 200
    assert opus["tokens_output"] == 400
    assert opus["msgs"] == 1


def test_aggregates_per_sidecar():
    """Events from dev-01 + laptop → by_sidecar has both with correct sums."""
    s = _session()
    _add_event(
        s,
        "e1",
        _mid_window(),
        sidecar_id="dev-01",
        tokens_input=300,
        tokens_output=600,
        tokens_cache_read=30,
        tokens_cache_create=15,
        tokens_reasoning=9,
    )
    _add_event(
        s,
        "e2",
        _mid_window() + timedelta(hours=1),
        sidecar_id="dev-01",
        tokens_input=100,
        tokens_output=200,
        tokens_cache_read=0,
        tokens_cache_create=0,
        tokens_reasoning=0,
    )
    _add_event(
        s,
        "e3",
        _mid_window() + timedelta(hours=2),
        sidecar_id="laptop",
        tokens_input=50,
        tokens_output=80,
        tokens_cache_read=5,
        tokens_cache_create=2,
        tokens_reasoning=1,
    )

    result = query_window_aggregation(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type=_WINDOW_TYPE,
        reset_at=_RESET_AT,
    )

    bs = result["by_sidecar"]
    assert set(bs.keys()) == {"dev-01", "laptop"}

    dev01 = bs["dev-01"]
    assert dev01["tokens_input"] == 400  # 300+100
    assert dev01["tokens_output"] == 800  # 600+200
    assert dev01["msgs"] == 2

    laptop = bs["laptop"]
    assert laptop["tokens_input"] == 50
    assert laptop["tokens_output"] == 80
    assert laptop["msgs"] == 1


def test_excludes_events_outside_window():
    """Events before window_start and at/after reset_at are excluded."""
    s = _session()

    window_start = _RESET_AT - timedelta(days=7)
    before_window = window_start - timedelta(hours=1)
    at_reset = _RESET_AT  # excluded (open right boundary)
    after_reset = _RESET_AT + timedelta(hours=1)  # excluded

    # Boundary-excluded events (should not be counted)
    _add_event(s, "before", before_window, tokens_input=999, tokens_output=999)
    _add_event(s, "at_end", at_reset, tokens_input=888, tokens_output=888)
    _add_event(s, "after", after_reset, tokens_input=777, tokens_output=777)

    # One inside event (should be counted)
    _add_event(
        s,
        "inside",
        _mid_window(),
        tokens_input=100,
        tokens_output=200,
        tokens_cache_read=10,
        tokens_cache_create=5,
        tokens_reasoning=3,
    )

    result = query_window_aggregation(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type=_WINDOW_TYPE,
        reset_at=_RESET_AT,
    )

    tu = result["token_usage"]
    assert tu["input"] == 100
    assert tu["output"] == 200
    assert tu["cache_read"] == 10
    assert tu["cache_create"] == 5
    assert tu["reasoning"] == 3
    assert tu["total"] == 318  # 100+200+10+5+3


def test_excludes_kind_error_events():
    """Events with kind='error' are not counted in aggregation."""
    s = _session()

    # A normal message event
    _add_event(
        s,
        "msg1",
        _mid_window(),
        kind="message",
        tokens_input=100,
        tokens_output=200,
        tokens_cache_read=10,
        tokens_cache_create=5,
        tokens_reasoning=3,
    )
    # An error event (must be excluded)
    _add_event(
        s,
        "err1",
        _mid_window() + timedelta(hours=1),
        kind="error",
        tokens_input=9999,
        tokens_output=9999,
    )

    result = query_window_aggregation(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type=_WINDOW_TYPE,
        reset_at=_RESET_AT,
    )

    tu = result["token_usage"]
    assert tu["input"] == 100
    assert tu["output"] == 200


def test_full_breakdown_matches_total():
    """Per-model and per-sidecar sums each match the all-up total.

    All events must have non-null model_id and sidecar_id for this invariant
    to hold (NULL model_id / sidecar_id events are excluded from the
    by_model / by_sidecar breakdowns but counted in the total).
    """
    s = _session()

    # 3 events: all with explicit model_id and sidecar_id
    _add_event(
        s,
        "e1",
        _mid_window(),
        model_id="sonnet",
        sidecar_id="dev-01",
        tokens_input=100,
        tokens_output=200,
        tokens_cache_read=10,
        tokens_cache_create=5,
        tokens_reasoning=3,
    )
    _add_event(
        s,
        "e2",
        _mid_window() + timedelta(hours=1),
        model_id="opus",
        sidecar_id="dev-01",
        tokens_input=50,
        tokens_output=100,
        tokens_cache_read=5,
        tokens_cache_create=2,
        tokens_reasoning=1,
    )
    _add_event(
        s,
        "e3",
        _mid_window() + timedelta(hours=2),
        model_id="sonnet",
        sidecar_id="laptop",
        tokens_input=30,
        tokens_output=60,
        tokens_cache_read=3,
        tokens_cache_create=1,
        tokens_reasoning=0,
    )

    result = query_window_aggregation(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type=_WINDOW_TYPE,
        reset_at=_RESET_AT,
    )

    total = result["token_usage"]["total"]

    # Sum across all by_model entries must equal total
    bm_total = sum(
        m["tokens_input"]
        + m["tokens_output"]
        + m["tokens_cache_read"]
        + m["tokens_cache_create"]
        + m["tokens_reasoning"]
        for m in result["by_model"].values()
    )
    assert bm_total == total, f"by_model sum {bm_total} != total {total}"

    # Sum across all by_sidecar entries must equal total
    bs_total = sum(
        s_["tokens_input"]
        + s_["tokens_output"]
        + s_["tokens_cache_read"]
        + s_["tokens_cache_create"]
        + s_["tokens_reasoning"]
        for s_ in result["by_sidecar"].values()
    )
    assert bs_total == total, f"by_sidecar sum {bs_total} != total {total}"


def test_window_start_and_end_fields():
    """Result includes window_start and window_end ISO strings matching the computed window."""
    s = _session()
    result = query_window_aggregation(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type=_WINDOW_TYPE,
        reset_at=_RESET_AT,
    )
    expected_start = datetime(2026, 5, 5, 18, 0, 0, tzinfo=UTC).isoformat()
    assert result["window_start"] == expected_start
    assert result["window_end"] == _RESET_AT.isoformat()


def test_only_matches_correct_provider_and_account():
    """Events for a different provider/account are not included."""
    s = _session()

    # Target account
    _add_event(
        s,
        "e1",
        _mid_window(),
        provider_id="anthropic",
        account_id="user@example.com",
        tokens_input=100,
    )
    # Different account
    _add_event(
        s,
        "e2",
        _mid_window() + timedelta(hours=1),
        provider_id="anthropic",
        account_id="other@example.com",
        tokens_input=9999,
    )
    # Different provider
    _add_event(
        s,
        "e3",
        _mid_window() + timedelta(hours=2),
        provider_id="openai",
        account_id="user@example.com",
        tokens_input=8888,
    )

    result = query_window_aggregation(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        window_type=_WINDOW_TYPE,
        reset_at=_RESET_AT,
    )
    assert result["token_usage"]["input"] == 100
