"""Iterate local SQLite results without retaining every raw row."""

import sqlite3
from collections.abc import Iterator
from typing import Any


class CursorReadError(Exception):
    """A cursor failed while reading, distinct from a parser/validation failure."""


def iter_cursor(cursor: sqlite3.Cursor) -> Iterator[Any]:
    """Translate read failures without intercepting errors in the caller's parser.

    SQLite Cursor.__iter__ returns itself; database reads happen in next().
    """
    rows = iter(cursor)
    while True:
        try:
            row = next(rows)
        except StopIteration:
            return
        except sqlite3.Error as exc:
            raise CursorReadError from exc
        yield row
