"""Non-ASCII text must never be written with an implicit encoding.

``Path.write_text`` without an ``encoding`` argument uses the locale's
preferred encoding, which on a default Windows install is cp1252. Text with a
character outside cp1252 -- an arrow, an em-dash -- then raises
``UnicodeEncodeError``, and the error names neither the encoding nor the file.

The repository's own ``.gitignore`` contains three such characters, so a test
that copies it into a throwaway checkout used to report 16 errors on a Windows
checkout while passing on the Linux runner: the failure is a property of the
console encoding, not of the code.

``tests/conftest.py`` installs a session-wide guard that refuses such a write
anywhere in the suite. These tests cover the guard itself, because a guard that
nobody exercises is a guard that may not be installed at all.
"""

from __future__ import annotations

import pathlib

import pytest

from tests.fixtures.encoding_guard import ImplicitEncodingError


def test_the_guard_is_installed():
    """The session guard must be in place, or the other tests prove nothing.

    Asserted through the wrapper's own marker attribute rather than by
    behaviour: if the ``conftest.py`` wiring were deleted, the behavioural
    tests would still pass, because they would be testing the unguarded
    ``Path.write_text``.
    """
    assert getattr(pathlib.Path.write_text, "_encoding_guard", False) is True


def test_the_guard_refuses_non_ascii_without_an_encoding(tmp_path):
    """The failure it prevents, with the message that names the file."""
    with pytest.raises(ImplicitEncodingError, match="non-ASCII"):
        (tmp_path / "arrow.txt").write_text("Settings → Credentials")


def test_the_guard_allows_non_ascii_with_an_encoding(tmp_path):
    """An explicit encoding is the supported way to write non-ASCII text."""
    target = tmp_path / "arrow.txt"
    target.write_text("Settings → Credentials", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "Settings → Credentials"


def test_the_guard_allows_ascii_without_an_encoding(tmp_path):
    """ASCII is safe in every encoding, so the guard must leave it alone.

    219 of the suite's writes omit the encoding argument and write ASCII;
    failing those would be a false positive on every one of them.
    """
    target = tmp_path / "plain.txt"
    target.write_text("plain ascii")
    assert target.read_text(encoding="utf-8") == "plain ascii"


def test_the_guard_allows_a_write_that_cannot_fail(tmp_path):
    """``errors="replace"`` makes the write succeed, so it is not a failure.

    With the error handler set, cp1252 substitutes the characters it cannot
    encode instead of raising -- the file is written, just with different
    characters. Flagging that would be a false positive.
    """
    target = tmp_path / "arrow.txt"
    target.write_text("Settings → Credentials", errors="replace")
    assert target.exists()


def test_a_broad_except_cannot_swallow_the_error(tmp_path):
    """The signal has to survive the code under test.

    The write happens inside whatever the test exercises, and a broad
    ``except Exception:`` there would swallow an ordinary error and let the
    test pass without the file ever being written. That is why the guard's
    error is not an ``Exception``.
    """

    def swallows_everything() -> None:
        try:
            (tmp_path / "arrow.txt").write_text("Settings → Credentials")
        except Exception:  # noqa: BLE001 -- the point of the test
            pass

    assert not issubclass(ImplicitEncodingError, Exception)
    with pytest.raises(ImplicitEncodingError):
        swallows_everything()
