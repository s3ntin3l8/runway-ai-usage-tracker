"""Tests for app.core.date_utils helpers."""

from datetime import UTC, datetime

from app.core.date_utils import parse_iso8601_utc


class TestParseIso8601Utc:
    """parse_iso8601_utc: offset-less → UTC, Z → UTC, aware → normalised."""

    def test_offset_less_string_assumed_utc(self):
        dt = parse_iso8601_utc("2026-06-01T00:00:00")
        assert dt.tzinfo is UTC
        assert dt.year == 2026 and dt.month == 6 and dt.day == 1

    def test_z_suffix_parsed(self):
        dt = parse_iso8601_utc("2026-06-01T12:30:00Z")
        assert dt.tzinfo is UTC

    def test_positive_offset_normalised(self):
        dt = parse_iso8601_utc("2026-06-01T12:00:00+05:00")
        assert dt.tzinfo is not None and dt.tzinfo.utcoffset(None).total_seconds() == 0
        assert dt.hour == 7  # 12 - 5 = 07 UTC

    def test_negative_offset_normalised(self):
        dt = parse_iso8601_utc("2026-06-01T03:00:00-05:00")
        assert dt.tzinfo is not None and dt.tzinfo.utcoffset(None).total_seconds() == 0
        assert dt.hour == 8  # 03 + 5 = 08 UTC

    def test_naive_datetime_assumed_utc(self):
        naive = datetime(2026, 6, 1, 0, 0, 0)
        dt = parse_iso8601_utc(naive)
        assert dt.tzinfo is UTC

    def test_aware_datetime_normalised_to_utc(self):
        from datetime import timedelta, timezone

        aware = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
        dt = parse_iso8601_utc(aware)
        assert dt.tzinfo is UTC
        assert dt.hour == 7

    def test_utc_datetime_unchanged(self):
        utc_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        dt = parse_iso8601_utc(utc_dt)
        assert dt is utc_dt  # astimezone(UTC) returns self for UTC input
