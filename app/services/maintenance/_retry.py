"""Retry utilities for maintenance operations under SQLite lock contention."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from sqlalchemy.exc import OperationalError
from sqlmodel import Session

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_INITIAL_BACKOFF = 0.05


def _is_sqlite_locked_or_busy(exc: OperationalError) -> bool:
    """True if `exc` represents SQLite SQLITE_BUSY or SQLITE_LOCKED contention.

    Inspects `exc.orig` for the driver-level error name/code first to prevent false
    positives from bound parameters containing the words 'locked' or 'busy'.
    """
    orig = getattr(exc, "orig", None)
    if orig is not None:
        errorname = getattr(orig, "sqlite_errorname", None)
        if errorname and (
            errorname in ("SQLITE_BUSY", "SQLITE_LOCKED")
            or errorname.startswith(("SQLITE_BUSY_", "SQLITE_LOCKED_"))
        ):
            return True
        errorcode = getattr(orig, "sqlite_errorcode", None)
        if errorcode is not None and (errorcode & 0xFF) in (5, 6):  # SQLITE_BUSY=5, SQLITE_LOCKED=6
            return True
        msg = str(orig).lower()
        if "database is locked" in msg or "database is busy" in msg:
            return True
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def _retry_on_lock[T](
    fn: Callable[[], T],
    *,
    session: Session,
    action_desc: str,
    max_retries: int = _MAX_RETRIES,
    initial_backoff: float = _INITIAL_BACKOFF,
) -> T:
    """Execute `fn`, rolling back and retrying with exponential backoff on SQLite lock contention."""
    if max_retries <= 0:
        raise ValueError(f"max_retries must be positive, got {max_retries}")
    if initial_backoff < 0:
        raise ValueError(f"initial_backoff must be non-negative, got {initial_backoff}")
    for attempt in range(max_retries):
        try:
            return fn()
        except OperationalError as exc:
            if _is_sqlite_locked_or_busy(exc) and attempt < max_retries - 1:
                session.rollback()
                delay = initial_backoff * (2**attempt) + random.uniform(0.01, 0.05)
                logger.warning(
                    "Database locked during %s (attempt %d/%d); retrying in %.2fs",
                    action_desc,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                time.sleep(delay)
                continue
            raise
    raise AssertionError("unreachable: retry loop either returns or re-raises")
