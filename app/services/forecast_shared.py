"""Thresholds and value types shared by forecast assembly and trend fitting.

Kept out of both modules so neither imports the other at module scope.
"""

from dataclasses import dataclass

# Minimum number of distinct buckets before we trust a slope. Below this we
# return insufficient_data; otherwise a single recent spike skews the fit.
MIN_BUCKETS_FOR_TREND = 4

# Status thresholds in pct_used terms.
LIMIT_PCT = 100.0


@dataclass(frozen=True)
class NowState:
    """Card's current position."""

    used: float | None
    pct: float | None


@dataclass(frozen=True)
class Projection:
    """Projected position at the end of the window."""

    used: float | None = None
    pct: float | None = None
    hit_at: str | None = None
