"""Tests for query_heatmap timezone-aware bucketing.

Verifies:
- Without `tz`, falls back to UTC bucketing (existing SQL `strftime` path).
- With `tz`, events are re-bucketed by the user's local hour-of-day.
- Invalid IANA names fall back to UTC silently (don't crash).
- DST handling — `zoneinfo` is used (not a fixed offset), so a tz with a
  spring-forward inside the window doesn't smear cells.
"""

from datetime import UTC, datetime

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import UsageEvent
from app.services.event_query import query_heatmap

# Fixed window anchor so the May 2026 event timestamps below never age out
# of the rolling `days` window (they're 12–13 days before this, inside both
# days=14 and days=30). Without this the tests are time bombs.
_NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)


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
    tokens: int = 100,
    cost_usd: float = 0.0,
):
    ev = UsageEvent(
        provider_id="anthropic",
        account_id="user@example.com",
        sidecar_id="local",
        event_id=event_id,
        ts=ts,
        kind="message",
        model_id="sonnet",
        tokens_input=tokens,
        cost_usd=cost_usd,
    )
    session.add(ev)
    session.commit()


def _cell(cells, dow: int, hour: int) -> int:
    for c in cells:
        if c["dow"] == dow and c["hour"] == hour:
            return c["tokens"]
    raise AssertionError(f"missing cell dow={dow} hour={hour}")


def test_utc_bucketing_when_tz_missing():
    s = _session()
    # Event at 2026-05-07 14:30 UTC — that's a Thursday.
    # SQLite %w: Sun=0..Sat=6 → Thursday = 4.
    _add_event(s, "e1", datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC), tokens=500)

    cells = query_heatmap(
        s, provider_id="anthropic", account_id="user@example.com", days=30, now=_NOW
    )

    assert _cell(cells, dow=4, hour=14) == 500
    assert sum(c["tokens"] for c in cells) == 500


def test_local_bucketing_shifts_with_tz():
    s = _session()
    # Same UTC event at 2026-05-07 14:30 UTC. In Europe/Berlin (UTC+2 in May
    # due to DST), the local time is 16:30, still Thursday.
    _add_event(s, "e1", datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC), tokens=500)

    cells_utc = query_heatmap(
        s, provider_id="anthropic", account_id="user@example.com", days=30, tz="UTC", now=_NOW
    )
    cells_berlin = query_heatmap(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        days=30,
        tz="Europe/Berlin",
        now=_NOW,
    )

    # UTC: hour 14 on Thursday
    assert _cell(cells_utc, dow=4, hour=14) == 500
    # Berlin (CEST = UTC+2 in May): hour 16, still Thursday
    assert _cell(cells_berlin, dow=4, hour=16) == 500
    # And NOT in the 14:00 Berlin cell — it must have moved.
    assert _cell(cells_berlin, dow=4, hour=14) == 0


def test_local_bucketing_can_change_day_of_week():
    s = _session()
    # Event at 2026-05-08 01:30 UTC (a Friday). In Asia/Tokyo (UTC+9) that's
    # 10:30 on Friday, but in America/Los_Angeles (UTC-7 with DST = -7 in May)
    # it's 18:30 on THURSDAY.
    _add_event(s, "e1", datetime(2026, 5, 8, 1, 30, 0, tzinfo=UTC), tokens=750)

    cells_la = query_heatmap(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        days=30,
        tz="America/Los_Angeles",
        now=_NOW,
    )

    # Pacific: Thursday (dow=4) 18:00 — proves dow remap from Python Mon=0..Sun=6
    # to wire convention Sun=0..Sat=6 works AND the day rolled back.
    assert _cell(cells_la, dow=4, hour=18) == 750


def test_invalid_tz_falls_back_to_utc():
    s = _session()
    _add_event(s, "e1", datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC), tokens=500)

    cells = query_heatmap(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        days=30,
        tz="Not/A/Real_Zone",
        now=_NOW,
    )

    # Should match the UTC bucketing — bug-for-bug compatible with no tz.
    assert _cell(cells, dow=4, hour=14) == 500


def test_returns_full_168_cells():
    s = _session()
    cells = query_heatmap(
        s,
        provider_id="anthropic",
        account_id="user@example.com",
        days=14,
        tz="Europe/Berlin",
        now=_NOW,
    )
    assert len(cells) == 168
    # All zero — no events.
    assert sum(c["tokens"] for c in cells) == 0
    assert sum(c["cost_usd"] for c in cells) == 0.0
    # dow values present 0..6, hour values 0..23
    dows = sorted({c["dow"] for c in cells})
    hours = sorted({c["hour"] for c in cells})
    assert dows == list(range(7))
    assert hours == list(range(24))


def test_cost_usd_is_summed_per_cell():
    s = _session()
    # Two events in the same UTC (dow, hour) bucket, plus one in a different cell.
    _add_event(s, "e1", datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC), tokens=200, cost_usd=1.25)
    _add_event(s, "e2", datetime(2026, 5, 7, 14, 45, 0, tzinfo=UTC), tokens=300, cost_usd=0.75)
    _add_event(s, "e3", datetime(2026, 5, 7, 9, 0, 0, tzinfo=UTC), tokens=100, cost_usd=0.50)

    cells = query_heatmap(
        s, provider_id="anthropic", account_id="user@example.com", days=30, now=_NOW
    )

    # Helper to fetch a whole cell dict.
    def _full(dow: int, hour: int) -> dict:
        for c in cells:
            if c["dow"] == dow and c["hour"] == hour:
                return c
        raise AssertionError(f"missing cell dow={dow} hour={hour}")

    # Thursday (dow=4) 14:00 — two events summed.
    cell_thu_14 = _full(4, 14)
    assert cell_thu_14["tokens"] == 500
    assert cell_thu_14["cost_usd"] == 2.0

    # Thursday 09:00 — one event.
    cell_thu_9 = _full(4, 9)
    assert cell_thu_9["cost_usd"] == 0.50

    # Totals across the grid.
    assert sum(c["cost_usd"] for c in cells) == 2.50
