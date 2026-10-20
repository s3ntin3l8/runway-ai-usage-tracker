"""Unit tests for the shared strict version validator.

The value it produces can end up in an HTTP header (the Antigravity
User-Agent's version decides which quota pools the Cloud Code Assist API
serves), so it must be plain ASCII digits — httpx encodes headers as ASCII and
raises on anything else.
"""

from app.core.version import normalize_version


class TestNormalizeVersion:
    def test_accepts_a_bare_version(self):
        assert normalize_version("1.4.3") == "1.4.3"

    def test_accepts_a_v_prefix(self):
        assert normalize_version("v1.4.3") == "1.4.3"

    def test_accepts_surrounding_whitespace(self):
        assert normalize_version(" 1.4.3\n") == "1.4.3"

    def test_accepts_one_or_two_parts(self):
        assert normalize_version("1") == "1"
        assert normalize_version("1.4") == "1.4"

    def test_rejects_anything_that_is_not_a_version(self):
        for bad in (
            "",
            None,
            "unknown",
            "1.4.3-beta",
            "1.4.3\r\nX-Injected: 1",
            "1..3",
            "v",
            "1.4.3 1.4.4",
            # More than three parts.
            "1.4.3.2",
        ):
            assert normalize_version(bad) is None

    def test_rejects_unicode_digits(self):
        """Python's ``\\d`` matches Unicode decimals; the header must not carry them."""
        assert normalize_version("1.٤.٩") is None
        assert normalize_version("١.٢.٣") is None
        assert normalize_version("１.２.３") is None

    def test_rejects_non_strings(self):
        """The value arrives over the ingest route — it is not a string until vetted."""
        for bad in (12345, 1.4, ["1.4.3"], {"version": "1.4.3"}, True):
            assert normalize_version(bad) is None
