"""Time-of-day rate schedules for `provider_pricing` rows.

A row names a schedule (`rate_schedule`) and a multiplier
(`rate_schedule_multiplier`): while the event's UTC timestamp is inside the
schedule's window, every rate of the row (including its long-prompt tier) is
multiplied by it; outside the window the row's base rates apply. Rows without
a schedule behave exactly as before.

The window lives here, once, and has two renderings that must agree:
`schedule_multiplier` (Python, used by `cost_calculator`) and
`schedule_window_sql` (used by `maintenance/sql_cost`, the expression behind
the Data Health price checks). `tests/unit/test_pricing_schedule.py` checks
them against each other at every boundary hour.

Not modelled: public-holiday calendars (DeepSeek's peak excludes CN holidays,
so those few weekdays are billed at the peak multiplier here — a small
overcount of the cheaper holiday rate).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NamedTuple, Protocol

from sqlalchemy import Integer as SAInteger
from sqlalchemy import and_, case, cast, func, literal, or_

WEEKDAYS = (0, 1, 2, 3, 4)  # Python weekday(): Monday=0 .. Sunday=6
WEEKEND = (5, 6)


class Window(NamedTuple):
    """`weekdays` (Python numbering) x `[start_hour, end_hour)` in UTC."""

    weekdays: tuple[int, ...]
    start_hour: int
    end_hour: int


#: name -> windows in which the row's multiplier applies.
SCHEDULES: dict[str, tuple[Window, ...]] = {
    # DeepSeek direct: 2x peak, 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri
    # (https://api-docs.deepseek.com/quick_start/pricing, checked 2026-11-09).
    "deepseek_peak": (Window(WEEKDAYS, 1, 4), Window(WEEKDAYS, 6, 10)),
    # Ollama Cloud: half price "outside 12:00 and 18:00 UTC on weekdays and
    # all day on weekends" (https://ollama.com/pricing, checked 2026-11-09).
    "ollama_offpeak": (
        Window(WEEKDAYS, 0, 12),
        Window(WEEKDAYS, 18, 24),
        Window(WEEKEND, 0, 24),
    ),
}


class _Scheduled(Protocol):
    @property
    def rate_schedule(self) -> str | None:
        """Name of the schedule in `SCHEDULES`, or None."""
        raise NotImplementedError

    @property
    def rate_schedule_multiplier(self) -> float | None:
        """Factor applied inside the schedule's window, or None."""
        raise NotImplementedError


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def in_window(schedule: str, ts: datetime) -> bool:
    """True when `ts` (naive = UTC) falls inside a known schedule's window."""
    windows = SCHEDULES.get(schedule)
    if windows is None:
        return False
    utc = _utc(ts)
    return any(
        utc.weekday() in w.weekdays and w.start_hour <= utc.hour < w.end_hour for w in windows
    )


def schedule_multiplier(row: _Scheduled, ts: datetime) -> float:
    """Factor to apply to every rate of `row` for an event at `ts`; 1.0 when the
    row has no (known) schedule or `ts` is outside its window."""
    if not row.rate_schedule or row.rate_schedule_multiplier is None:
        return 1.0
    return row.rate_schedule_multiplier if in_window(row.rate_schedule, ts) else 1.0


def schedule_window_sql(schedule: str, ts_column: Any) -> Any | None:
    """SQL boolean for `in_window` over a UTC `ts_column` (SQLite `strftime`);
    `None` for an unknown schedule (never in window)."""
    windows = SCHEDULES.get(schedule)
    if windows is None:
        return None
    # strftime('%w') is 0=Sunday..6=Saturday; Python weekday() is Monday=0.
    sql_dow = cast(func.strftime("%w", ts_column), SAInteger)
    hour = cast(func.strftime("%H", ts_column), SAInteger)
    return or_(
        *(
            and_(
                sql_dow.in_([(d + 1) % 7 for d in w.weekdays]),
                hour >= w.start_hour,
                hour < w.end_hour,
            )
            for w in windows
        )
    )


def schedule_factor_sql(row: _Scheduled, ts_column: Any) -> Any | None:
    """SQL factor for the row's schedule, or `None` when it has none."""
    if not row.rate_schedule or row.rate_schedule_multiplier is None:
        return None
    window = schedule_window_sql(row.rate_schedule, ts_column)
    if window is None:
        return None
    return case((window, literal(row.rate_schedule_multiplier)), else_=literal(1.0))
