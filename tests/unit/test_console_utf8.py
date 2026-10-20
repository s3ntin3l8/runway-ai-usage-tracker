"""The console streams must survive non-ASCII output on Windows.

A redirected stdout/stderr gets the locale code page on Windows (cp1252 by
default), so a character outside it — an arrow in a docstring, a warning sign
in a log line — raises UnicodeEncodeError the moment it is printed. These
tests pin the helper that removes that failure mode.
"""

from __future__ import annotations

import io

from app.core.console import force_utf8_console


class _FakeStream:
    """A stream that records its reconfigure() calls."""

    def __init__(self):
        self.reconfigured: list[dict] = []

    def reconfigure(self, **kwargs):
        self.reconfigured.append(kwargs)


def test_a_stream_is_switched_to_utf8():
    stream = _FakeStream()
    force_utf8_console((stream,))
    assert stream.reconfigured == [{"encoding": "utf-8", "errors": "replace"}]


def test_non_ascii_output_survives_the_switch():
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252", newline="")
    force_utf8_console((stream,))
    stream.write("merge → rollup\n")
    stream.flush()
    assert buffer.getvalue().decode("utf-8") == "merge → rollup\n"


def test_a_missing_stream_is_ignored():
    # A windowed build has sys.stdout is None.
    force_utf8_console((None,))


def test_a_stream_that_cannot_be_reconfigured_is_ignored():
    class _Broken:
        def reconfigure(self, **kwargs):
            raise ValueError("I/O operation on closed file")

    force_utf8_console((_Broken(),))  # type: ignore[arg-type]


def test_a_stream_without_reconfigure_is_ignored():
    force_utf8_console((object(),))  # type: ignore[arg-type]
