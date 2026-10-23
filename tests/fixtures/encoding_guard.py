"""Refuse to write non-ASCII text without an explicit encoding.

``Path.write_text`` without an ``encoding`` argument uses the locale's
preferred encoding, which on a default Windows install is cp1252. Text with a
character outside cp1252 -- an arrow, an em-dash -- then raises
``UnicodeEncodeError``, and the error names neither the encoding nor the file.
That is how ``tests/unit/test_gitignore_symlinked_dirs.py`` reported 16 errors
on a Windows checkout while passing on the Linux runner.

``tests/conftest.py`` installs this guard for the whole pytest session.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class ImplicitEncodingError(BaseException):
    """Raised when a test writes non-ASCII text without an explicit encoding.

    Deliberately **not** an ``Exception``. The write happens inside the code
    under test, and a broad ``except Exception:`` there would swallow the
    signal and let the test pass without the file ever being written -- two
    such sites exist in ``scripts/audit_published_images.py`` and
    ``scripts/audit_release_image.py``. Deriving from ``BaseException`` keeps
    the error out of their reach.

    A handler that catches ``BaseException`` outright would still swallow it.
    That is a narrower and more deliberate pattern, and the sites that use it
    are daemon and pairing loops rather than test write paths.
    """


@contextmanager
def encoding_guard() -> Iterator[None]:
    """Guard ``Path.write_text`` for the duration of the block.

    ASCII is always safe and is left alone; writing non-ASCII with an
    explicit ``encoding="utf-8"`` is the supported way to do it.
    """
    original = pathlib.Path.write_text

    def guarded_write_text(self: Any, data: Any, *args: Any, **kwargs: Any) -> Any:
        encoding = kwargs.get("encoding")
        if encoding is None and args:
            encoding = args[0]
        errors = kwargs.get("errors")
        if errors is None and len(args) >= 2:
            errors = args[1]
        # ``errors="replace"`` and friends make the write *succeed* on cp1252
        # by substituting the characters it cannot encode, so those are not
        # the failure this guard exists to catch. Only the strict default is.
        if (
            encoding is None
            and errors in (None, "strict")
            and isinstance(data, str)
            and not data.isascii()
        ):
            offenders = sorted({char for char in data if ord(char) > 127})
            raise ImplicitEncodingError(
                f"{self}: refusing to write non-ASCII text without an explicit "
                f'encoding (characters: {offenders!r}). Pass encoding="utf-8" -- '
                "the implicit encoding is the console's preferred one, which is "
                "cp1252 on a default Windows install, and the write raises "
                "UnicodeEncodeError there."
            )
        return original(self, data, *args, **kwargs)

    guarded_write_text._encoding_guard = True
    pathlib.Path.write_text = guarded_write_text
    try:
        yield
    finally:
        pathlib.Path.write_text = original
