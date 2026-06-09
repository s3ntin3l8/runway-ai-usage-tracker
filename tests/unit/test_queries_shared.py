"""Unit tests for app.services.queries._shared helpers."""

from datetime import UTC, datetime

from app.services.queries._shared import _parse_period_key


def test_parse_period_key_valid_month():
    assert _parse_period_key("2026-06", "month") == datetime(2026, 6, 1, tzinfo=UTC)


def test_parse_period_key_valid_day():
    assert _parse_period_key("2026-06-09", "day") == datetime(2026, 6, 9, tzinfo=UTC)


def test_parse_period_key_unrecognised_format_returns_none():
    """A malformed key for a known period_type hits the except branch and returns None."""
    assert _parse_period_key("garbage", "month") is None


def test_parse_period_key_unknown_period_type_returns_none():
    """An unknown period_type falls through to the final return None."""
    assert _parse_period_key("2026-06", "decade") is None
